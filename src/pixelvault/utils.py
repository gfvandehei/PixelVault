import io
import uuid
import zipfile
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse, urljoin

import magic
from flask import request, abort, current_app
from flask_login import current_user
from PIL import Image

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass  # HEIC support unavailable if pillow-heif not installed

from .config import ALLOWED_PHOTO_TYPES, ALLOWED_MIME_TYPES, ALLOWED_EXTENSIONS


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def validate_file(file_storage):
    """Validate file by extension AND actual MIME type (magic bytes)."""
    filename = file_storage.filename
    if not filename:
        return None, "No filename provided"

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return None, f"File extension '{ext}' is not allowed"

    header = file_storage.read(2048)
    file_storage.seek(0)

    detected_mime = magic.from_buffer(header, mime=True)
    if detected_mime not in ALLOWED_MIME_TYPES:
        return None, f"File type '{detected_mime}' is not allowed"

    return detected_mime, None


def save_file(file_storage, mime_type):
    """Save file with a UUID name and generate thumbnail if image.
    HEIC/HEIF files are converted to JPEG for browser compatibility."""
    ext = Path(file_storage.filename).suffix.lower()
    is_heic = ext in ('.heic', '.heif') or mime_type in ('image/heic', 'image/heif')

    if is_heic:
        ext = '.jpg'

    stored_name = f"{uuid.uuid4()}{ext}"
    upload_dir = Path(current_app.config['UPLOAD_FOLDER'])
    upload_dir.mkdir(parents=True, exist_ok=True)
    save_path = upload_dir / stored_name

    if is_heic:
        with Image.open(file_storage.stream) as img:
            img = img.convert('RGB')
            img.save(str(save_path), 'JPEG', quality=92)
    else:
        file_storage.save(str(save_path))

    file_size = save_path.stat().st_size
    has_thumbnail = False

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


def delete_photo_files(photo):
    upload_dir = Path(current_app.config['UPLOAD_FOLDER'])
    for fname in [photo.stored_filename, f"thumb_{photo.stored_filename}"]:
        fpath = upload_dir / fname
        if fpath.exists():
            fpath.unlink()


def build_album_zip(album):
    """Return a BytesIO containing a ZIP of all photos in the album."""
    buf = io.BytesIO()
    upload_dir = Path(current_app.config['UPLOAD_FOLDER'])
    seen = {}
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for photo in album.photos:
            src = upload_dir / photo.stored_filename
            if not src.exists():
                continue
            name = photo.original_filename
            if name in seen:
                seen[name] += 1
                stem, suffix = Path(name).stem, Path(name).suffix
                name = f"{stem}_{seen[name]}{suffix}"
            else:
                seen[name] = 0
            zf.write(str(src), name)
    buf.seek(0)
    return buf

def create_admin(username, email, password, session, print=print):
    """Create the admin user from ADMIN_USERNAME / ADMIN_EMAIL / ADMIN_PASSWORD env vars."""
    from .models import User

    if not username or not email or not password:
        print('Set ADMIN_USERNAME, ADMIN_EMAIL, and ADMIN_PASSWORD environment variables.')
        return

    if session.query(User).filter_by(is_admin=True).first():
        print('An admin user already exists.')
        return
    if session.query(User).filter_by(email=email).first():
        print(f'A user with email {email} already exists.')
        return

    admin = User(username=username, email=email, is_admin=True)
    admin.set_password(password)
    session.add(admin)
    session.commit()
    print(f'Admin user "{username}" created successfully.')