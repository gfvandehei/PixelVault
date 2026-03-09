# PixelVault

A self-hosted photo and video sharing platform. Create albums, share links, and let anyone upload to your server via a polished web interface.

## Features

- **Account system** — Register/login with bcrypt-hashed passwords
- **Albums** — Create named albums with descriptions
- **Shareable upload links** — Each album has a unique UUID link anyone can upload to
- **Anonymous or members-only** — Choose whether guests need an account to upload
- **Drag-and-drop upload** — Batched uploads with progress tracking
- **Thumbnail generation** — Automatic JPEG thumbnails for photos
- **Lightbox gallery** — Browse photos full-screen with keyboard navigation
- **Download & delete** — Album owners can manage all files
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
sudo apt-get install libmagic1 libmagic-dev libjpeg-dev libpng-dev
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

| Variable          | Default    | Description                                       |
|-------------------|------------|---------------------------------------------------|
| `SECRET_KEY`      | *required* | Flask secret key — use a long random string       |
| `HTTPS`           | `false`    | Set `true` to enable Secure cookies and HSTS      |
| `UPLOAD_FOLDER`   | `uploads`  | Directory where files are stored                  |
| `MAX_UPLOAD_MB`   | `500`      | Max upload size in MB per request                 |
| `FLASK_DEBUG`     | `false`    | Never set `true` in production                    |
| `PORT`            | `5000`     | Port for the Flask/Gunicorn server                |
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
├── app.py                 # Main Flask application
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── nginx.conf
├── templates/
│   ├── base.html          # Base layout + nav
│   ├── login.html
│   ├── register.html
│   ├── dashboard.html     # Album list
│   ├── create_album.html
│   ├── album_view.html    # Owner's gallery view
│   ├── album_upload.html  # Public share page
│   ├── admin.html         # Admin panel (allowed emails + users)
│   └── error.html
├── uploads/               # Created at runtime
└── instance/              # SQLite database (created at runtime)
```

---

## Supported File Types

**Photos:** JPEG, PNG, GIF, WebP, HEIC  
**Videos:** MP4, MOV, AVI, WebM, MPG/MPEG

---

## License

MIT
