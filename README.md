# PixelVault

A self-hosted photo and video sharing platform. Create albums, share links, and let others upload or browse via a polished web interface.

## Features

- **Account system** — Invite-only registration with bcrypt-hashed passwords
- **Albums** — Create named albums with descriptions
- **Upload links** — Share a link that lets others upload to your album
- **View-only links** — Share a separate read-only link for browsing without upload access
- **Drag-and-drop upload** — Batched uploads with progress tracking
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

Registration is invite-only — only emails the admin has authorized can sign up. You must create the admin account first:

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

### 1. Set your domain in nginx.conf

Edit `nginx.conf` and replace `your-domain.com` with your actual domain.

### 2. Obtain SSL certificates

Using Certbot (recommended):
```bash
certbot certonly --standalone -d your-domain.com
# Then copy certs:
mkdir certs
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem certs/
cp /etc/letsencrypt/live/your-domain.com/privkey.pem certs/
```

### 3. Configure .env

```bash
cp .env.example .env
# Set:
#   SECRET_KEY=<long random string>
#   HTTPS=true
#   MAX_UPLOAD_MB=500
```

### 4. Deploy

```bash
docker compose up -d
```

### 5. Auto-renew certificates

Add to crontab:
```
0 3 * * * certbot renew --quiet && docker compose restart nginx
```

---

## Environment Variables

| Variable          | Default    | Description                                        |
|-------------------|------------|----------------------------------------------------|
| `SECRET_KEY`      | *required* | Flask secret key — use a long random string        |
| `HTTPS`           | `false`    | Set `true` to enable Secure cookies and HSTS       |
| `UPLOAD_FOLDER`   | `uploads`  | Directory where uploaded files are stored          |
| `MAX_UPLOAD_MB`   | `500`      | Max upload size in MB per request                  |
| `FLASK_DEBUG`     | `false`    | Never set `true` in production                     |
| `PORT`            | `5000`     | Port for the Flask/Gunicorn server                 |
| `DATABASE_URL`    | *(SQLite)* | SQLAlchemy DB URI — defaults to `instance/pixelvault.db` |
| `DATA_DIRECTORY`  | —          | If set, used to locate `pixelvault.db` when `DATABASE_URL` is not set |
| `ADMIN_USERNAME`  | —          | Used by `flask create-admin` to set admin username |
| `ADMIN_EMAIL`     | —          | Used by `flask create-admin` to set admin email    |
| `ADMIN_PASSWORD`  | —          | Used by `flask create-admin` to set admin password |

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
| Spam uploads | Rate limited at 60 uploads/hour per IP |
| Unauthorized access | Media files served through Flask auth check, not static |

### Recommended additional hardening for production:
- Run behind Nginx (included) — never expose Flask directly
- Use HTTPS (Let's Encrypt is free)
- Set `HTTPS=true` in .env
- Store `uploads/` on a separate volume or object storage
- Regularly back up `instance/pixelvault.db`
- Monitor logs for abuse

---

## Project Structure

```
pixelvault/
├── app.py                    # Entry point — calls create_app(), used by Gunicorn
├── migrate_heic.py           # One-time script to convert stored HEIC files to JPEG
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── nginx.conf
├── pixelvault/               # Application package
│   ├── __init__.py           # create_app() factory, DB migrations, error handlers, CLI
│   ├── config.py             # Allowed file types, validation constants
│   ├── extensions.py         # db, login_manager, limiter instances
│   ├── models.py             # User, AllowedEmail, Album, Photo models
│   ├── utils.py              # File handling, ZIP building, admin_required decorator
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
├── uploads/                  # Created at runtime
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
