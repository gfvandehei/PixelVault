# PixelVault — Claude Onboarding Guide

## Project Overview

PixelVault is an invite-only, self-hosted photo album sharing application. Users upload photos/videos into named albums, then share albums via two link types: an upload link (guests can browse & upload) and a view-only link. Built with Flask + SQLite + Nginx, packaged for Docker deployment.

Accounts exist only by invitation: an admin adds an address, the app emails a single-use link, and following that link is the only way to register. There is no public sign-up page.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Flask 3.1+, SQLAlchemy ORM, Flask-Login, Flask-Limiter |
| Database | SQLite (via SQLAlchemy) |
| Auth | Flask-Login + PBKDF2-SHA256 (600k rounds), Flask-WTF CSRF |
| CSRF | Flask-WTF `CSRFProtect` — app-wide, no forms library involved |
| Image Processing | Pillow, pillow-heif (HEIC support) |
| File Validation | python-magic (MIME type verification) |
| Frontend | Jinja2 templates + vanilla JS |
| Production Server | Gunicorn (gthread, 2 workers x 4 threads) behind Nginx, behind Cloudflare |
| Containerization | Docker + Docker Compose |

---

## Project Structure

```
pixelvault/
├── app.py                          # WSGI entry point (Gunicorn target)
├── pyproject.toml                  # Project metadata & dependencies
├── requirements.txt                # pip dependencies
├── .env.example                    # Config template (copy to .env)
├── .env.prod                       # Production config template
├── src/pixelvault/
│   ├── __init__.py                 # App factory (create_app), migrations, CLI, error handlers
│   ├── config.py                   # Env var loading & validation
│   ├── extensions.py               # db, login_manager, limiter, mailer — initialized separately to avoid circular imports
│   ├── models.py                   # SQLAlchemy models: User, Album, Photo, AllowedEmail
│   ├── utils.py                    # File handling, ZIP building, shared decorators
│   ├── uploads.py                  # Chunked upload sessions: quotas, chunk append, TTL sweep
│   ├── mailer.py                   # SMTP transport only — knows nothing about invites
│   ├── emails.py                   # Message content: renders the invite email from templates
│   ├── invites.py                  # Invite lifecycle: issue, rotate, validate, consume
│   └── routes/
│       ├── __init__.py             # Registers all route blueprints via register(app)
│       ├── auth.py                 # /login, /logout, /invite/* (registration is invite-only)
│       ├── albums.py               # /dashboard, /album/* (CRUD, settings, ZIP download)
│       ├── share.py                # /share/<token> and /view/<view_token> (public links)
│       ├── media.py                # /media/<filename> (authenticated file serving)
│       ├── api.py                  # /api/* (JSON photo lists for gallery JS)
│       └── admin.py                # /admin (invites, user list, album list)
├── templates/                      # Jinja2 HTML (base.html + 8 pages)
│   └── email/                      # invite.txt / invite.html — the invitation's two parts
├── docs/
│   ├── configuration.md            # Every environment variable, why it exists, how to set it
│   ├── database_schema.md          # Table/column reference
│   ├── registration_invites.md     # Operator guide: inviting, resending, troubleshooting mail
│   ├── invite_registration_design.md  # Architectural design for the invite system (#7)
│   ├── upload_protocol.md          # Chunked upload wire contract (client <-> server)
│   ├── upload_client.md            # Client-side uploader internals
│   └── upload_operations.md        # Deployment, per-hop limits, troubleshooting, tuning
├── docker/
│   ├── Dockerfile.prod             # Production image (Python 3.13-slim)
│   ├── Dockerfile.dev              # Dev image with hot reload
│   ├── prod.docker-compose.yml     # App + Nginx
│   └── test.docker-compose.yml     # Test container (port 5050, pre-seeded admin)
├── .github/
│   ├── workflows/dependency-audit.yml  # uv lock --check, pip-audit, floor check
│   ├── scripts/check_floors.py     # requirements.txt floors must not sit below uv.lock
│   └── dependabot.yml              # uv + docker + github-actions
├── scripts/
│   ├── create_admin.py             # Standalone admin creation (no Flask CLI needed)
│   ├── migrate_heic.py             # Convert stored HEIC files to JPEG
│   └── migrate_thumbnails_orientation.py  # Fix EXIF orientation on thumbnails
└── instance/                       # Runtime: pixelvault.db created here
```

---

## Running Locally

```bash
# System deps (Ubuntu/Debian)
sudo apt-get install libmagic1 libmagic-dev libjpeg-dev libpng-dev libwebp-dev

# Python env
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Set SECRET_KEY: python3 -c "import secrets; print(secrets.token_hex(32))"

# Create admin
python scripts/create_admin.py --env .env --user admin --email admin@example.com --password yourpassword
# OR via Flask CLI:
export ADMIN_USERNAME=admin ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD=yourpassword
flask --app app create-admin

# Run
python app.py  # http://localhost:5000
```

---

## Running with Docker

**Test/dev (port 5050, pre-seeded admin: admin@test.com / password):**
```bash
export PIXELVAULT_ROOT=/path/to/pixelvault
export DATA_DIRECTORY=/path/to/data
export UPLOAD_FOLDER=/path/to/uploads
docker compose -f ./docker/test.docker-compose.yml up
```

The test container has no SMTP relay, so invitations are printed to the log by `ConsoleMailer`.
To exercise registration: log in as the seeded admin, add an address in `/admin`, then copy the
invite link out of `docker compose logs` and open it.

**Production:**
```bash
# 1. Get SSL certs
certbot certonly --standalone -d your-domain.com
mkdir certs && cp /etc/letsencrypt/live/your-domain.com/{fullchain,privkey}.pem certs/

# 2. Configure .env.prod (set SECRET_KEY, paths, HTTPS=true)

# 3. Deploy
docker compose -f ./docker/prod.docker-compose.yml --env-file .env.prod up -d --build
```

---

## Environment Variables

| Variable | Default | Required | Description |
|---|---|---|---|
| `SECRET_KEY` | none | **YES** | Flask session secret. The app **refuses to boot** without one, on the `.env.example` placeholder, or on the empty string Compose substitutes for an unset `${SECRET_KEY}`. A random per-process fallback survives only under `FLASK_DEBUG=true`, because two Gunicorn workers with two different keys log users out at random |
| `HTTPS` | `false` | no | Set `true` in production (enables Secure cookies + HSTS) |
| `UPLOAD_FOLDER` | `./uploads` | no | Absolute path for uploaded files |
| `DATABASE_URL` | `sqlite:///pixelvault.db` | no | SQLAlchemy URI (4 slashes for absolute paths) |
| `MAX_UPLOAD_MB` | `500` | no | Max upload size per request |
| `FLASK_DEBUG` | `false` | no | Never `true` in production |
| `PORT` | `5000` | no | Server port |
| `ADMIN_USERNAME/EMAIL/PASSWORD` | — | no | Used only by `create-admin` CLI command |
| `DATA_DIRECTORY` | — | no | Instance directory for database |
| `UPLOAD_CHUNK_SIZE` | `8388608` (8 MiB) | no | Chunk size in bytes handed to the client at `init`. Must stay well under Cloudflare's 100 MB edge body cap |
| `UPLOAD_SESSION_TTL_HOURS` | `24` | no | How long a partial upload stays resumable before the sweep reclaims its row and `.part` file |
| `MAX_CONCURRENT_UPLOADS_PER_USER` | `10` | no | Open upload sessions one user may hold at once; enforced at `init` |
| `MAX_INFLIGHT_UPLOAD_MB_PER_USER` | `2048` | no | Total declared bytes across a user's open sessions, in MB. Read into `MAX_INFLIGHT_UPLOAD_BYTES_PER_USER`. **Must be ≥ `MAX_UPLOAD_MB`**, ideally 3× it (the client uploads 3 files in parallel and reserves each declared size in full) — otherwise no large file can ever be uploaded. `validate_upload_limits()` logs the contradiction at boot |
| `TRUSTED_PROXY_COUNT` | `1` | no | Proxies appending to `X-Forwarded-For` ahead of Flask; consumed by `ProxyFix`. **Setting it too high lets clients spoof their rate-limit identity** — when unsure, set it too low |
| `PUBLIC_BASE_URL` | — | **once SMTP is set** | Canonical external origin invite links are built from, e.g. `https://photos.example.com`. Configured rather than derived from the request, so a spoofed `Host` cannot point an invite at another domain |
| `MAIL_ENABLED` | `true` | no | `false` → `NullMailer`; invites are still issued and still usable via the admin copy-link button |
| `SMTP_HOST` | — | no | Unset → `ConsoleMailer`, which prints the whole invitation to the log. That is the dev default |
| `SMTP_PORT` | `587` | no | 587 pairs with `starttls`, 465 with `ssl` |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | — | no | Blank = unauthenticated relay. The password is **not** stripped — Gmail app passwords are pasted with spaces |
| `SMTP_SECURITY` | `starttls` | no | `starttls` \| `ssl` \| `none` |
| `MAIL_FROM` | — | **once SMTP is set** | Envelope and header sender. With Gmail this must be the `SMTP_USERNAME` account |
| `MAIL_FROM_NAME` | `PixelVault` | no | Display name |
| `MAIL_TIMEOUT_SECONDS` | `10` | no | Socket timeout. Sends are synchronous inside the admin request, so this bounds how long one of 8 threads is held by a dead relay |
| `ADMIN_CONTACT` | falls back to `MAIL_FROM` | no | Address shown to accountless share-link guests on `request_permission.html` |
| `INVITE_TTL_HOURS` | `72` | no | Invite link lifetime, measured from issue or rotation |
| `INVITE_RESEND_COOLDOWN_SECONDS` | `60` | no | Minimum gap between two sends of one invite. Not politeness: an unthrottled resend button is a mail-bomb primitive |

The upload five are documented in depth in [docs/upload_operations.md](docs/upload_operations.md);
**every** variable, with its reasoning, is in [docs/configuration.md](docs/configuration.md).

---

## Architecture & Key Patterns

### App Factory
`create_app()` in [src/pixelvault/__init__.py](src/pixelvault/__init__.py) creates and configures the Flask app. Extensions (db, login_manager, limiter) live in [extensions.py](src/pixelvault/extensions.py) and are initialized there to avoid circular imports.

### Route Organization
Each route domain has a `register(app)` function. All are wired together in [routes/__init__.py](src/pixelvault/routes/__init__.py). When adding a new feature, add a new route file and call `register(app)` there.

### Database Migrations
No Alembic — migrations are handled by `_run_migrations()` in `__init__.py`. It runs `ALTER TABLE ... ADD COLUMN` inside try/except to handle "column already exists" gracefully. Use this pattern for any schema additions.

### File Handling
- Uploaded files are **always** stored with UUID filenames — never user-provided names on disk.
- Validation uses **both** extension check and python-magic MIME byte verification.
- HEIC files are converted to JPEG on upload; EXIF orientation is preserved.
- Thumbnails are 400×400 JPEG (quality 85), auto-generated at upload time.
- Files are served through authenticated Flask endpoints (`/media/<filename>`), never as static files directly.

### Chunked Resumable Uploads
Files above `UPLOAD_CHUNK_SIZE` (8 MiB) upload as a sequence of chunk requests rather than one
large body, because Cloudflare's free plan rejects request bodies over 100 MB at the edge — the
origin never sees the request, so the browser observes a silent stall with nothing in the app logs.
Chunks append in place to `UPLOAD_FOLDER/partials/<upload_id>.part`; the transfer is resumable
within `UPLOAD_SESSION_TTL_HOURS`. Files at or below the threshold keep the legacy single-request
path unchanged.

- Wire contract (endpoints, status codes, headers): [docs/upload_protocol.md](docs/upload_protocol.md)
- Client internals: [docs/upload_client.md](docs/upload_client.md)
- Deployment, per-hop limits, troubleshooting, tuning: [docs/upload_operations.md](docs/upload_operations.md)

Server-side session logic lives in [src/pixelvault/uploads.py](src/pixelvault/uploads.py), separate
from the routes. The `upload_session` table is created by `_run_migrations()` on boot — no manual
step. Abandoned partials are swept opportunistically inside `init` and on demand via
`flask cleanup-uploads`.

### Invite-Based Registration
There is no public sign-up. `/register` does not exist; an account can only be created by
following an emailed, single-use link. Adding an address in `/admin` mints a token, commits the
row, and *then* sends — so a relay outage leaves a resendable invite rather than nothing.

`AllowedEmail` is no longer a passive whitelist: one row is both the assertion that an address
may register and the credential that carries that permission, so it has a lifecycle
(`state` is a derived property, never a column — see [docs/database_schema.md](docs/database_schema.md)).

Three modules, three concerns, deliberately not one:
- [mailer.py](src/pixelvault/mailer.py) — transport. Has never heard of invites, so the next
  feature needing email is free and a relay migration is one config change.
- [emails.py](src/pixelvault/emails.py) — content. Renders the invitation's text and HTML parts.
- [invites.py](src/pixelvault/invites.py) — lifecycle. No Flask imports; testable without a client.

Load-bearing rules, in decreasing order of how badly breaking them hurts:
- **The email address comes from the invite row, never from the form.** The acceptance form's
  email field has no `name` attribute and `invites.consume` reads the address off the row.
  Otherwise the holder of a link for one address could register as another and the whitelist
  means nothing.
- **Tokens are hashed at rest** (SHA-256 of `secrets.token_urlsafe(32)`). The plaintext exists
  only in the sent email and one flash. A link can therefore never be re-shown — renewal
  *mints*, which is why resend and copy-link both kill the previous link.
- **`rotate` is the only renewal path.** Resend, copy-link, and *Send invite* on a legacy row
  all call it; `issue` is strictly for an address never seen before.
- **`mark_sent` is owned by `emails.send_invite`**, called on both outcomes. Routes never call
  it, or `send_count` double-counts.
- **The token leaves the URL immediately.** `/invite/<token>` validates, stashes the token in
  the signed session, and redirects to `/invite` — keeping a bearer credential out of nginx
  access logs and browser history.

Operator guide (setup, the panel's buttons, mail troubleshooting):
[docs/registration_invites.md](docs/registration_invites.md). Design and rationale:
[docs/invite_registration_design.md](docs/invite_registration_design.md).

### Share Link System
Each `Album` has two tokens:
- `token` → `/share/<token>` — upload link (guests can browse & upload)
- `view_token` → `/view/<view_token>` — view-only link (browse only)

**A token names an album; it does not authorize anything.** Opening the album page while
signed in *mints* an `AlbumAccess` grant whose `access_type` records which of the two links
was followed, and every later request — media, photo index, ZIP, upload — is judged against
that grant by `may_read_album` / `may_upload_to` in
[routes/share.py](src/pixelvault/routes/share.py). The other route modules import those two
helpers rather than re-deriving the rule.

That indirection is the fix for a family of bugs where the token *was* the capability: a
view-only recipient could read the upload token out of their own (signed, but not encrypted)
session cookie and upload with it; an owner downgrading a guest to view-only revoked nothing,
because no upload endpoint consulted `access_type`; and an unauthenticated caller holding one
media URL was promoted into album-wide access by the media route itself. Nothing writes a
capability into the session now. Following the upload link also cannot *upgrade* an existing
`view` grant, or a downgraded guest could simply re-grant themselves.

Both links still respect the album-level `allow_upload` toggle, which is checked before the
per-guest grant so a closed album says so rather than implying the guest lacks access.

### CSRF Protection
`CSRFProtect(app)` in [__init__.py](src/pixelvault/__init__.py) guards **every** POST, PUT,
PATCH and DELETE in the app. It is a `before_request` hook, not a per-view decorator, and
that is the whole design: a route added next year is protected because nobody had to
remember anything. `tests/test_csrf.py` asserts that of the live `url_map`, so the day
someone reaches for `@csrf.exempt` a test fails instead of a hole opening quietly.

Nothing here uses WTForms. Flask-WTF is pulled in for `CSRFProtect` alone — forms stay
hand-written Jinja, and `csrf_token()` is the only thing the templates gain.

Four kinds of caller, three ways to carry the token:

| Caller | How the token travels |
|---|---|
| Browser form POST | `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">` |
| In-page `fetch` (delete photo) | `X-CSRFToken` header, read from the meta tag |
| Chunk upload (`application/octet-stream`) | `X-CSRFToken` header — the body is raw bytes, so there is no field to use |
| Fire-and-forget `DELETE` (`upload/cancel`) | `X-CSRFToken` header |

Load-bearing details:

- **The token is rendered once per page** as `<meta name="csrf-token">` in
  [base.html](templates/base.html), and `uploader.js` re-reads that tag *per request*
  rather than capturing it at construction. An uploader built at page load may still be
  running an hour later.
- **`WTF_CSRF_TIME_LIMIT = None`.** Flask-WTF's one-hour default is shorter than a single
  legitimate action here — a 470 MB video is ~59 requests — and a token expiring mid-file
  fails chunks with a `400` indistinguishable from corruption. The token still dies with
  the session, so this swaps a second weaker clock for the session's own rather than
  removing a clock.
- **A rejected token answers in the caller's dialect**, JSON for XHR and the error page for
  a browser, for the same reason `handle_unauthorized` does: uploader.js reads `body.error`,
  and an HTML page reaches it as the useless "Upload failed (HTTP 400)".
- **`400`, not `403`.** A `403` from an upload endpoint is already spoken for — uploader.js
  reports it as "Uploads are disabled for this album".
- The wire contract for the upload endpoints is
  [docs/upload_protocol.md §6.0](docs/upload_protocol.md); `tests/protocol.py` mirrors it.

### Rate Limits (Flask-Limiter)
| Endpoint | Limit |
|---|---|
| Login | 20/hour |
| Invite link click (`GET /invite/<token>`) | 60/hour (per IP) |
| Invite form (`GET /invite`) | 60/hour (per IP) |
| Invite submit (`POST /invite`) | 20/hour (per IP) |
| Upload (legacy single-request) | 600/hour |
| Album create | 30/hour |
| Album ZIP download (owner / share / view — one bucket each) | 10/hour |
| Admin email add (issues + sends an invite) | 60/hour |
| Admin invite resend | 30/hour |
| Admin invite copy-link | 30/hour |
| Chunked `init` | 120/hour |
| Chunked `status` | 300/hour |
| Chunked `chunk` | 600/hour, **charged only on non-200** |
| Chunked `complete` | 600/hour |
| Chunked `cancel` | 120/hour |

The chunk endpoint's budget is sized for *failures*, not traffic: a legitimate 500 MB
upload is ~63 chunks and spends nothing, while every refusal (409/422/413/400/404) is
charged so no status is a free 8 MiB sink. It has to be that generous because
Flask-Limiter checks the limit on entry even when it does not deduct — a small budget
spent on failures would start rejecting the good chunks too.

The album-download budget is sized the other way round — for *cost per call*, not for
volume. One call reads the whole album off the media volume, DEFLATEs it, stages the
archive back onto that same volume and streams it out, so ten an hour is already more
archives than any human clicking a link produces, while leaving room for the
phone-on-hotel-wifi case where a multi-gigabyte transfer dies and gets retried.
`rate_limit_key` resolves an authenticated caller to `user:<id>`, so this is a real
per-account budget rather than a bucket shared by everyone behind one NAT. The number
lives in `ALBUM_ZIP_RATE_LIMIT` in [utils.py](src/pixelvault/utils.py), next to the
cost it bounds.

**A CSRF rejection is the one refusal that is *not* charged.** It aborts before the
limiter's deferred deduction sees a response, and that is the right way round twice over:
it never reads the 8 MiB body the budget protects, so it is cheaper than the `409` beside
it; and its likeliest cause is a page left open across a logout, where charging would keep
the user locked out for an hour *after* the reload that fixed the problem. A refusal whose
remedy is "reload" must not outlive the reload. Pinned by `tests/test_csrf.py`, because an
extension upgrade could reverse it silently.

---

## Security Notes

- **Every state-changing request carries a CSRF token** — `CSRFProtect` checks every POST,
  PUT, PATCH and DELETE in a `before_request` hook, so a new route is protected by default
  rather than by remembering a decorator. Forms carry a hidden `csrf_token` field; the XHR
  and `fetch` callers send `X-CSRFToken`. Before this, `SESSION_COOKIE_SAMESITE = 'Lax'` was
  the only thing between a cross-site POST and album deletion — a single point of failure,
  and one already defeated by a `remember_token` cookie that authenticates on its own.
  `SameSite` remains as defence in depth; it is no longer the defence.
- The check runs **before** `@login_required` and before the view, so a forged request never
  reaches a database session — the album, the photo and the invite are still there
  afterwards, which is what `tests/test_csrf.py` asserts rather than merely asserting a 400.
- Registration is **invite-only and link-only**: there is no `/register`. An admin issues an
  invite, the app emails a single-use TTL-bounded link, and accepting it is the only way an
  account is created.
- The registering email address is read from the invite row, never from the submitted form —
  the single highest-value line in the feature.
- Invite tokens are **hashed at rest**, so neither a database backup nor a screenshot of the
  admin panel holds a working account-creation credential.
- Invites are single-use (consumption nulls the token hash in the same transaction as user
  creation) and expire on their own after `INVITE_TTL_HOURS`.
- Resends are cooldown-gated so the app cannot be used as a mail-bomb relay against a
  third-party address. Copy-link is exempt because it sends nothing.
- Invite links are built from `PUBLIC_BASE_URL`, never from the request's `Host`.
- Accepting an invite while already signed in is refused, not silently merged.
- **Both** auth cookies carry `HttpOnly`, `SameSite=Lax`, and `Secure` when `HTTPS=true`.
  Flask-Login's `remember_token` is a second, independent authenticator — it rebuilds a full
  session on its own — and its library defaults ship with neither `Secure` nor `SameSite`,
  which made it both a cleartext credential on any `http://` request and a way around the
  session cookie's `SameSite`. It is capped at 30 days rather than Flask-Login's 365, and
  invite acceptance no longer mints one (nobody asked for it on that form).
- Security headers set on every response: `X-Frame-Options`, `X-Content-Type-Options`, HSTS.
- Passwords use PBKDF2-SHA256 at 600,000 rounds (`User.set_password`) — deliberately slow.
  `/login` verifies a dummy hash when the username is unknown, so both branches cost the
  same and response time does not reveal whether an account exists.
- `ProxyFix` is wrapped around the WSGI app with `x_for=TRUSTED_PROXY_COUNT`. **Never set that
  higher than the real number of proxies appending to `X-Forwarded-For`** — a client's own header
  then survives into the trusted region and it can pick its own rate-limit identity.
- Chunked uploads are bounded by DB-backed per-user quotas (`MAX_CONCURRENT_UPLOADS_PER_USER`,
  `MAX_INFLIGHT_UPLOAD_MB_PER_USER`), not by the rate limiter — limits live in `memory://` per
  worker and reset on every deploy, so they are damping, not a defence.

---

## Data Models

| Model | Key Fields |
|---|---|
| `User` | `username`, `email`, `password_hash`, `is_admin` |
| `AllowedEmail` | `email`, `token_hash` (SHA-256; the plaintext is never stored), `token_issued_at`, `expires_at`, `last_sent_at`, `send_count`, `last_send_error`, `accepted_at`, `accepted_user_id`, `note`, `prefill_username`, `invited_by_id` — one row is both the whitelist entry and the invite credential. `state` and `is_pending` are **derived properties**, never columns |
| `Album` | `name`, `token`, `view_token`, `allow_upload`, `owner_id` |
| `Photo` | `stored_filename` (UUID), `original_filename`, `mime_type`, `album_id`, `uploader_id`, `is_thumbnail` |
| `UploadSession` | `upload_id` (UUID, the resume handle), `client_key`, `total_size`, `received_bytes`, `album_id`, `user_id`, `updated_at` — unique on `(user_id, album_id, client_key)` |

---

## Supported File Types

**Photos:** JPEG, PNG, GIF, WebP, HEIC (auto-converted to JPEG)
**Videos:** MP4, MOV, AVI, WebM, MPG, MPEG

---

## Tests

`pytest` + Flask test client, in `tests/`. Run with `.venv/bin/python -m pytest tests/ -q`
(pytest is installed in `.venv`, not in the system interpreter).

| Module | Covers |
|---|---|
| `protocol.py` | An executable copy of `docs/upload_protocol.md` — every JSON key and header spelled out as a literal, mirroring what `uploader.js` actually sends |
| `test_upload_contract.py` | The client/server wire seam: field names, header casing, octet-stream body, shared `results` envelope |
| `test_upload_lifecycle.py` | Happy path byte-for-byte, resumption, legacy single-request path |
| `test_upload_integrity.py` | 409/422 handling and the truncate-back crash-safety property |
| `test_upload_limits.py` | Rate-limit charging asymmetry, size ceilings, per-user quotas |
| `test_upload_access.py` | Cross-user isolation, revoked `allow_upload`, anonymous callers |
| `test_upload_recovery.py` | TTL sweep, orphaned partials, quota reclamation |
| `test_upload_security.py` | Decompression-bomb ceiling and the MIME-not-extension path choice |
| `test_upload_cancel.py` | Cancel releasing quota immediately, idempotence, cross-user isolation |
| `test_upload_config.py` | `validate_upload_limits()` — caps that contradict each other |
| `test_mailer.py` | Backend selection from config, timeout application, `MailError` on refusal, `MemoryMailer` capture |
| `test_invite_model.py` | The six-state `state` property, its evaluation order, and the migration's effect on pre-existing rows |
| `test_invite_lifecycle.py` | Issue → send → accept, single use, rotation killing the old link, TTL expiry, resend cooldown |
| `test_invite_email.py` | The composed invitation: both parts, the link's origin, refusal to compose without `PUBLIC_BASE_URL` |
| `test_invite_admin.py` | The panel's four actions, the issue-commit-then-send order, and what each failure flashes |
| `test_invite_access.py` | **The email cannot be overridden via the form**; bad/expired/replayed tokens; accepting while signed in; `/register` is gone; rate limits |
| `test_album_download.py` | Archive contents and duplicate-name suffixing; that peak memory does not scale with album size; that the staging file and its descriptor never leak; the per-user download budget |
| `test_share_access.py` | The grant model: a view-only link never yields upload rights, a downgrade takes them away, and neither a bare token nor an account alone reaches an album |
| `test_session_security.py` | Raw `Set-Cookie` flags on both auth cookies, the login timing equaliser, and every `SECRET_KEY` value the boot check refuses |
| `test_csrf.py` | The **only** module that runs with CSRF on. Each caller class refused without a token and accepted with one; the url_map sweep that covers routes not yet written; the templates scanned for a POST form missing its field |

`conftest.py` sets the environment before importing the app, because `config.py` reads
env vars at module import time — which is why the invite TTL and resend cooldown are chosen
there and not inside a test. A `mailer` fixture swaps a `MemoryMailer` into
`app.extensions['mailer']`, so every test asserts on what *would* have been sent, with no
network and no monkeypatched module globals.

The shared `app` fixture sets `WTF_CSRF_ENABLED = False`, so ordinary tests post forms without
a token; `test_csrf.py` flips that key back on per test and is the only module that exercises
the real thing. A test whose subject is a *guest* must mint an `AlbumAccess` grant with the
`grant_access` fixture — holding the share token is deliberately not enough any more, and a
test that leaned on it would stop describing the app.

The shared `app` fixture also sets **`WTF_CSRF_ENABLED = False`**. No test outside
`test_csrf.py` is about CSRF, and every one of them posts without a browser having rendered a
token first — leaving it on would turn all of them into a 400 that says nothing about what
they were testing. It is a config flag rather than an unregistered extension precisely so
`test_csrf.py` can flip that same key back to `True` for the duration of a test and drive the
real endpoints through the real hook. It flips a flag rather than building a second app
because `create_app()` re-registers every route on the module-level limiter singleton.

Coverage is upload-, invite- and access-control-focused. Album browsing, the dashboard
filters and the admin panel's rendering are still verified manually via the Docker test
container. See [#25](https://github.com/gfvandehei/PixelVault/issues/25).

---

## Utility Scripts

| Script | Purpose |
|---|---|
| `scripts/create_admin.py` | Create admin without Flask CLI (useful in CI/Docker) |
| `scripts/migrate_heic.py` | Convert existing HEIC uploads to JPEG, updates DB, regenerates thumbnails |
| `scripts/migrate_thumbnails_orientation.py` | Retroactively fix EXIF orientation on stored thumbnails |

## Flask CLI Commands

| Command | Purpose |
|---|---|
| `flask --app app create-admin` | Create the admin user from `ADMIN_*` env vars |
| `flask --app app cleanup-uploads` | Delete upload sessions idle past `UPLOAD_SESSION_TTL_HOURS` and their `.part` files |
