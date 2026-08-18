"""Session and partial-file management for chunked, resumable uploads.

Everything that governs a file mid-flight lives here: per-user quotas, where the
``.part`` file sits, the crash-safe append, and the sweep that reclaims abandoned
transfers. The route layer stays a thin translation between HTTP and these calls,
which is what keeps the integrity rules in one auditable place instead of spread
across four endpoints.

`docs/upload_protocol.md` is the contract; this module is its server half. The
exceptions below carry the status code the protocol assigns to each fault, so a
route can map them without re-deriving the table.
"""

import fcntl
import os
import time
import uuid
import hashlib
import hmac
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, insert, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import ObjectDeletedError

from .models import UploadSession
from .config import (
    RE_CLIENT_KEY,
    UPLOAD_PARTIALS_SUBDIR,
    UPLOAD_SESSION_TTL_HOURS,
    MAX_CONCURRENT_UPLOADS_PER_USER,
    MAX_INFLIGHT_UPLOAD_BYTES_PER_USER,
)


class UploadError(Exception):
    """Base for upload faults, carrying the HTTP status the protocol assigns them."""
    status_code = 400

    def __init__(self, message, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra  # merged into the JSON error body by the route layer


class OffsetMismatch(UploadError):
    """The chunk did not start where the resume cursor sits.

    Normal control flow rather than an error — it is how a resuming client, or the
    loser of a two-tab race, discovers where to seek. The true offset rides in
    ``extra`` so the client can re-seek without a second round trip, and the
    limiter must not charge for it.
    """
    status_code = 409

    def __init__(self, received_bytes):
        super().__init__("Chunk offset does not match the session's resume cursor",
                         received_bytes=received_bytes)
        self.received_bytes = received_bytes


class ChecksumMismatch(UploadError):
    """The chunk's bytes did not match the SHA-256 the client declared for them.

    Means the payload arrived corrupt or forged; nothing is written. Retrying the
    same chunk is the correct client response, which is why it is 422 and not 400.
    """
    status_code = 422


class MissingDigest(UploadError):
    """The chunk arrived without the ``X-Chunk-SHA256`` header the protocol requires.

    Separate from ChecksumMismatch, and deliberately a 400 rather than a 422: a 422
    tells the client "the bytes were corrupt, send them again", whereas an absent
    header is a malformed request that will be malformed again on every retry.

    The distinction is load-bearing rather than cosmetic. Verification used to be
    skipped when the header was absent, which made omitting it both a way to write
    unverified bytes into someone's file *and* a way to sidestep the rate-limit
    charge that a bad digest earns. Refusing outright closes both at once.
    """
    status_code = 400


class ChunkOverrun(UploadError):
    """Accepting the chunk would push the partial past the size declared at init."""
    status_code = 413


class SessionUnusable(UploadError):
    """The session's partial file is gone, so there is nothing left to resume into.

    Reported as 404 alongside unknown and expired sessions: the client's response to
    all three is identical — evict the stored mapping and re-init.
    """
    status_code = 404


class QuotaExceeded(UploadError):
    """The user already has as many bytes or sessions in flight as they are allowed."""
    status_code = 429


# ── Partial files ──────────────────────────────────────────────────────────

def partials_dir(upload_dir):
    """Return the directory holding in-progress ``.part`` files, creating it if absent.

    A sub-directory of UPLOAD_FOLDER rather than the folder itself, so a half-written
    file can never be confused with a committed one by anything walking the media root.
    """
    path = Path(upload_dir) / UPLOAD_PARTIALS_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_valid_client_key(client_key):
    """Return True if the client's idempotency key has the expected 64-hex-char shape."""
    return bool(client_key) and bool(RE_CLIENT_KEY.match(client_key))


# ── Session lifecycle ──────────────────────────────────────────────────────

def get_session(db_session, upload_id, user_id=None, album_id=None):
    """Look up a session by its client-facing ``upload_id``, optionally scoped to an owner.

    Pass ``user_id`` and ``album_id`` on every request that acts on a session: the
    ``upload_id`` is a bearer handle, and re-checking ownership per chunk is what stops
    one user's leaked handle from becoming another user's write target.
    """
    stmt = select(UploadSession).where(UploadSession.upload_id == upload_id)
    if user_id is not None:
        stmt = stmt.where(UploadSession.user_id == user_id)
    if album_id is not None:
        stmt = stmt.where(UploadSession.album_id == album_id)
    return db_session.execute(stmt).scalar_one_or_none()


def find_session_by_client_key(db_session, user_id, album_id, client_key):
    """Return the session already in flight for this user, album and file, or None."""
    return db_session.execute(
        select(UploadSession).where(
            UploadSession.user_id == user_id,
            UploadSession.album_id == album_id,
            UploadSession.client_key == client_key,
        )
    ).scalar_one_or_none()


def open_sessions_for_user(db_session, user_id):
    """Return every upload this user currently has in flight, newest last."""
    return list(db_session.execute(
        select(UploadSession)
        .where(UploadSession.user_id == user_id)
        .order_by(UploadSession.created_at)
    ).scalars())


def inflight_bytes_for_user(db_session, user_id):
    """Return the disk this user's open sessions have committed to, counting declared totals.

    Declared totals rather than bytes actually written: a session that has received
    one chunk still intends to reach ``total_size``, and a quota that only counted
    what had landed would happily admit a hundred uploads that collectively cannot fit.
    """
    total = db_session.execute(
        select(func.coalesce(func.sum(UploadSession.total_size), 0))
        .where(UploadSession.user_id == user_id)
    ).scalar_one()
    return int(total or 0)


def check_user_quota(db_session, user_id, total_size,
                     max_sessions=MAX_CONCURRENT_UPLOADS_PER_USER,
                     max_bytes=MAX_INFLIGHT_UPLOAD_BYTES_PER_USER):
    """Raise QuotaExceeded if opening one more session of this size would exceed a per-user cap.

    These DB-backed caps are the load-bearing defence against a client filling the
    disk with partials it never completes. The rate limiter cannot do this job:
    it is per-worker, in-memory, and resets on every deploy.

    This is the *diagnostic* half of the quota, not the enforcing one. Read-then-insert
    across two statements is a time-of-check/time-of-use window — pysqlite runs a bare
    SELECT in autocommit, so nothing holds a lock between the count below and the INSERT
    that follows, and a burst of concurrent inits sails straight through it (measured:
    16 sessions admitted against a cap of 10). ``create_session`` re-imposes both caps
    inside the INSERT itself and is the authority; this function runs first only because
    it can tell the two caps apart and say *which* one was hit, which the guarded insert
    cannot. It returns the caps it enforced so the caller can hand exactly the same
    numbers to the insert — otherwise a test or caller that overrides them here would
    silently be checked against different ones there.
    """
    open_count = db_session.execute(
        select(func.count(UploadSession.id)).where(UploadSession.user_id == user_id)
    ).scalar_one()
    if open_count >= max_sessions:
        raise QuotaExceeded(
            f"Too many uploads in progress ({open_count}/{max_sessions}). "
            "Finish or cancel one before starting another."
        )

    projected = inflight_bytes_for_user(db_session, user_id) + int(total_size)
    if projected > max_bytes:
        raise QuotaExceeded(
            f"Upload would exceed your in-flight limit of {max_bytes // (1024 * 1024)} MB."
        )
    return max_sessions, max_bytes


def create_session(db_session, upload_dir, user_id, album_id, client_key,
                   original_filename, total_size,
                   max_sessions=MAX_CONCURRENT_UPLOADS_PER_USER,
                   max_bytes=MAX_INFLIGHT_UPLOAD_BYTES_PER_USER):
    """Open a new session under both per-user caps and lay down the ``.part`` file it grows into.

    The row is inserted by an ``INSERT ... SELECT ... WHERE <both caps still hold>``
    rather than by the ORM. That single statement is what actually enforces the quota:
    SQLite evaluates the guard and appends the row under one write lock, so two inits
    racing for the last slot cannot both read "9 open" and both insert. The ORM path it
    replaces read the counts in one statement and inserted in another, which admitted
    ~1.6x either cap under concurrency and left the overshoot pinned for the full 24 h
    TTL. A conditional statement was chosen over wrapping the pair in ``BEGIN
    IMMEDIATE`` because it needs no change to how the engine begins transactions —
    pysqlite's deferred BEGIN would otherwise have to be overridden globally, for every
    request in the app, to fix one insert.

    The caps are parameters rather than constants read here so the numbers the caller
    already checked against in ``check_user_quota`` are the numbers enforced.

    Raises QuotaExceeded if the guard refuses the row. The file is created up front so
    every later append can open it ``r+b`` and treat a missing partial as an
    unrecoverable session rather than silently starting over at an offset the client
    believes it has already passed.
    """
    now = datetime.utcnow()
    open_count = (
        select(func.count(UploadSession.id))
        .where(UploadSession.user_id == user_id)
        .scalar_subquery()
    )
    inflight = (
        select(func.coalesce(func.sum(UploadSession.total_size), 0))
        .where(UploadSession.user_id == user_id)
        .scalar_subquery()
    )
    guarded_row = select(
        literal(str(uuid.uuid4())),      # upload_id: the model's default never runs here
        literal(album_id),
        literal(user_id),
        literal(client_key),
        literal(original_filename),
        literal(int(total_size)),
        literal(0),                      # received_bytes
        literal(now),
        literal(now),
    ).where(
        open_count < max_sessions,
        inflight + int(total_size) <= max_bytes,
    )
    result = db_session.execute(insert(UploadSession).from_select(
        ['upload_id', 'album_id', 'user_id', 'client_key', 'original_filename',
         'total_size', 'received_bytes', 'created_at', 'updated_at'],
        guarded_row,
    ))
    db_session.commit()

    if result.rowcount != 1:
        # Lost the race. Re-run the diagnostic check so the client gets the specific
        # message for whichever cap the winner filled; it will normally raise. If it
        # does not — the winner finished or was swept in the microseconds since — fall
        # back to a generic 429 rather than pretending the insert succeeded.
        check_user_quota(db_session, user_id, total_size, max_sessions, max_bytes)
        raise QuotaExceeded("Too many uploads in progress. Try again in a moment.")

    upload_session = find_session_by_client_key(db_session, user_id, album_id, client_key)
    partials_dir(upload_dir)
    upload_session.partial_path(upload_dir).touch()
    return upload_session


def open_or_recover_session(db_session, upload_dir, user_id, album_id, client_key,
                            original_filename, total_size,
                            ttl_hours=UPLOAD_SESSION_TTL_HOURS):
    """Return ``(session, created)`` for an init request, reusing an in-flight one if there is one.

    This is what makes ``init`` idempotent on ``client_key``: re-picking the same file
    resumes the existing transfer at its true offset instead of orphaning the partial.
    An expired session, or one whose declared size no longer matches, is discarded and
    replaced — the client key covers name, size and mtime, so a mismatch means the row
    is stale rather than that the client is confused.
    """
    existing = find_session_by_client_key(db_session, user_id, album_id, client_key)
    if existing is not None:
        partial_exists = existing.partial_path(upload_dir).exists()
        if (not existing.is_expired(ttl_hours)
                and existing.total_size == int(total_size)
                and partial_exists):
            return existing, False
        discard_session(db_session, existing, upload_dir)

    max_sessions, max_bytes = check_user_quota(db_session, user_id, total_size)
    try:
        return create_session(db_session, upload_dir, user_id, album_id, client_key,
                              original_filename, total_size,
                              max_sessions=max_sessions, max_bytes=max_bytes), True
    except IntegrityError:
        # Two tabs can hit `init` for the same file at once; the unique constraint
        # decides the winner. Fall back to the row that landed rather than failing the
        # loser, so both clients converge on one session instead of one partial each.
        db_session.rollback()
        existing = find_session_by_client_key(db_session, user_id, album_id, client_key)
        if existing is None:
            raise
        return existing, False


def discard_session(db_session, upload_session, upload_dir):
    """Delete a session row and unlink its partial, in that order.

    Row first: a stray ``.part`` with no row is inert and the sweep will collect it,
    whereas a row pointing at a file that is already gone would strand the client
    resuming into nothing.
    """
    path = upload_session.partial_path(upload_dir)
    db_session.delete(upload_session)
    db_session.commit()
    path.unlink(missing_ok=True)


def discard_sessions_for_album(db_session, album_id, upload_dir):
    """Discard every session in flight against an album, returning how many went.

    Called when the album itself is deleted. Nothing else collects these: the rows
    carry a foreign key to ``album``, but SQLite runs with ``PRAGMA foreign_keys = 0``
    and there is no ORM cascade, so the row outlives the album it points at. It is then
    dead weight in the worst way — every chunk against it 404s because the share token
    no longer resolves, yet ``check_user_quota`` still counts it, charging the uploader
    a concurrency slot and its full declared ``total_size`` until the TTL expires a day
    later. The sweep remains the backstop for rows orphaned by a crash mid-delete.
    """
    sessions = list(db_session.execute(
        select(UploadSession).where(UploadSession.album_id == album_id)
    ).scalars())
    for upload_session in sessions:
        discard_session(db_session, upload_session, upload_dir)
    return len(sessions)


# ── Chunk appends ──────────────────────────────────────────────────────────

def ensure_offset_matches(upload_session, offset):
    """Raise OffsetMismatch unless a chunk starting here lines up with the resume cursor.

    Split out of ``append_chunk`` so the route can answer a mis-aimed chunk *before*
    reading its body. A 409 is the one refusal a well-behaved client produces in bulk —
    every resume and every two-tab race is one — and it was the cheapest sink on the
    endpoint: an 8 MiB body read into memory, and a libmagic sniff on top of it, purely
    to discover an offset the session already knew was wrong.
    """
    if not upload_session.accepts_chunk_at(offset):
        raise OffsetMismatch(upload_session.received_bytes)


def append_chunk(db_session, upload_session, upload_dir, data, offset, chunk_sha256):
    """Append one verified chunk to the partial and advance the cursor, returning ``received_bytes``.

    Raises OffsetMismatch (409), MissingDigest (400), ChecksumMismatch (422),
    ChunkOverrun (413) or SessionUnusable (404); nothing is written unless every check
    passes. ``chunk_sha256`` has no default — the digest is mandatory, and a default
    would let a caller re-introduce the unverified path by forgetting an argument.
    """
    ensure_offset_matches(upload_session, offset)

    if upload_session.would_overrun(len(data)):
        raise ChunkOverrun(
            "Chunk would push the upload past the size declared at init"
        )

    if not chunk_sha256:
        raise MissingDigest("X-Chunk-SHA256 is required on every chunk")

    # compare_digest rather than == so a caller probing for a valid digest cannot
    # read progress out of the comparison's timing.
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), chunk_sha256.lower()):
        raise ChecksumMismatch("Chunk checksum does not match the declared SHA-256")

    try:
        fh = open(upload_session.partial_path(upload_dir), 'r+b')
    except FileNotFoundError:
        raise SessionUnusable("Partial file for this upload is missing")

    with fh:
        # Everything from here to the commit is one critical section, held on the
        # partial itself. Without it the append is check-then-write: six concurrent
        # POSTs of different bytes at the same offset all passed their checks, all
        # wrote, and all committed ``received_bytes = offset + len`` off the same stale
        # in-memory row — so five clients held a 200 receipt for bytes that only one of
        # them had actually left on disk.
        #
        # An flock rather than a compare-and-swap on ``received_bytes``, because a CAS
        # can only be lost *after* the write has already clobbered the winner's bytes,
        # and moving it before the write would leave the counter ahead of the durable
        # data — the one direction this design treats as unrecoverable. The lock is an
        # open-file-description lock, so it serialises gthread threads and gunicorn
        # worker processes alike, and it is released by the ``with`` on every path out.
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

        # Re-read the row now the lock is ours: the cursor may have moved while we
        # waited. Only the offset needs re-checking — if it still matches then
        # ``received_bytes`` is unchanged, so the overrun verdict above still holds.
        try:
            db_session.refresh(upload_session)
        except ObjectDeletedError:
            raise SessionUnusable("Upload session was removed while the chunk was in flight")
        ensure_offset_matches(upload_session, offset)

        # Truncate back to the cursor before writing. A chunk that half-landed before
        # the socket died is discarded rather than duplicated, so the file length and
        # the DB counter converge without any repair pass.
        fh.truncate(upload_session.received_bytes)
        fh.seek(upload_session.received_bytes)
        fh.write(data)
        fh.flush()
        # fsync before the counter moves. The whole scheme rests on the counter never
        # running ahead of the durable bytes: behind is self-healing (the next chunk
        # truncates the excess away), ahead is unrecoverable corruption. Buffered
        # writes lost in a crash would put it ahead.
        os.fsync(fh.fileno())

        upload_session.received_bytes += len(data)
        upload_session.updated_at = datetime.utcnow()  # explicit: doubles as TTL keep-alive
        db_session.commit()
        return upload_session.received_bytes


# ── Cleanup ────────────────────────────────────────────────────────────────

_last_sweep_at = 0.0


def sweep_expired_sessions(session, upload_dir, ttl_hours=UPLOAD_SESSION_TTL_HOURS):
    """Delete sessions idle past the TTL along with their partials, returning how many went.

    Also unlinks ``.part`` files older than the TTL that no session row claims. Those
    appear when a row is removed without its file — an album deleted out from under an
    upload, or a crash between the two steps of ``discard_session`` — and nothing else
    would ever reclaim them.
    """
    now = datetime.utcnow()
    removed = 0
    live_names = set()

    for upload_session in session.execute(select(UploadSession)).scalars().all():
        if upload_session.is_expired(ttl_hours, now=now):
            discard_session(session, upload_session, upload_dir)
            removed += 1
        else:
            live_names.add(f"{upload_session.upload_id}.part")

    cutoff = time.time() - ttl_hours * 3600
    for path in partials_dir(upload_dir).glob('*.part'):
        if path.name in live_names:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass  # racing sweep or permissions — the next pass will retry

    return removed


def maybe_sweep_expired_sessions(session, upload_dir, ttl_hours=UPLOAD_SESSION_TTL_HOURS,
                                 min_interval_seconds=300):
    """Run the sweep opportunistically, at most once per interval, returning how many went.

    Safe to call on every ``init``. There is no scheduler in this stack, so cleanup has
    to ride on request traffic; the throttle keeps a burst of inits from each paying for
    a full table scan. The clock is per worker process, which only means a busy
    deployment sweeps a little more often than the interval — harmless, since the sweep
    is idempotent.
    """
    global _last_sweep_at
    now = time.monotonic()
    if now - _last_sweep_at < min_interval_seconds:
        return 0
    _last_sweep_at = now
    return sweep_expired_sessions(session, upload_dir, ttl_hours)
