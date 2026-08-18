"""Reclaiming what abandoned uploads leave behind.

A partial upload is a row plus a ``.part`` file, and the client that owns them can
simply close its laptop. Nothing in this stack schedules anything, so the sweep
rides on ``init`` traffic and throttles itself. These tests drive
``sweep_expired_sessions`` directly where the property is about the sweep, and go
through ``init`` where the property is about the throttle.

Time is moved by rewriting timestamps — ``updated_at`` on the row, ``st_mtime`` on
the file — never by sleeping. The two clocks are separate on purpose: the row is
aged against ``datetime.utcnow()`` and the orphan file against ``time.time()``, so
a test that only moved one of them would prove half the sweep.
"""

import os
import time

import pytest

from tests.conftest import TEST_CHUNK_SIZE, TEST_MAX_SESSIONS, TEST_TTL_HOURS


def _sweep(app):
    """Run the sweep synchronously, returning how many things it reclaimed."""
    from pixelvault.extensions import db
    from pixelvault.uploads import sweep_expired_sessions
    with app.app_context():
        return sweep_expired_sessions(db.session, app.config["UPLOAD_FOLDER"],
                                      TEST_TTL_HOURS)


def _arm_opportunistic_sweep(monkeypatch):
    """Let the next ``init`` actually sweep.

    ``reset_state`` pins the module's clock to +inf so no unrelated init reclaims a
    session a test is holding. Winding it back to 0 is what makes ``init`` run the
    sweep, and monkeypatch puts it back afterwards.
    """
    import pixelvault.uploads as uploads_module
    monkeypatch.setattr(uploads_module, "_last_sweep_at", 0.0)


def _age_file(path, hours):
    """Backdate a file's mtime, which is what the orphan branch of the sweep reads."""
    past = time.time() - hours * 3600
    os.utime(path, (past, past))


def _started(protocol, data, chunks_to_send=2):
    init = protocol.init_for("clip.jpg", data).get_json()
    stop = chunks_to_send * TEST_CHUNK_SIZE
    protocol.send_chunks(init["upload_id"], data, TEST_CHUNK_SIZE, stop=stop)
    return init["upload_id"], stop


# ── The sweep itself ───────────────────────────────────────────────────────

def test_the_sweep_reclaims_an_expired_session_and_its_partial(
        app, protocol, multi_chunk_jpeg, age_session, session_row, partials_dir):
    upload_id, _ = _started(protocol, multi_chunk_jpeg)
    age_session(upload_id)

    removed = _sweep(app)

    assert removed == 1
    assert session_row(upload_id) is None
    assert not (partials_dir / f"{upload_id}.part").exists()


def test_the_sweep_leaves_a_live_session_alone(app, protocol, multi_chunk_jpeg,
                                               session_row, partials_dir):
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)

    removed = _sweep(app)

    assert removed == 0
    assert session_row(upload_id).received_bytes == cursor
    assert (partials_dir / f"{upload_id}.part").exists()


def test_the_sweep_reclaims_only_the_expired_session_of_several(
        app, protocol, small_jpeg, age_session, session_row):
    stale = protocol.init("stale.jpg", 4096).get_json()["upload_id"]
    live = protocol.init("live.jpg", 4096).get_json()["upload_id"]
    age_session(stale)

    removed = _sweep(app)

    assert removed == 1
    assert session_row(stale) is None
    assert session_row(live) is not None


def test_a_session_one_hour_short_of_the_ttl_survives(app, protocol, multi_chunk_jpeg,
                                                      age_session, session_row):
    """The boundary is measured from the last chunk, so a slow client is not evicted."""
    upload_id, _ = _started(protocol, multi_chunk_jpeg)
    age_session(upload_id, hours=TEST_TTL_HOURS - 1)

    _sweep(app)

    assert session_row(upload_id) is not None


def test_a_chunk_pushes_the_expiry_out(app, protocol, multi_chunk_jpeg, age_session,
                                       session_row):
    """``updated_at`` doubles as the keep-alive, so a transfer that is still making
    progress can outlive the TTL many times over."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    age_session(upload_id, hours=TEST_TTL_HOURS - 1)   # one hour left
    nearly_due = protocol.status(upload_id).get_json()["expires_at"]

    protocol.chunk(upload_id, cursor, multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE])
    renewed = protocol.status(upload_id).get_json()["expires_at"]

    assert renewed > nearly_due          # ISO-8601 Z strings sort chronologically
    assert _sweep(app) == 0
    assert session_row(upload_id) is not None


def test_an_already_expired_session_cannot_be_kept_alive_by_a_late_chunk(
        protocol, multi_chunk_jpeg, age_session, session_row):
    """Expiry is decided when the request arrives, before the append is considered —
    otherwise a client that vanished for a week could resurrect its partial."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    age_session(upload_id)

    response = protocol.chunk(upload_id, cursor,
                              multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE])

    assert response.status_code == 404
    assert session_row(upload_id) is None


# ── Orphaned partials ──────────────────────────────────────────────────────

def test_the_sweep_collects_an_old_part_file_no_session_claims(app, partials_dir):
    """A row can go without its file — an album deleted mid-upload, or a crash between
    the two steps of ``discard_session``. Nothing else would ever reclaim it."""
    partials_dir.mkdir(parents=True, exist_ok=True)
    orphan = partials_dir / "deadbeef-0000-0000-0000-000000000000.part"
    orphan.write_bytes(b"abandoned")
    _age_file(orphan, TEST_TTL_HOURS + 1)

    removed = _sweep(app)

    assert removed == 1
    assert not orphan.exists()


def test_the_sweep_leaves_a_recent_orphan_alone(app, partials_dir):
    """A file younger than the TTL may belong to a session mid-init; deleting it would
    race the row that is about to claim it."""
    partials_dir.mkdir(parents=True, exist_ok=True)
    orphan = partials_dir / "deadbeef-1111-1111-1111-111111111111.part"
    orphan.write_bytes(b"just created")

    removed = _sweep(app)

    assert removed == 0
    assert orphan.exists()


def test_the_sweep_never_collects_the_partial_of_a_live_session(
        app, protocol, multi_chunk_jpeg, partials_dir):
    """Even an old-looking file is safe while a live row names it — the row is the
    authority, not the mtime."""
    upload_id, _ = _started(protocol, multi_chunk_jpeg)
    _age_file(partials_dir / f"{upload_id}.part", TEST_TTL_HOURS + 1)

    removed = _sweep(app)

    assert removed == 0
    assert (partials_dir / f"{upload_id}.part").exists()


def test_the_sweep_ignores_files_that_are_not_partials(app, partials_dir):
    partials_dir.mkdir(parents=True, exist_ok=True)
    stray = partials_dir / "notes.txt"
    stray.write_bytes(b"not ours")
    _age_file(stray, TEST_TTL_HOURS + 10)

    _sweep(app)

    assert stray.exists()


# ── Expiry seen from the wire ──────────────────────────────────────────────

def test_an_expired_session_is_404_and_is_dropped_on_sight(
        protocol, multi_chunk_jpeg, age_session, session_row, partials_dir):
    """The request path does not wait for the sweep — unknown, expired and completed
    are one case to the client, so the route reclaims as it answers."""
    upload_id, _ = _started(protocol, multi_chunk_jpeg)
    age_session(upload_id)

    response = protocol.status(upload_id)

    assert response.status_code == 404
    assert session_row(upload_id) is None
    assert not (partials_dir / f"{upload_id}.part").exists()


def test_completing_an_expired_session_is_404_rather_than_a_late_commit(
        protocol, small_jpeg, age_session, album, photos):
    init = protocol.init_for("holiday.jpg", small_jpeg).get_json()
    protocol.chunk(init["upload_id"], 0, small_jpeg)
    age_session(init["upload_id"])

    response = protocol.complete(init["upload_id"])

    assert response.status_code == 404
    assert photos(album.id) == []


def test_reinit_after_expiry_opens_a_fresh_session_at_zero(protocol, multi_chunk_jpeg,
                                                           age_session):
    """The client key is the same file; the offset behind it is worthless now."""
    upload_id, _ = _started(protocol, multi_chunk_jpeg)
    age_session(upload_id)

    response = protocol.init_for("clip.jpg", multi_chunk_jpeg)

    assert response.status_code == 201
    assert response.get_json()["resumed"] is False
    assert response.get_json()["received_bytes"] == 0
    assert response.get_json()["upload_id"] != upload_id


# ── The opportunistic sweep on init ────────────────────────────────────────

def test_init_sweeps_expired_sessions_belonging_to_other_uploads(
        protocol, multi_chunk_jpeg, small_jpeg, age_session, session_row, monkeypatch):
    """The sweep is the only cleanup there is, so it has to reach rows the current
    request would never otherwise look at."""
    stale, _ = _started(protocol, multi_chunk_jpeg)
    age_session(stale)
    _arm_opportunistic_sweep(monkeypatch)

    protocol.init_for("holiday.jpg", small_jpeg)

    assert session_row(stale) is None


def test_init_does_not_sweep_again_inside_the_throttle_interval(
        protocol, multi_chunk_jpeg, small_jpeg, age_session, session_row, monkeypatch):
    """A burst of inits must not each pay for a full table scan."""
    _arm_opportunistic_sweep(monkeypatch)
    protocol.init("first.jpg", 4096)          # consumes the interval

    stale, _ = _started(protocol, multi_chunk_jpeg)
    age_session(stale)
    protocol.init_for("holiday.jpg", small_jpeg)

    assert session_row(stale) is not None


def test_the_swept_slot_is_available_to_a_new_upload(protocol, age_session, monkeypatch):
    """Reclaiming has to give the quota back, or a user who abandons uploads is locked
    out for a whole TTL."""
    ids = [protocol.init(f"file{i}.jpg", 4096 + i).get_json()["upload_id"]
           for i in range(TEST_MAX_SESSIONS)]
    assert protocol.init("blocked.jpg", 4096).status_code == 429
    for upload_id in ids:
        age_session(upload_id)

    _arm_opportunistic_sweep(monkeypatch)
    response = protocol.init("after-sweep.jpg", 4096)

    assert response.status_code == 201


def test_the_swept_bytes_are_given_back_to_the_in_flight_quota(protocol, age_session,
                                                               monkeypatch):
    MB = 1024 * 1024
    stale = protocol.init("big-a.jpg", 3 * MB).get_json()["upload_id"]
    assert protocol.init("big-b.jpg", 3 * MB).status_code == 429
    age_session(stale)

    _arm_opportunistic_sweep(monkeypatch)
    response = protocol.init("big-c.jpg", 3 * MB)

    assert response.status_code == 201


def test_sweeping_is_idempotent(app, protocol, multi_chunk_jpeg, age_session):
    """It runs on request traffic across several workers; a second pass must be a no-op."""
    upload_id, _ = _started(protocol, multi_chunk_jpeg)
    age_session(upload_id)

    first = _sweep(app)
    second = _sweep(app)

    assert (first, second) == (1, 0)


def test_the_cleanup_cli_runs_the_same_sweep(app, protocol, multi_chunk_jpeg,
                                             age_session, session_row):
    """`flask cleanup-uploads` is the operator's manual handle on the same code path."""
    upload_id, _ = _started(protocol, multi_chunk_jpeg)
    age_session(upload_id)

    result = app.test_cli_runner().invoke(args=["cleanup-uploads"])

    assert result.exit_code == 0
    assert "Reclaimed 1" in result.output
    assert session_row(upload_id) is None


@pytest.mark.parametrize("chunks_sent", [0, 1, 3])
def test_expiry_reclaims_a_session_at_any_point_in_its_life(
        app, protocol, multi_chunk_jpeg, age_session, session_row, partials_dir,
        chunks_sent):
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    upload_id = init["upload_id"]
    if chunks_sent:
        protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE,
                             stop=chunks_sent * TEST_CHUNK_SIZE)
    age_session(upload_id)

    _sweep(app)

    assert session_row(upload_id) is None
    assert list(partials_dir.glob("*.part")) == []


# ── Sessions orphaned by an album that goes away ───────────────────────────

def test_deleting_an_album_does_not_strand_the_uploaders_quota(
        app, client, protocol, album, multi_chunk_jpeg):
    """A session outliving its album is dead but not free: every chunk 404s because
    the share token no longer resolves, while the row goes on charging a concurrency
    slot and its whole declared size against the byte quota for the rest of the TTL."""
    from pixelvault.extensions import db
    from pixelvault.models import UploadSession

    _started(protocol, multi_chunk_jpeg)

    client.post(f"/album/{album.token}/delete")

    with app.app_context():
        assert db.session.query(UploadSession).count() == 0


def test_deleting_an_album_takes_the_partial_files_with_it(
        app, client, protocol, album, multi_chunk_jpeg, partials_dir):
    """The row and its bytes go together, or the disk leaks for a day either way."""
    _started(protocol, multi_chunk_jpeg)
    assert list(partials_dir.glob("*.part")) != []

    client.post(f"/album/{album.token}/delete")

    assert list(partials_dir.glob("*.part")) == []


def test_a_session_orphaned_by_a_deleted_album_is_still_reclaimed_by_the_sweep(
        app, protocol, album, multi_chunk_jpeg, session_row, partials_dir,
        age_session):
    """The backstop. The delete route now cascades, but it is two steps — rows, then
    files — and a crash between them, or any album removed by a route that does not
    know about uploads, still strands a session. The album row is dropped directly
    here to reproduce exactly that state; the TTL sweep is what collects it, which is
    why its orphan branch exists at all."""
    from pixelvault.extensions import db
    from pixelvault.models import Album

    upload_id, _ = _started(protocol, multi_chunk_jpeg)
    with app.app_context():
        db.session.delete(db.session.query(Album).filter_by(id=album.id).one())
        db.session.commit()
    age_session(upload_id)

    _sweep(app)

    assert session_row(upload_id) is None
    assert list(partials_dir.glob("*.part")) == []


# ── Sessions left behind by a failed commit ────────────────────────────────

def test_a_complete_that_blows_up_in_storage_does_not_strand_the_session(
        app, protocol, multi_chunk_jpeg, session_row, partials_dir, monkeypatch):
    """``complete`` used to return the error and keep everything.

    The session then held a concurrency slot and its full declared size against the
    byte quota until the TTL, the ``.part`` held the disk, and the whole conversion
    was replayable at the complete endpoint's own limit. The validation branch beside
    it already discarded; this is the same answer for the same reason.
    """
    import pixelvault.routes.share as share_module

    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    upload_id = init["upload_id"]
    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE)

    def _explode(*args, **kwargs):
        raise RuntimeError("thumbnailer fell over")
    monkeypatch.setattr(share_module, "store_upload", _explode)

    response = protocol.complete(upload_id)

    assert response.status_code == 200
    assert "error" in response.get_json()["results"][0]
    assert session_row(upload_id) is None
    assert list(partials_dir.glob("*.part")) == []


def test_the_slot_a_failed_complete_held_is_given_back(
        app, protocol, multi_chunk_jpeg, monkeypatch):
    """The point of discarding: the user can start something else afterwards."""
    import pixelvault.routes.share as share_module

    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    protocol.send_chunks(init["upload_id"], multi_chunk_jpeg, TEST_CHUNK_SIZE)
    for i in range(TEST_MAX_SESSIONS - 1):
        protocol.init(f"filler{i}.jpg", 4096)

    monkeypatch.setattr(share_module, "store_upload",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    protocol.complete(init["upload_id"])

    assert protocol.init("next.jpg", 4096).status_code == 201
