import os
import re
import uuid
import mimetypes
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse, urljoin

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, send_from_directory, abort, jsonify, g
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image
import magic  # python-magic for MIME type verification

# ──────────────────────────────────────────────
# App Configuration
# ──────────────────────────────────────────────

app = Flask(__name__)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(32).hex())
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL', 'sqlite:///pixelvault.db'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_UPLOAD_MB', 500)) * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('HTTPS', 'false').lower() == 'true'
app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 30  # 30 days

# Allowed file types (whitelist)
ALLOWED_PHOTO_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/heic'}
ALLOWED_VIDEO_TYPES = {'video/mp4', 'video/quicktime', 'video/x-msvideo', 'video/webm', 'video/mpeg'}
ALLOWED_MIME_TYPES = ALLOWED_PHOTO_TYPES | ALLOWED_VIDEO_TYPES

ALLOWED_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic',
    '.mp4', '.mov', '.avi', '.webm', '.mpg', '.mpeg'
}

# Input validation
_RE_USERNAME = re.compile(r'^[a-zA-Z0-9_\-]+$')
_RE_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
MAX_PASSWORD_LEN = 1024  # prevent slow-hash DoS via huge passwords

# ──────────────────────────────────────────────
# Extensions
# ──────────────────────────────────────────────

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per hour"],
    storage_uri="memory://"
)

# ──────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    albums = db.relationship('Album', backref='owner', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256:600000')

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class AllowedEmail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    note = db.Column(db.String(256), default='')
    added_at = db.Column(db.DateTime, default=datetime.utcnow)


class Album(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.String(512), default='')
    token = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    allow_anonymous = db.Column(db.Boolean, default=True)  # allow uploads without login
    photos = db.relationship('Photo', backref='album', lazy=True, cascade='all, delete-orphan')

    @property
    def photo_count(self):
        return len(self.photos)

    @property
    def cover_photo(self):
        photos = [p for p in self.photos if p.is_photo]
        return photos[0] if photos else None


class Photo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    album_id = db.Column(db.Integer, db.ForeignKey('album.id'), nullable=False)
    uploader_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    uploader_name = db.Column(db.String(64), default='Anonymous')
    stored_filename = db.Column(db.String(256), nullable=False)  # UUID-based, never user input
    original_filename = db.Column(db.String(256), nullable=False)
    mime_type = db.Column(db.String(64), nullable=False)
    file_size = db.Column(db.Integer, default=0)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    has_thumbnail = db.Column(db.Boolean, default=False)

    uploader = db.relationship('User', backref='photos', lazy=True)

    @property
    def is_photo(self):
        return self.mime_type in ALLOWED_PHOTO_TYPES

    @property
    def is_video(self):
        return self.mime_type in ALLOWED_VIDEO_TYPES

    @property
    def thumbnail_filename(self):
        return f"thumb_{self.stored_filename}"

    @property
    def file_size_human(self):
        size = self.file_size
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated

# ──────────────────────────────────────────────
# Security Headers
# ──────────────────────────────────────────────

@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    if os.environ.get('HTTPS', 'false').lower() == 'true':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def validate_file(file_storage):
    """Validate file by extension AND actual MIME type (magic bytes)."""
    filename = file_storage.filename
    if not filename:
        return None, "No filename provided"

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return None, f"File extension '{ext}' is not allowed"

    # Read first chunk to check magic bytes
    header = file_storage.read(2048)
    file_storage.seek(0)

    detected_mime = magic.from_buffer(header, mime=True)
    if detected_mime not in ALLOWED_MIME_TYPES:
        return None, f"File type '{detected_mime}' is not allowed"

    return detected_mime, None


def save_file(file_storage, mime_type):
    """Save file with a UUID name and generate thumbnail if image."""
    ext = Path(file_storage.filename).suffix.lower()
    stored_name = f"{uuid.uuid4()}{ext}"
    upload_dir = Path(app.config['UPLOAD_FOLDER'])
    upload_dir.mkdir(parents=True, exist_ok=True)
    save_path = upload_dir / stored_name
    file_storage.save(str(save_path))

    file_size = save_path.stat().st_size
    has_thumbnail = False

    # Generate thumbnail for images
    if mime_type in ALLOWED_PHOTO_TYPES:
        try:
            thumb_path = upload_dir / f"thumb_{stored_name}"
            with Image.open(str(save_path)) as img:
                img = img.convert('RGB')
                img.thumbnail((400, 400), Image.LANCZOS)
                img.save(str(thumb_path), 'JPEG', quality=85)
            has_thumbnail = True
        except Exception:
            pass

    return stored_name, file_size, has_thumbnail


def is_safe_redirect(target):
    """Return True only if target is a relative URL on the same host."""
    if not target:
        return False
    ref = urlparse(request.host_url)
    test = urlparse(urljoin(request.host_url, target))
    return test.scheme in ('http', 'https') and ref.netloc == test.netloc

# ──────────────────────────────────────────────
# Routes — Auth
# ──────────────────────────────────────────────

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
@limiter.limit("10 per hour")
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        errors = []
        if not username or len(username) < 3:
            errors.append("Username must be at least 3 characters.")
        elif len(username) > 64:
            errors.append("Username must be 64 characters or fewer.")
        elif not _RE_USERNAME.match(username):
            errors.append("Username may only contain letters, numbers, hyphens, and underscores.")
        if not email or not _RE_EMAIL.match(email):
            errors.append("Please enter a valid email address.")
        elif len(email) > 120:
            errors.append("Email address is too long.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        elif len(password) > MAX_PASSWORD_LEN:
            errors.append("Password is too long.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if not errors:
            if User.query.filter_by(username=username).first():
                errors.append("Username already taken.")
            if User.query.filter_by(email=email).first():
                errors.append("Email already registered.")
            if not AllowedEmail.query.filter_by(email=email).first():
                errors.append("This email address has not been authorized to register.")

        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('register.html', username=username, email=email)

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user, remember=True)
        flash('Welcome to PixelVault!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("20 per hour")
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()[:64]
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))

        # Reject oversized password before touching the hash function
        if len(password) > MAX_PASSWORD_LEN:
            flash('Invalid username or password.', 'error')
            return render_template('login.html', username=username)

        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            flash('Invalid username or password.', 'error')
            return render_template('login.html', username=username)

        login_user(user, remember=remember)
        next_page = request.args.get('next')
        return redirect(next_page if is_safe_redirect(next_page) else url_for('dashboard'))

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

# ──────────────────────────────────────────────
# Routes — Dashboard & Albums
# ──────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    albums = Album.query.filter_by(owner_id=current_user.id).order_by(Album.created_at.desc()).all()
    return render_template('dashboard.html', albums=albums)


@app.route('/album/create', methods=['GET', 'POST'])
@login_required
@limiter.limit("30 per hour")
def create_album():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        allow_anonymous = request.form.get('allow_anonymous') == 'on'

        if not name:
            flash('Album name is required.', 'error')
            return render_template('create_album.html')

        album = Album(
            name=name,
            description=description,
            owner_id=current_user.id,
            allow_anonymous=allow_anonymous
        )
        db.session.add(album)
        db.session.commit()
        flash(f'Album "{name}" created!', 'success')
        return redirect(url_for('album_view', token=album.token))

    return render_template('create_album.html')


@app.route('/album/<token>')
@login_required
def album_view(token):
    album = Album.query.filter_by(token=token).first_or_404()
    if album.owner_id != current_user.id:
        abort(403)
    photos = Photo.query.filter_by(album_id=album.id).order_by(Photo.uploaded_at.desc()).all()
    share_url = url_for('album_upload', token=token, _external=True)
    return render_template('album_view.html', album=album, photos=photos, share_url=share_url)


@app.route('/album/<token>/delete', methods=['POST'])
@login_required
def delete_album(token):
    album = Album.query.filter_by(token=token).first_or_404()
    if album.owner_id != current_user.id:
        abort(403)

    # Delete physical files
    for photo in album.photos:
        _delete_photo_files(photo)

    db.session.delete(album)
    db.session.commit()
    flash('Album deleted.', 'info')
    return redirect(url_for('dashboard'))


@app.route('/photo/<int:photo_id>/delete', methods=['POST'])
@login_required
def delete_photo(photo_id):
    photo = db.session.get(Photo, photo_id)
    if not photo:
        abort(404)
    album = db.session.get(Album, photo.album_id)
    if album.owner_id != current_user.id:
        abort(403)
    token = album.token
    _delete_photo_files(photo)
    db.session.delete(photo)
    db.session.commit()
    return jsonify({'success': True})


def _delete_photo_files(photo):
    upload_dir = Path(app.config['UPLOAD_FOLDER'])
    for fname in [photo.stored_filename, f"thumb_{photo.stored_filename}"]:
        fpath = upload_dir / fname
        if fpath.exists():
            fpath.unlink()

# ──────────────────────────────────────────────
# Routes — Public Share / Upload
# ──────────────────────────────────────────────

@app.route('/share/<token>', methods=['GET'])
def album_upload(token):
    """Public shareable upload page."""
    album = Album.query.filter_by(token=token).first_or_404()
    return render_template('album_upload.html', album=album)


@app.route('/share/<token>/upload', methods=['POST'])
@limiter.limit("60 per hour")
def do_upload(token):
    """Handle file upload from share page."""
    album = Album.query.filter_by(token=token).first_or_404()

    uploader_id = None
    uploader_name = request.form.get('uploader_name', 'Anonymous').strip() or 'Anonymous'

    if current_user.is_authenticated:
        uploader_id = current_user.id
        uploader_name = current_user.username
    elif not album.allow_anonymous:
        return jsonify({'error': 'This album requires login to upload.'}), 403

    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files provided.'}), 400

    results = []
    for file in files[:20]:  # max 20 files per request
        if not file or not file.filename:
            continue

        mime_type, err = validate_file(file)
        if err:
            results.append({'filename': file.filename, 'error': err})
            continue

        try:
            stored_name, file_size, has_thumbnail = save_file(file, mime_type)
        except Exception as e:
            results.append({'filename': file.filename, 'error': 'Upload failed.'})
            continue

        photo = Photo(
            album_id=album.id,
            uploader_id=uploader_id,
            uploader_name=uploader_name,
            stored_filename=stored_name,
            original_filename=secure_filename(file.filename)[:200],
            mime_type=mime_type,
            file_size=file_size,
            has_thumbnail=has_thumbnail
        )
        db.session.add(photo)
        results.append({'filename': file.filename, 'success': True})

    db.session.commit()
    return jsonify({'results': results})

# ──────────────────────────────────────────────
# Routes — Media Serving (authenticated)
# ──────────────────────────────────────────────

@app.route('/media/<path:filename>')
def serve_media(filename):
    """Serve uploaded files — only album owner or valid share token can access."""
    # Only allow simple filenames, no path traversal
    if '/' in filename or '..' in filename:
        abort(400)

    # Find the photo record
    stored_name = filename.replace('thumb_', '', 1) if filename.startswith('thumb_') else filename
    photo = Photo.query.filter_by(stored_filename=stored_name).first_or_404()
    album = db.session.get(Album, photo.album_id)

    # Allow access if: owner is logged in, OR uploader is logged in
    if current_user.is_authenticated and (
        album.owner_id == current_user.id or photo.uploader_id == current_user.id
    ):
        pass
    else:
        # Check share token in session (set when visiting /share/<token>)
        allowed_token = session.get('album_access_token')
        if allowed_token != album.token:
            abort(403)

    upload_dir = Path(app.config['UPLOAD_FOLDER']).resolve()
    return send_from_directory(str(upload_dir), filename)


@app.route('/share/<token>/media/<path:filename>')
def serve_share_media(token, filename):
    """Serve media for share page visitors."""
    if '/' in filename or '..' in filename:
        abort(400)
    album = Album.query.filter_by(token=token).first_or_404()

    stored_name = filename.replace('thumb_', '', 1) if filename.startswith('thumb_') else filename
    photo = Photo.query.filter_by(stored_filename=stored_name, album_id=album.id).first_or_404()

    session['album_access_token'] = token  # Grant media access for this album
    upload_dir = Path(app.config['UPLOAD_FOLDER']).resolve()
    return send_from_directory(str(upload_dir), filename)

# ──────────────────────────────────────────────
# API — Album photos (JSON, for gallery)
# ──────────────────────────────────────────────

@app.route('/api/album/<token>/photos')
def api_album_photos(token):
    album = Album.query.filter_by(token=token).first_or_404()

    # Auth check
    if not current_user.is_authenticated or album.owner_id != current_user.id:
        # Allow if they have share access
        if session.get('album_access_token') != token and not current_user.is_authenticated:
            abort(403)

    photos = Photo.query.filter_by(album_id=album.id).order_by(Photo.uploaded_at.desc()).all()
    result = []
    for p in photos:
        if current_user.is_authenticated and album.owner_id == current_user.id:
            thumb_url = url_for('serve_media', filename=p.thumbnail_filename if p.has_thumbnail else p.stored_filename)
            full_url = url_for('serve_media', filename=p.stored_filename)
        else:
            thumb_url = url_for('serve_share_media', token=token, filename=p.thumbnail_filename if p.has_thumbnail else p.stored_filename)
            full_url = url_for('serve_share_media', token=token, filename=p.stored_filename)

        result.append({
            'id': p.id,
            'original_filename': p.original_filename,
            'uploader_name': p.uploader_name,
            'uploaded_at': p.uploaded_at.strftime('%b %d, %Y %H:%M'),
            'file_size': p.file_size_human,
            'is_video': p.is_video,
            'thumb_url': thumb_url,
            'full_url': full_url,
        })
    return jsonify(result)

# ──────────────────────────────────────────────
# Routes — Admin
# ──────────────────────────────────────────────

@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    allowed_emails = AllowedEmail.query.order_by(AllowedEmail.added_at.desc()).all()
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin.html', allowed_emails=allowed_emails, users=users)


@app.route('/admin/email/add', methods=['POST'])
@login_required
@admin_required
@limiter.limit("60 per hour")
def admin_add_email():
    email = request.form.get('email', '').strip().lower()
    note = request.form.get('note', '').strip()

    if not email or '@' not in email:
        flash('Please enter a valid email address.', 'error')
        return redirect(url_for('admin_panel'))

    if AllowedEmail.query.filter_by(email=email).first():
        flash(f'{email} is already on the allowed list.', 'info')
        return redirect(url_for('admin_panel'))

    entry = AllowedEmail(email=email, note=note)
    db.session.add(entry)
    db.session.commit()
    flash(f'{email} has been authorized.', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/email/<int:entry_id>/remove', methods=['POST'])
@login_required
@admin_required
def admin_remove_email(entry_id):
    entry = db.session.get(AllowedEmail, entry_id)
    if not entry:
        abort(404)
    email = entry.email
    db.session.delete(entry)
    db.session.commit()
    flash(f'{email} has been removed from the allowed list.', 'info')
    return redirect(url_for('admin_panel'))


# ──────────────────────────────────────────────
# Error Handlers
# ──────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template('error.html', code=403, message="Access denied."), 403

@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, message="Page not found."), 404

@app.errorhandler(413)
def too_large(e):
    return render_template('error.html', code=413, message="File too large."), 413

@app.errorhandler(429)
def rate_limited(e):
    return render_template('error.html', code=429, message="Too many requests. Please slow down."), 429

# ──────────────────────────────────────────────
# Init
# ──────────────────────────────────────────────

def create_app():
    with app.app_context():
        db.create_all()
    return app


@app.cli.command('create-admin')
def create_admin_command():
    """Create the admin user from ADMIN_USERNAME / ADMIN_EMAIL / ADMIN_PASSWORD env vars."""
    import click
    username = os.environ.get('ADMIN_USERNAME', '').strip()
    email = os.environ.get('ADMIN_EMAIL', '').strip().lower()
    password = os.environ.get('ADMIN_PASSWORD', '').strip()

    if not username or not email or not password:
        click.echo('Set ADMIN_USERNAME, ADMIN_EMAIL, and ADMIN_PASSWORD environment variables.')
        return

    with app.app_context():
        db.create_all()
        if User.query.filter_by(is_admin=True).first():
            click.echo('An admin user already exists.')
            return
        if User.query.filter_by(email=email).first():
            click.echo(f'A user with email {email} already exists.')
            return
        admin = User(username=username, email=email, is_admin=True)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        click.echo(f'Admin user "{username}" created successfully.')


if __name__ == '__main__':
    create_app()
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=debug)
