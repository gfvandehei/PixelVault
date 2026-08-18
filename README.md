# PixelVault

A self-hosted photo and video sharing platform. Create albums, share links, and let others upload or browse via a polished web interface.

## Features

- **Account system** — Invite-only registration with bcrypt-hashed passwords
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

Registration is invite-only — only emails the admin has authorized can sign up. You must create the admin account first.

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

Visit http://localhost:5000, log in as admin, and go to **Admin** in the nav to authorize email addresses before sharing the registration link.

---

## Production Deployment (Docker + Nginx)

> **Which nginx?** `conf/nginx.conf` configures the `nginx` service in
> `docker/prod.docker-compose.yml`, which is gated behind `profiles: [nginx]` and does **not** start
> unless you run `--profile nginx`. If you instead terminate TLS on a separate reverse proxy and
> point it at the published `5000`, that proxy's config is the one that matters — see
> [#28](https://github.com/gfvandehei/PixelVault/issues/28) and
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

See [docs/upload_operations.md](docs/upload_operations.md) for how to tune the upload variables and
for a per-hop map of every limit an upload passes through.

---

## Security

PixelVault is designed with internet exposure in mind:

| Threat | Mitigation |
|--------|-----------|
| Unauthorized registration | Invite-only — admin must authorize each email before registration is allowed |
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
| Rate-limit evasion via spoofed IP | `ProxyFix` trusts exactly `TRUSTED_PROXY_COUNT` proxy hops |
| Unauthorized access | Media files served through Flask auth check, not static |

### Recommended additional hardening for production:
- Run behind Nginx (included) — never expose Flask directly
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
│   ├── extensions.py         # db, login_manager, limiter instances
│   ├── models.py             # User, AllowedEmail, Album, Photo (vanilla SQLAlchemy)
│   ├── utils.py              # File handling, ZIP building, admin_required decorator
│   ├── uploads.py            # Chunked upload sessions: quotas, chunk append, TTL sweep
│   └── routes/
│       ├── auth.py           # Register, login, logout
│       ├── albums.py         # Dashboard, create/view/delete album, download
│       ├── share.py          # Upload link, view-only link, file upload handler
│       ├── media.py          # Authenticated media serving
│       ├── api.py            # JSON photo list endpoints for gallery JS
│       └── admin.py          # Admin panel — allowed emails and user list
├── templates/
│   ├── base.html             # Base layout + nav
│   ├── login.html
│   ├── register.html
│   ├── dashboard.html        # Album list (owned + contributed)
│   ├── create_album.html
│   ├── album_view.html       # Owner's gallery view with share links
│   ├── album_upload.html     # Share page (upload or view-only depending on link)
│   ├── admin.html            # Admin panel
│   └── error.html
├── docs/                     # Upload protocol, client, and operations references
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
