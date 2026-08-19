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

SECRET_KEY = os.environ.get('SECRET_KEY', os.urandom(32).hex())
SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///pixelvault.db')
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'uploads')
MAX_CONTENT_LENGTH = int(os.environ.get('MAX_UPLOAD_MB', 500)) * 1024 * 1024
SESSION_COOKIE_SECURE = os.environ.get('HTTPS', 'false').lower() == 'true'
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', '').strip()
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '').strip().lower()
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '').strip()
TEMPLATES_FOLDER = pathlib.Path(os.environ.get("FLASK_TEMPLATES_FOLDER", DEFAULT_ROOT/"templates"))
STATIC_FOLDER = pathlib.Path(os.environ.get("FLASK_STATIC_FOLDER", DEFAULT_ROOT/"static"))

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
