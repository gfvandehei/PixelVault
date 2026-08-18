import io
import os
import uuid
import zipfile
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse, urljoin

import magic
from flask import request, abort, current_app
from flask_login import current_user
from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass  # HEIC support unavailable if pillow-heif not installed

from .config import ALLOWED_PHOTO_TYPES, ALLOWED_MIME_TYPES, ALLOWED_EXTENSIONS


def admin_required(f):
    """Decorator that restricts a route to admin users, returning 403 for everyone else."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# How many leading bytes libmagic is shown. The signatures it matches all live in
# the first few hundred bytes, and a chunked upload only has its first slice on hand
# when the early type check runs, so the sample must stay small enough to be cheap
# and stable across both callers.
_MIME_SAMPLE_BYTES = 2048


def validate_file_header(filename, header):
    """Validate a name's extension and a leading byte sample's real MIME type.

    The single place the allow-lists are consulted. Every upload path reaches this
    function — the legacy single request, the early check on a chunked upload's first
    slice, and the authoritative check on the assembled file — so no path can drift
    into accepting a type another one refuses. Returns ``(mime_type, None)`` or
    ``(None, error_message)``.
    """
    if not filename:
        return None, "No filename provided"

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return None, f"File extension '{ext}' is not allowed"

    detected_mime = magic.from_buffer(header, mime=True)
    if detected_mime not in ALLOWED_MIME_TYPES:
        return None, f"File type '{detected_mime}' is not allowed"

    return detected_mime, None


def validate_file(file_storage):
    """Validate file by extension AND actual MIME type (magic bytes)."""
    if not file_storage.filename:
        return None, "No filename provided"

    header = file_storage.read(_MIME_SAMPLE_BYTES)
    file_storage.seek(0)
    return validate_file_header(file_storage.filename, header)


def validate_stored_file(path, original_filename):
    """Validate a file already assembled on disk, under the name the client declared for it.

    The security boundary for a chunked upload: the per-chunk checks only ever see a
    slice, so the type of the finished artefact is not known until it is whole.
    """
    with open(path, 'rb') as fh:
        header = fh.read(_MIME_SAMPLE_BYTES)
    return validate_file_header(original_filename, header)


_EXIF_DATETIME_ORIGINAL = 36867
_EXIF_DATETIME = 306
_EXIF_SUBIFD_POINTER = 0x8769


def extract_exif_taken_at(img):
    """Return the photo's EXIF capture date (DateTimeOriginal, falling back to DateTime), or None."""
    try:
        exif = img.getexif()
        date_str = None
        try:
            sub_ifd = exif.get_ifd(_EXIF_SUBIFD_POINTER)
            date_str = sub_ifd.get(_EXIF_DATETIME_ORIGINAL)
        except Exception:
            pass
        date_str = date_str or exif.get(_EXIF_DATETIME)
        if not date_str:
            return None
        return datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
    except Exception:
        return None


def _is_file_storage(source):
    """Return True if the incoming bytes are a Werkzeug upload rather than a path on disk.

    The one place the two shapes are told apart. A single-request upload arrives as a
    FileStorage still being streamed off the socket; a chunked one arrives as a
    finished ``.part`` file, because the chunk appends already assembled it.
    """
    return hasattr(source, 'stream')


def _open_source_image(source):
    """Open the incoming bytes as a PIL image, from either a FileStorage or a path.

    Handed the path itself when there is one, so Pillow owns and closes the descriptor
    it opens — passing our own handle would leak it, since Pillow only closes what it
    opened.
    """
    return Image.open(source.stream if _is_file_storage(source) else str(source))


def _place_source(source, dest):
    """Put the incoming bytes at ``dest`` byte-for-byte, without re-encoding them.

    A file already assembled on disk is *moved*, not copied: partials live inside
    UPLOAD_FOLDER, so this is a same-filesystem rename — atomic, and it spares a
    470 MB video a pointless read-and-rewrite on the way to its final name.
    """
    if _is_file_storage(source):
        source.save(str(dest))
    else:
        os.replace(str(source), str(dest))


def store_upload(source, original_filename, mime_type):
    """Commit fully-received bytes to the media root under a UUID name, returning file metadata.

    Shared by both upload paths — ``save_file`` for a single request, ``complete`` for
    a chunked transfer — so UUID naming, HEIC conversion, EXIF orientation, capture
    date and thumbnailing are decided once. ``source`` is either a Werkzeug
    FileStorage or the path of a file already whole on disk; only how the bytes are
    read differs, and everything downstream of that works off the stored file.

    Returns ``(stored_name, file_size, has_thumbnail, taken_at)``.
    """
    ext = Path(original_filename).suffix.lower()
    is_heic = ext in ('.heic', '.heif') or mime_type in ('image/heic', 'image/heif')

    if is_heic:
        ext = '.jpg'

    stored_name = f"{uuid.uuid4()}{ext}"
    upload_dir = Path(current_app.config['UPLOAD_FOLDER'])
    upload_dir.mkdir(parents=True, exist_ok=True)
    save_path = upload_dir / stored_name
    taken_at = None

    if is_heic:
        # The one case that cannot be a rename: browsers do not render HEIC, so the
        # pixels genuinely have to be re-encoded rather than relocated.
        with _open_source_image(source) as img:
            taken_at = extract_exif_taken_at(img)
            img = ImageOps.exif_transpose(img)
            img = img.convert('RGB')
            img.save(str(save_path), 'JPEG', quality=92)
    else:
        _place_source(source, save_path)

    file_size = save_path.stat().st_size
    has_thumbnail = False

    if mime_type in ALLOWED_PHOTO_TYPES:
        try:
            thumb_path = upload_dir / f"thumb_{stored_name}"
            with Image.open(str(save_path)) as img:
                if taken_at is None:
                    taken_at = extract_exif_taken_at(img)
                img = ImageOps.exif_transpose(img)
                img = img.convert('RGB')
                img.thumbnail((400, 400), Image.LANCZOS)
                img.save(str(thumb_path), 'JPEG', quality=85)
            has_thumbnail = True
        except Exception:
            pass

    return stored_name, file_size, has_thumbnail, taken_at


def save_file(file_storage, mime_type):
    """Save file with a UUID name and generate thumbnail if image.
    HEIC/HEIF files are converted to JPEG for browser compatibility."""
    return store_upload(file_storage, file_storage.filename, mime_type)


def is_safe_redirect(target):
    """Return True only if target is a relative URL on the same host."""
    if not target:
        return False
    ref = urlparse(request.host_url)
    test = urlparse(urljoin(request.host_url, target))
    return test.scheme in ('http', 'https') and ref.netloc == test.netloc


def delete_photo_files(photo):
    """Delete the stored file and its thumbnail from disk, silently skipping any that don't exist."""
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
    print(session.query(User).all())

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