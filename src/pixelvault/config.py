import re
import os
import pathlib

DEFAULT_ROOT = pathlib.Path(__file__).parents[2].absolute()

ALLOWED_PHOTO_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/heic'}
ALLOWED_VIDEO_TYPES = {'video/mp4', 'video/quicktime', 'video/x-msvideo', 'video/webm', 'video/mpeg'}
ALLOWED_MIME_TYPES = ALLOWED_PHOTO_TYPES | ALLOWED_VIDEO_TYPES

ALLOWED_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic',
    '.mp4', '.mov', '.avi', '.webm', '.mpg', '.mpeg'
}

RE_USERNAME = re.compile(r'^[a-zA-Z0-9_\-]+$')
RE_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
# The client's per-file fingerprint. Treated as an opaque idempotency token — the
# server never recomputes it, it only checks the shape, so the two sides cannot
# drift apart over hashing details. See docs/upload_protocol.md §7.
RE_CLIENT_KEY = re.compile(r'^[0-9a-f]{64}$')
MAX_PASSWORD_LEN = 1024  # prevent slow-hash DoS via huge passwords

# Whether the Werkzeug debugger is on. Read here as well as in app.py because the
# session-secret check below treats a dev checkout differently from a deployment,
# and it needs to know which one it is at import time.
FLASK_DEBUG = os.environ.get('FLASK_DEBUG', 'false').strip().lower() == 'true'

# The value the operator actually configured, kept separately from the key the
# app ends up signing with. check_secret_key() has to be able to tell "unset"
# from "set to a throwaway we generated ourselves", and once SECRET_KEY carries
# the fallback that distinction is gone.
_SECRET_KEY_ENV = os.environ.get('SECRET_KEY', '')

#: The value .env.example ships. A deployment still running it is signing sessions
#: with a key published in a public repository, which is strictly worse than
#: having no key at all — see check_secret_key().
SECRET_KEY_PLACEHOLDER = 'change-me-to-a-long-random-string'
#: Below this many characters a key is reported as weak, but not refused. The
#: number is `secrets.token_hex(16)`'s output length: shorter than that and the
#: key is almost certainly a typed word rather than something generated.
MIN_SECRET_KEY_LENGTH = 32

# The random fallback survives only for FLASK_DEBUG=true. It is genuinely random,
# so it is not a forgery risk — but it is generated once per *process*, and
# production runs two Gunicorn workers, so the two sign cookies with different
# keys and half of every user's requests log them out with nothing in the logs to
# say why. Outside debug the empty string is left in place deliberately, so
# _validate_secret_key() in __init__.py refuses to boot instead.
SECRET_KEY = _SECRET_KEY_ENV or (os.urandom(32).hex() if FLASK_DEBUG else '')

SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///pixelvault.db')
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'uploads')
MAX_CONTENT_LENGTH = int(os.environ.get('MAX_UPLOAD_MB', 500)) * 1024 * 1024
SESSION_COOKIE_SECURE = os.environ.get('HTTPS', 'false').lower() == 'true'
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', '').strip()
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '').strip().lower()
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '').strip()
TEMPLATES_FOLDER = pathlib.Path(os.environ.get("FLASK_TEMPLATES_FOLDER", DEFAULT_ROOT/"templates"))
STATIC_FOLDER = pathlib.Path(os.environ.get("FLASK_STATIC_FOLDER", DEFAULT_ROOT/"static"))


def check_secret_key(configured=None, debug=None):
    """Return ``[(severity, message)]`` describing what is wrong with ``SECRET_KEY``.

    Pure and parameterised for the same reason as :func:`validate_upload_limits`
    below — the defaults read the module globals at call time, so a test can pass
    a value in rather than re-import the package. The decision about what to *do*
    with each severity belongs to ``_validate_secret_key()`` in ``__init__.py``.

    An ``'error'`` here is fatal, and that is the deliberate difference from
    :func:`validate_upload_limits`, which reports the same shape and never stops
    boot. The upload caps have a degraded mode: uploads over some size get
    refused and everything else works. A session secret has none. Every value
    this function calls an error produces one of:

    * **unset** — the fallback is minted per process, so with ``--workers 2`` the
      two workers sign with different keys and a user is logged out on roughly
      half their requests, with nothing logged anywhere to explain it;
    * **empty** — ``docker compose`` substitutes an unset ``${SECRET_KEY}`` as
      ``''``, and because the variable *is* then set, the fallback never fires.
      Flask raises ``RuntimeError: The session is unavailable...`` on every
      request that writes a session, so login, flashes and invites all 500;
    * **the .env.example placeholder** — a real, working key that is also in a
      public repository. Anyone who has read it can mint a cookie with
      ``_user_id`` set to the admin's and skip authentication entirely.

    The first two are outages an operator cannot diagnose from the symptom; the
    third is a full authentication bypass that produces *no* symptom at all. A
    container that refuses to start names its own cause in one line, which is why
    all three raise even though a crash loop on deploy is a real cost.

    Length is only a warning. A short key is weak, not broken, and a deployment
    that has been running one for a year should be told rather than taken down.
    """
    configured = _SECRET_KEY_ENV if configured is None else configured
    debug = FLASK_DEBUG if debug is None else debug

    generate = ('Generate one with: '
                'python3 -c "import secrets; print(secrets.token_hex(32))"')

    if not configured.strip():
        if debug:
            # A dev checkout must still boot. One process, one key, and the only
            # cost is that restarting logs you out of your own laptop.
            return [(
                'warning',
                'SECRET_KEY is unset; running on a random key generated for this '
                'process because FLASK_DEBUG=true. Sessions will not survive a '
                'restart, and with more than one worker they would not survive at '
                f'all. {generate}'
            )]
        return [(
            'error',
            'SECRET_KEY is unset or empty, and there is no safe default: a key '
            'minted per process means each Gunicorn worker signs sessions with a '
            'different one, and an empty key makes Flask raise on every request '
            'that touches the session. Note that docker compose substitutes an '
            f'unset ${{SECRET_KEY}} as the empty string. {generate}'
        )]

    if configured.strip() == SECRET_KEY_PLACEHOLDER:
        return [(
            'error',
            f'SECRET_KEY is still the .env.example placeholder ({SECRET_KEY_PLACEHOLDER!r}). '
            'That is a working key which is also published in this repository, so '
            'anyone who has read it can forge a session cookie for any account, '
            f'including an admin, without knowing a password. {generate}'
        )]

    if len(configured.strip()) < MIN_SECRET_KEY_LENGTH:
        return [(
            'warning',
            f'SECRET_KEY is {len(configured.strip())} characters; anything under '
            f'{MIN_SECRET_KEY_LENGTH} is short enough to be worth guessing offline, '
            'and a guessed key forges sessions for any account. '
            f'{generate}'
        )]

    return []


# ── Chunked resumable uploads (docs/upload_protocol.md) ────────────────────
# Slice size the client is told to use. Sized to sit well under Cloudflare's
# 100 MB edge body cap while still finishing inside Gunicorn's worker timeout
# on a slow uplink.
UPLOAD_CHUNK_SIZE = int(os.environ.get('UPLOAD_CHUNK_SIZE', 8 * 1024 * 1024))
# How long a partial upload stays resumable before the sweep reclaims its row
# and its .part file.
UPLOAD_SESSION_TTL_HOURS = int(os.environ.get('UPLOAD_SESSION_TTL_HOURS', 24))
# Sub-directory of UPLOAD_FOLDER holding in-progress <upload_id>.part files.
# Kept out of the served media root so nothing half-written is ever reachable.
UPLOAD_PARTIALS_SUBDIR = 'partials'

# Per-user quotas on in-flight uploads. These are the load-bearing defence
# against a client filling the disk: chunking makes MAX_CONTENT_LENGTH a
# per-request bound only, and the rate limiter is per-process and resets on
# every deploy, so neither of them bounds total bytes on disk.
MAX_CONCURRENT_UPLOADS_PER_USER = int(os.environ.get('MAX_CONCURRENT_UPLOADS_PER_USER', 10))
# Deliberately tighter than MAX_CONCURRENT_UPLOADS_PER_USER * MAX_UPLOAD_MB
# (5 GB at the defaults) — that product is the worst case the session count
# alone would permit, and this cap exists to shrink it.
# It must also be at least MAX_UPLOAD_MB, or no file above it can ever be
# uploaded: init accepts the declared total_size against MAX_CONTENT_LENGTH and
# the per-user quota then refuses the same number. validate_upload_limits()
# below reports that pairing at boot.
MAX_INFLIGHT_UPLOAD_BYTES_PER_USER = int(os.environ.get('MAX_INFLIGHT_UPLOAD_MB_PER_USER', 2048)) * 1024 * 1024

# How many files the uploader starts in parallel — CONCURRENCY in
# static/js/uploader.js. Mirrored here only so the boot check can say whether
# the caps can admit a full parallel batch; keep the two values in step.
CLIENT_UPLOAD_CONCURRENCY = 3


def validate_upload_limits(max_file_bytes=None, max_inflight_bytes=None,
                           max_sessions=None, chunk_size=None,
                           concurrency=CLIENT_UPLOAD_CONCURRENCY):
    """Return ``[(severity, message)]`` for upload caps that contradict one another.

    The four caps come from independent env vars that have to agree, and a
    disagreement is invisible in operation: the operator sees uploads refused at
    ``init`` with nothing tying the refusal back to configuration.

    Reported rather than raised. Refusing to boot would turn a degraded upload
    feature into a crash-looping container on every existing deployment that has
    the defaults slightly wrong, and the test suite deliberately inverts the two
    byte caps (``MAX_INFLIGHT`` below ``MAX_UPLOAD``) so a 429 can be told apart
    from a 413 — a contradictory pairing is a legitimate configuration, just an
    unhappy one.

    Defaults read the module globals at call time rather than at definition
    time, so a test can monkeypatch them.
    """
    max_file_bytes = MAX_CONTENT_LENGTH if max_file_bytes is None else max_file_bytes
    max_inflight_bytes = (MAX_INFLIGHT_UPLOAD_BYTES_PER_USER
                          if max_inflight_bytes is None else max_inflight_bytes)
    max_sessions = MAX_CONCURRENT_UPLOADS_PER_USER if max_sessions is None else max_sessions
    chunk_size = UPLOAD_CHUNK_SIZE if chunk_size is None else chunk_size

    def mb(n):
        return n // (1024 * 1024)

    problems = []

    if max_inflight_bytes < max_file_bytes:
        problems.append((
            'error',
            f'MAX_INFLIGHT_UPLOAD_MB_PER_USER ({mb(max_inflight_bytes)}) is below '
            f'MAX_UPLOAD_MB ({mb(max_file_bytes)}): no file larger than '
            f'{mb(max_inflight_bytes)} MB can ever be uploaded, because init accepts '
            f'the declared size and the per-user quota then refuses it. Raise '
            f'MAX_INFLIGHT_UPLOAD_MB_PER_USER to at least '
            f'{mb(concurrency * max_file_bytes)} (= {concurrency} x MAX_UPLOAD_MB), '
            f'or lower MAX_UPLOAD_MB.'))
    elif max_inflight_bytes < concurrency * max_file_bytes:
        problems.append((
            'warning',
            f'MAX_INFLIGHT_UPLOAD_MB_PER_USER ({mb(max_inflight_bytes)}) is below '
            f'{concurrency} x MAX_UPLOAD_MB ({mb(concurrency * max_file_bytes)}): the '
            f'uploader starts {concurrency} files at once and reserves each file\'s '
            f'full declared size, so a batch of large files will have some of its '
            f'members refused at init until the others finish.'))

    if chunk_size > max_file_bytes:
        problems.append((
            'error',
            f'UPLOAD_CHUNK_SIZE ({chunk_size} bytes) exceeds MAX_UPLOAD_MB '
            f'({mb(max_file_bytes)} MB): the client would be told to send chunks '
            f'larger than a request may be, and every chunk would be refused 413.'))

    if max_sessions < 1:
        problems.append((
            'error',
            f'MAX_CONCURRENT_UPLOADS_PER_USER is {max_sessions}: no upload session '
            f'can be opened at all, so every chunked upload fails at init.'))

    return problems

# Number of reverse proxies that append to X-Forwarded-For before a request
# reaches us, consumed by ProxyFix in create_app(). Configurable because the
# real chain (Cloudflare -> VPS nginx -> :5000) depends on an nginx config that
# is still being written, and because a self-hosted deployment behind a
# different topology needs a different value.
#
# Setting this HIGHER than the true hop count is a security hole, not a
# tuning mistake: the client's own X-Forwarded-For header then survives into
# the region ProxyFix trusts, letting a caller name any IP it likes and so
# choose its own rate-limit bucket. When unsure, set it too low.
TRUSTED_PROXY_COUNT = int(os.environ.get('TRUSTED_PROXY_COUNT', 1))

# ── Mail & invites (docs/configuration.md) ─────────────────────────────────
# The canonical external origin invite links are built from, e.g.
# "https://photos.example.com". Deliberately configured rather than derived
# from url_for(_external=True): behind Cloudflare -> nginx -> Gunicorn the
# reconstructed URL is only as trustworthy as the forwarded headers, so an
# attacker-controlled Host on the add-email request could otherwise point an
# invite link at a domain they own. Stored without a trailing slash so callers
# can concatenate a path without guessing.
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', '').strip().rstrip('/')

# false -> NullMailer: invites are still issued and their links are still
# usable via the admin copy-link fallback, but nothing leaves the process.
MAIL_ENABLED = os.environ.get('MAIL_ENABLED', 'true').lower() == 'true'

# Unset -> ConsoleMailer, so a dev checkout boots and prints invite links to
# the log instead of demanding a relay.
SMTP_HOST = os.environ.get('SMTP_HOST', '').strip()
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USERNAME = os.environ.get('SMTP_USERNAME', '').strip()
# Not stripped: a password may legitimately begin or end with a space, and
# Gmail app passwords are usually pasted with the grouping spaces intact.
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
# 'starttls' (587, upgrade in band) | 'ssl' (465, TLS from the first byte) |
# 'none' (plaintext; only defensible for a relay on localhost).
SMTP_SECURITY = os.environ.get('SMTP_SECURITY', 'starttls').strip().lower()

MAIL_FROM = os.environ.get('MAIL_FROM', '').strip()
MAIL_FROM_NAME = os.environ.get('MAIL_FROM_NAME', 'PixelVault').strip()
# Socket timeout for the whole SMTP conversation. Sends happen synchronously
# inside the admin request, and production runs 2 workers x 4 threads — without
# a bound, one hung relay holds an eighth of the server until the OS gives up.
MAIL_TIMEOUT_SECONDS = int(os.environ.get('MAIL_TIMEOUT_SECONDS', 10))

# Address shown to accountless share-link guests so they know who to ask for an
# invitation. Falls back to the sending address, which is nearly always right.
ADMIN_CONTACT = os.environ.get('ADMIN_CONTACT', '').strip() or MAIL_FROM

# Invite link lifetime, measured from issue or rotation. Consumed by invites.py.
INVITE_TTL_HOURS = int(os.environ.get('INVITE_TTL_HOURS', 72))
# Minimum gap between two sends of one invite. Not an anti-annoyance measure:
# an invite is mail this server sends to a third party on request, so an
# unthrottled resend button is a mail-bomb primitive aimed at whatever address
# was typed in — and it burns the sending domain's reputation with the relay.
INVITE_RESEND_COOLDOWN_SECONDS = int(os.environ.get('INVITE_RESEND_COOLDOWN_SECONDS', 60))
