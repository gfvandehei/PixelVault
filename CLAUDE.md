# PixelVault — Claude Onboarding Guide

## Project Overview

PixelVault is an invite-only, self-hosted photo album sharing application. Users upload photos/videos into named albums, then share albums via two link types: an upload link (guests can browse & upload) and a view-only link. Built with Flask + SQLite + Nginx, packaged for Docker deployment.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Flask 3.1+, SQLAlchemy ORM, Flask-Login, Flask-Limiter |
| Database | SQLite (via SQLAlchemy) |
| Auth | Flask-Login + bcrypt (600k rounds) |
| Image Processing | Pillow, pillow-heif (HEIC support) |
| File Validation | python-magic (MIME type verification) |
| Frontend | Jinja2 templates + vanilla JS |
| Production Server | Gunicorn (2 workers) behind Nginx |
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
│   ├── extensions.py               # db, login_manager, limiter — initialized separately to avoid circular imports
│   ├── models.py                   # SQLAlchemy models: User, Album, Photo, AllowedEmail
│   ├── utils.py                    # File handling, ZIP building, shared decorators
│   └── routes/
│       ├── __init__.py             # Registers all route blueprints via register(app)
│       ├── auth.py                 # /register, /login, /logout
│       ├── albums.py               # /dashboard, /album/* (CRUD, settings, ZIP download)
│       ├── share.py                # /share/<token> and /view/<view_token> (public links)
│       ├── media.py                # /media/<filename> (authenticated file serving)
│       ├── api.py                  # /api/* (JSON photo lists for gallery JS)
│       └── admin.py                # /admin (allowed emails, user list)
├── templates/                      # Jinja2 HTML (base.html + 8 pages)
├── docker/
│   ├── Dockerfile.prod             # Production image (Python 3.13-slim)
│   ├── Dockerfile.dev              # Dev image with hot reload
│   ├── prod.docker-compose.yml     # App + Nginx
│   └── test.docker-compose.yml     # Test container (port 5050, pre-seeded admin)
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
| `SECRET_KEY` | random | **YES** | Flask session secret — set a long random string |
| `HTTPS` | `false` | no | Set `true` in production (enables Secure cookies + HSTS) |
| `UPLOAD_FOLDER` | `./uploads` | no | Absolute path for uploaded files |
| `DATABASE_URL` | `sqlite:///pixelvault.db` | no | SQLAlchemy URI (4 slashes for absolute paths) |
| `MAX_UPLOAD_MB` | `500` | no | Max upload size per request |
| `FLASK_DEBUG` | `false` | no | Never `true` in production |
| `PORT` | `5000` | no | Server port |
| `ADMIN_USERNAME/EMAIL/PASSWORD` | — | no | Used only by `create-admin` CLI command |
| `DATA_DIRECTORY` | — | no | Instance directory for database |

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

### Share Link System
Each `Album` has two tokens:
- `token` → `/share/<token>` — upload link (guests can browse & upload)
- `view_token` → `/view/<view_token>` — view-only link (browse only)

Both links respect the album-level `allow_upload` toggle.

### Rate Limits (Flask-Limiter)
| Endpoint | Limit |
|---|---|
| Register | 10/hour |
| Login | 20/hour |
| Upload | 60/hour |
| Album create | 30/hour |
| Admin email add | 60/hour |

---

## Security Notes

- Registration is **invite-only**: admins add emails to `AllowedEmail` before users can register.
- Sessions use `HttpOnly=True`, `SameSite=Lax`, and `Secure=True` when `HTTPS=true`.
- Security headers set on every response: `X-Frame-Options`, `X-Content-Type-Options`, HSTS.
- bcrypt uses 600,000 rounds — password operations are intentionally slow.

---

## Data Models

| Model | Key Fields |
|---|---|
| `User` | `username`, `email`, `password_hash`, `is_admin` |
| `AllowedEmail` | `email` (whitelist for registration) |
| `Album` | `name`, `token`, `view_token`, `allow_upload`, `owner_id` |
| `Photo` | `stored_filename` (UUID), `original_filename`, `mime_type`, `album_id`, `uploader_id`, `is_thumbnail` |

---

## Supported File Types

**Photos:** JPEG, PNG, GIF, WebP, HEIC (auto-converted to JPEG)
**Videos:** MP4, MOV, AVI, WebM, MPG, MPEG

---

## Tests

No automated test suite exists yet. Testing is currently manual:
- Use the Docker test container (pre-seeded admin) for feature verification
- `pytest` + Flask test client is the recommended path for future tests

---

## Utility Scripts

| Script | Purpose |
|---|---|
| `scripts/create_admin.py` | Create admin without Flask CLI (useful in CI/Docker) |
| `scripts/migrate_heic.py` | Convert existing HEIC uploads to JPEG, updates DB, regenerates thumbnails |
| `scripts/migrate_thumbnails_orientation.py` | Retroactively fix EXIF orientation on stored thumbnails |
