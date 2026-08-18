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
MAX_INFLIGHT_UPLOAD_BYTES_PER_USER = int(os.environ.get('MAX_INFLIGHT_UPLOAD_MB_PER_USER', 2048)) * 1024 * 1024

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
