# PixelVault

A self-hosted photo and video sharing platform. Create albums, share links, and let others upload or browse via a polished web interface.

## Features

- **Account system** — Invite-only: an admin issues an invitation, the app emails a single-use link, and that link is the only way to register. bcrypt-hashed passwords
- **Albums** — Create named albums with descriptions
- **Upload links** — Share a link that lets others upload to your album
- **View-only links** — Share a separate read-only link for browsing without upload access
- **Drag-and-drop upload** — Batched uploads with progress tracking
- **Chunked resumable uploads** — Large files upload in 8 MiB slices and resume after a dropped connection, page reload, or browser restart
- **HEIC support** — Apple HEIC photos are converted to JPEG automatically on upload
- **Thumbnail generation** — Automatic JPEG thumbnails for photos
- **Lightbox gallery** — Browse photos full-screen with keyboard navigation and image preloading
- **ZIP download** — Download all album photos as a ZIP file
- **Security** — See Security section below

---

## Quick Start (Local)

### 1. Install system dependencies

**macOS:**
```bash
brew install libmagic
```

**Ubuntu/Debian:**
```bash
sudo apt-get install libmagic1 libmagic-dev libjpeg-dev libpng-dev libwebp-dev
```

### 2. Set up Python environment

```bash
cd pixelvault
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env — set SECRET_KEY to a long random string:
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 4. Create the admin account

Registration is invite-only and there is no sign-up page, so the first account has to be created from the command line.

**Option A — standalone script (recommended):**
```bash
python scripts/create_admin.py --env .env --user yourname --email you@example.com --password yourpassword
```

**Option B — Flask CLI:**
```bash
export ADMIN_USERNAME=yourname
export ADMIN_EMAIL=you@example.com
export ADMIN_PASSWORD=yourpassword
flask --app app create-admin
```

This only needs to be done once. If an admin already exists the command will do nothing.

### 5. Run

```bash
python app.py
```

Visit http://localhost:5000 and log in as admin.

### 6. Invite someone

Go to **Admin** in the nav and add an email address. That mints a single-use invitation link and
emails it; the recipient clicks it, picks a username and password, and is in. Nobody can register
any other way.

With no `SMTP_HOST` set, the invitation is printed to the application log instead of sent — copy
the link out of the log and hand it over. To send real mail, set `PUBLIC_BASE_URL`, `SMTP_HOST`
and `MAIL_FROM`; see [docs/configuration.md §5](docs/configuration.md#5-mail--invites) for the
full list (the Gmail app-password profile is there too) and
[docs/registration_invites.md](docs/registration_invites.md) for the operator guide — resending,
the copy-link fallback, and what to do when mail does not arrive.

---

## Production Deployment (Docker + Nginx)

> **Which nginx?** `conf/nginx.conf` configures the `nginx` service in
> `docker/prod.docker-compose.yml`, which is gated behind `profiles: [nginx]` and does **not** start
> unless you run `--profile nginx`. If you instead terminate TLS on a separate reverse proxy, point
> it at `http://127.0.0.1:5000` — the app container publishes its port on loopback only, so that
> proxy must run on the same host and its config is the one that matters — see
> [#28](https://github.com/gfvandehei/PixelVault/issues/28),
> [docs/configuration.md §6](docs/configuration.md#6-reverse-proxy) for why the origin is closed and
> what `TRUSTED_PROXY_COUNT` has to be, and
> [docs/upload_operations.md](docs/upload_operations.md), which maps every limit an upload passes
> through and which hop to blame for each failure.

### 1. Set your domain in nginx.conf

Edit `conf/nginx.conf` and replace `your-domain.com` with your actual domain.

### 2. Obtain SSL certificates

Using Certbot (recommended):
```bash
certbot certonly --standalone -d your-domain.com
# Then copy certs:
mkdir certs
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem certs/
cp /etc/letsencrypt/live/your-domain.com/privkey.pem certs/
```

### 3. Configure .env.prod

```bash
cp .env.prod .env.prod   # already provided — edit values
# Required:
#   SECRET_KEY=<long random string>
#   UPLOAD_FOLDER=<absolute path for uploads>
#   DATABASE_PATH=<absolute path for the directory containing pixelvault.db>
#   HTTPS=true
#   MAX_UPLOAD_MB=500
```

`DATABASE_URL` is set automatically in the compose file to `sqlite:////app/instance/pixelvault.db` (note the four slashes — required for an absolute path in SQLite URIs).

### 4. Deploy

```bash
docker compose -f ./docker/prod.docker-compose.yml --env-file .env.prod up -d --build
```

### 5. Auto-renew certificates

Add to crontab:
```
0 3 * * * certbot renew --quiet && docker compose restart nginx
```

### 6. Check the path in front of the app

If a CDN or proxy sits in front of the deployment, confirm its request-body limit before uploading
anything large. Cloudflare's free and Pro plans reject bodies over 100 MB **at the edge**, which
produces a silent stall with no HTTP status and nothing in the app logs. PixelVault uploads large
files in `UPLOAD_CHUNK_SIZE` slices specifically to stay under that ceiling; set
`TRUSTED_PROXY_COUNT` to the real number of proxy hops at the same time. Both are covered in
[docs/upload_operations.md](docs/upload_operations.md).

---

## Environment Variables

| Variable          | Default    | Description                                        |
|-------------------|------------|----------------------------------------------------|
| `SECRET_KEY`      | *required* | Flask secret key — use a long random string        |
| `HTTPS`           | `false`    | Set `true` to enable Secure cookies and HSTS       |
| `UPLOAD_FOLDER`   | `uploads`  | Absolute path where uploaded files are stored; mounted as a volume in Docker |
| `DATABASE_PATH`   | —          | Absolute path to the directory containing `pixelvault.db`; mounted as a volume in Docker |
| `DATABASE_URL`    | `sqlite:///pixelvault.db` | SQLAlchemy DB URI. Use four slashes for absolute paths: `sqlite:////abs/path/db` |
| `MAX_UPLOAD_MB`   | `500`      | Max upload size in MB per request                  |
| `FLASK_DEBUG`     | `false`    | Never set `true` in production                     |
| `PORT`            | `5000`     | Port for the Flask/Gunicorn server                 |
| `ADMIN_USERNAME`  | —          | Used by `flask create-admin` and `scripts/create_admin.py` |
| `ADMIN_EMAIL`     | —          | Used by `flask create-admin` and `scripts/create_admin.py` |
| `ADMIN_PASSWORD`  | —          | Used by `flask create-admin` and `scripts/create_admin.py` |
| `UPLOAD_CHUNK_SIZE` | `8388608` (8 MiB) | Chunk size in bytes for large uploads. Keep it well below any proxy/CDN request-body cap in front of the app |
| `UPLOAD_SESSION_TTL_HOURS` | `24` | How long an interrupted upload stays resumable before its partial file is reclaimed |
| `MAX_CONCURRENT_UPLOADS_PER_USER` | `10` | Open upload sessions one user may hold at once |
| `MAX_INFLIGHT_UPLOAD_MB_PER_USER` | `2048` | Total size, in MB, of one user's in-progress uploads. Bounds disk held by abandoned partials |
| `TRUSTED_PROXY_COUNT` | `1` | Number of reverse proxies in front of the app that append to `X-Forwarded-For`. **Setting it higher than the true count lets clients spoof their IP** — when unsure, set it lower |
| `PUBLIC_BASE_URL` | — | External origin invite links are built from, e.g. `https://photos.example.com`. Required once SMTP is configured |
| `MAIL_ENABLED` | `true` | Set `false` to stop sending entirely; invites are still issued and can be handed over with the admin copy-link button |
| `SMTP_HOST` | — | Leave empty to print invitations to the log instead of sending them |
| `SMTP_PORT` | `587` | 587 with `starttls`, 465 with `ssl` |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | — | Leave both empty for an unauthenticated relay |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl`, or `none` |
| `MAIL_FROM` | — | Sender address. Required once SMTP is configured |
| `MAIL_FROM_NAME` | `PixelVault` | Sender display name |
| `MAIL_TIMEOUT_SECONDS` | `10` | Socket timeout for the SMTP conversation |
| `ADMIN_CONTACT` | `MAIL_FROM` | Address shown to accountless guests who follow a share link |
| `INVITE_TTL_HOURS` | `72` | How long an invitation link stays valid |
| `INVITE_RESEND_COOLDOWN_SECONDS` | `60` | Minimum gap between two sends of one invitation |

See [docs/configuration.md](docs/configuration.md) for every variable with its reasoning,
and [docs/upload_operations.md](docs/upload_operations.md) for how to tune the upload variables
and a per-hop map of every limit an upload passes through.

---

## Security

PixelVault is designed with internet exposure in mind:

| Threat | Mitigation |
|--------|-----------|
| Unauthorized registration | No public sign-up. Accounts are created only by following a single-use, expiring invitation link an admin issued for that specific address |
| Registering under someone else's address | The address is read from the invite record on the server, never from the submitted form |
| Leaked database or backup | Invitation tokens are stored only as SHA-256 hashes — the plaintext link is never persisted |
| Replayed or stale invitation links | Single-use (consumed in the same transaction as account creation) and expiring after `INVITE_TTL_HOURS` |
| Using the invite mailer to spam a third party | Per-invite resend cooldown plus a 30/hour limit on the admin resend action |
| Weak passwords | Minimum 8 chars, bcrypt with 600,000 rounds |
| Brute force | Flask-Limiter: 20 login attempts/hour per IP |
| File upload attacks | Extension whitelist + magic-byte MIME verification |
| Path traversal | UUID-based filenames, no user input in file paths |
| Malicious uploads | Pillow re-encodes thumbnails; originals stored outside web root |
| Session hijacking | HttpOnly, SameSite=Lax cookies; Secure flag when HTTPS=true |
| Clickjacking | X-Frame-Options: SAMEORIGIN |
| MIME sniffing | X-Content-Type-Options: nosniff |
| Spam uploads | Rate limited at 600 uploads/hour on the single-request path; chunked uploads are bounded by per-user session and byte quotas instead |
| Disk exhaustion via abandoned uploads | Per-user caps on open sessions and in-flight bytes; partials older than the TTL are swept automatically |
| Rate-limit evasion via spoofed IP | `ProxyFix` trusts exactly `TRUSTED_PROXY_COUNT` proxy hops, and the origin is published on `127.0.0.1` only, so the proxy chain that count describes cannot be walked around |
| Unauthorized access | Media files served through Flask auth check, not static |

### Recommended additional hardening for production:
- Run behind Nginx (included) — never expose Flask directly. The compose file already binds the
  app's port to `127.0.0.1`; do not widen it. Note that a firewall is not an alternative, since
  Docker's published-port rules are evaluated before the chain `ufw` writes into
- Use HTTPS (Let's Encrypt is free)
- Set `HTTPS=true` in .env
- Store `uploads/` on a separate volume or object storage
- Regularly back up `instance/pixelvault.db` and `uploads/`, excluding `uploads/partials/`
- Monitor logs for abuse

---

## Project Structure

```
pixelvault/
├── app.py                    # Entry point — calls create_app(), used by Gunicorn
├── migrate_heic.py           # One-time script to convert stored HEIC files to JPEG
├── requirements.txt
├── .env.prod                 # Production environment template
├── scripts/
│   └── create_admin.py       # Standalone admin creation script (no Flask CLI needed)
├── docker/
│   ├── Dockerfile.prod       # Production Docker image
│   ├── Dockerfile.dev        # Development Docker image
│   └── prod.docker-compose.yml
├── src/pixelvault/           # Application package
│   ├── __init__.py           # create_app() factory, DB migrations, error handlers, CLI
│   ├── config.py             # Env-var config, allowed file types, validation constants
│   ├── extensions.py         # db, login_manager, limiter, mailer instances
│   ├── models.py             # User, AllowedEmail, Album, Photo (vanilla SQLAlchemy)
│   ├── utils.py              # File handling, ZIP building, admin_required decorator
│   ├── uploads.py            # Chunked upload sessions: quotas, chunk append, TTL sweep
│   ├── mailer.py             # SMTP transport (and console/null/memory backends)
│   ├── emails.py             # Renders the invitation message
│   ├── invites.py            # Invite lifecycle: issue, rotate, validate, consume
│   └── routes/
│       ├── auth.py           # Login, logout, invitation acceptance
│       ├── albums.py         # Dashboard, create/view/delete album, download
│       ├── share.py          # Upload link, view-only link, file upload handler
│       ├── media.py          # Authenticated media serving
│       ├── api.py            # JSON photo list endpoints for gallery JS
│       └── admin.py          # Admin panel — invites, users, albums
├── templates/
│   ├── base.html             # Base layout + nav
│   ├── login.html
│   ├── register.html         # Invitation acceptance form
│   ├── email/                # invite.txt / invite.html — the invitation message
│   ├── dashboard.html        # Album list (owned + contributed)
│   ├── create_album.html
│   ├── album_view.html       # Owner's gallery view with share links
│   ├── album_upload.html     # Share page (upload or view-only depending on link)
│   ├── admin.html            # Admin panel
│   └── error.html
├── docs/                     # Configuration, invites, database schema, upload references
├── uploads/                  # Created at runtime
│   └── partials/             # In-progress chunked uploads (transient — exclude from backups)
└── instance/                 # SQLite database (created at runtime)
```

---

## Share Links

Each album has two shareable links, both visible in the album owner's view:

| Link type | URL pattern | What recipients can do |
|-----------|-------------|------------------------|
| **Upload link** | `/share/<token>` | Browse the gallery and upload files |
| **View-only link** | `/view/<view_token>` | Browse the gallery only — upload UI is hidden and the upload endpoint refuses requests |

The owner can also toggle a master **Allow uploads** switch on the album, which disables the upload link for everyone regardless of which link they use.

---

## HEIC Migration

If you have existing HEIC files stored before HEIC-to-JPEG conversion was added, run the migration script once:

```bash
python migrate_heic.py --dry-run   # preview changes
python migrate_heic.py             # convert and update database
```

The script converts stored `.heic` files to JPEG, regenerates thumbnails, updates the database, and removes the original HEIC files. The filenames shown to users are preserved.

---

## Supported File Types

**Photos:** JPEG, PNG, GIF, WebP, HEIC (converted to JPEG on upload)
**Videos:** MP4, MOV, AVI, WebM, MPG/MPEG

---

## License

MIT
