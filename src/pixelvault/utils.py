import os
import tempfile
import uuid
import warnings
import zipfile
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse, urljoin

import magic
from flask import request, abort, current_app, send_file
from flask_login import current_user
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass  # HEIC support unavailable if pillow-heif not installed

from .config import ALLOWED_PHOTO_TYPES, ALLOWED_MIME_TYPES, ALLOWED_EXTENSIONS


# The decompression-bomb ceiling, in pixels.
#
# Pillow ships a 89,478,485 px limit, but between that and *twice* it Pillow only
# emits a DecompressionBombWarning and decodes the image anyway. A 173 KB PNG
# declaring 20000x8900 (178 Mpx) sits deliberately inside that window and costs
# ~1 GB of RSS to decode; with the 2 workers x 4 threads Gunicorn runs in
# production, eight of them at once are enough to get the container OOM-killed.
#
# 50 Mpx is chosen because it clears every camera a photo album realistically
# sees at full resolution — 48 MP iPhone (8064x6048), 50 MP Sony A1,
# 45 MP Canon R5, 61 MP A7R V downsampled — while capping a single decode at
# roughly 150 MB of RGB pixel data, so even the full 8-way fan-out cannot be
# driven much past 1 GB.
MAX_IMAGE_PIXELS = 50_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# Closes the warn-and-decode window: past the ceiling Pillow now raises instead of
# printing a warning and allocating the raster regardless. Belt and braces only —
# the explicit `_guard_image_dimensions` check below does not depend on the warnings
# filter, which any caller (pytest included) is free to reset.
warnings.filterwarnings('error', category=Image.DecompressionBombWarning)


class ImageTooLargeError(ValueError):
    """Raised when an image's declared dimensions exceed :data:`MAX_IMAGE_PIXELS`.

    A ``ValueError``, not an ``abort``: the upload routes turn a failed store into a
    per-file entry in the ``results`` envelope, so a bomb is reported like any other
    rejected file rather than as a 500.
    """


def _guard_image_dimensions(source):
    """Reject an image whose pixel count exceeds the ceiling, before anything decodes it.

    ``Image.open`` parses the header without allocating the raster, so the cost of
    this check is bounded by the file's metadata no matter how large the image claims
    to be. That ordering is the whole point: a dimension check performed after
    ``convert``, ``exif_transpose`` or ``thumbnail`` has already paid the gigabyte.

    Note this cannot be left to the thumbnail branch's ``except Exception: pass``
    below — that swallow would turn a rejected bomb into a silently committed file
    with no thumbnail, which is the opposite of what should happen.
    """
    from_stream = _is_file_storage(source)
    try:
        opened = Image.open(source.stream if from_stream else str(source))
        try:
            width, height = opened.size
            if width * height > MAX_IMAGE_PIXELS:
                raise ImageTooLargeError(
                    f"Image is too large to process: {width}x{height} is "
                    f"{width * height:,} pixels, and the limit is {MAX_IMAGE_PIXELS:,}."
                )
        finally:
            # Close only what we opened by path. Pillow closes whatever file object it
            # holds, so closing a stream-backed image would shut the FileStorage's own
            # stream and leave the caller with nothing left to save.
            if not from_stream:
                opened.close()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        # Pillow's own ceiling fires first for anything past twice the limit, before we
        # ever see .size. Re-raise as our type, with our wording: Pillow's mentions its
        # own doubled threshold and calls the file a "DOS attack", neither of which
        # helps someone who simply photographed a very large panorama.
        raise ImageTooLargeError(
            f"Image is too large to process: the limit is {MAX_IMAGE_PIXELS:,} pixels."
        ) from exc
    finally:
        if from_stream:
            # The caller still has to read these bytes; put the cursor back.
            source.stream.seek(0)


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
    # Decided by the DETECTED mime type alone. Trusting the extension here let a
    # client choose its own code path: naming a 178 Mpx PNG ".heic" routed it through
    # the re-encode branch below and then the thumbnail branch, doubling the cost of a
    # decompression bomb. The extension is the attacker's to pick; the sniffed type is not.
    is_heic = mime_type in ('image/heic', 'image/heif')

    if is_heic:
        ext = '.jpg'

    # Before any decode, and before the bytes are placed: a rejected image must leave
    # nothing behind in the media root.
    if mime_type in ALLOWED_PHOTO_TYPES:
        _guard_image_dimensions(source)

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


# The per-user hourly budget for building an album archive, applied by the three
# download routes (owner, upload-share, view-share). Declared here rather than
# spelled out three times in two route modules, so the number and the reasoning
# for it cannot drift apart.
#
# An album download is the most expensive thing one request can ask this app to
# do: read every byte of the album off the media volume, DEFLATE it, write the
# archive back to that same volume, then stream it out. build_album_zip has taken
# memory out of the picture, but the transient disk and the CPU are still linear
# in the album's size, and with --workers 2 --threads 4 eight of them run at once.
# Unlimited — which is what these routes were, since the 200/hour default is not a
# budget for gigabyte responses — a loop over one share link fills the upload
# volume and starves the uploads sharing it.
#
# Ten, keyed by user id: rate_limit_key resolves an authenticated caller to
# `user:<id>`, so this is a real per-account budget and not a bucket shared by
# everyone behind one NAT. Sized for a human clicking a link. Downloading an album
# is a once-a-trip action, and ten leaves room for the phone-on-hotel-wifi case
# where a multi-gigabyte transfer dies and is retried several times. Flask-Limiter
# scopes a limit per endpoint, so the worst honest hour from one account is thirty
# archives across the three routes — still far below what it takes to hurt the
# volume, while a hostile client is capped at ten album-sized stagings an hour
# instead of as many as it can open connections for.
ALBUM_ZIP_RATE_LIMIT = "10 per hour"


# Sub-directory of UPLOAD_FOLDER that album archives are assembled in.
#
# UPLOAD_FOLDER and the instance directory are the only paths the production
# container's non-root user can write to, and UPLOAD_FOLDER is the one sized for
# media-scale bytes — so an archive of the media has to be staged beside the media.
# A sub-directory rather than the folder itself, for the same reason `partials`
# is one: /media/<filename> serves out of the top level by name, and nothing
# half-written should ever be reachable there.
ZIP_STAGING_SUBDIR = 'tmp'


def build_album_zip(album):
    """Assemble the album's ZIP into an unlinked temp file and return the open handle.

    This used to build into an ``io.BytesIO`` and hand that to ``send_file``, which
    meant peak RSS tracked the album's on-disk size for the whole life of the
    response — videos are stored uncompressed and DEFLATE will not shrink them, so
    the buffer *is* the album. Production runs ``--workers 2 --threads 4``: eight
    concurrent downloads of one multi-gigabyte album is an OOM kill of the container,
    which takes down uploads, browsing and login along with the download (issue #41).
    Memory here is now bounded by ``zipfile``'s own copy buffer plus the socket's,
    regardless of how large the album is.

    **The tradeoff taken.** A temp file trades a memory problem for a disk one; a
    streaming generator (``zipstream``-style, yielding the archive as it is produced)
    would trade it for a protocol one. The file won because:

    * the archive's size is known before the first byte goes out, so the response
      keeps a real ``Content-Length`` — a generated response cannot have one, and
      without it nginx buffers or chunk-encodes, browsers lose the progress bar and
      the download-resume story gets worse, not better;
    * an exception halfway through zipping is still a clean 500. A streamed response
      has already sent ``200 OK``, so the same failure arrives at the client as a
      truncated but apparently successful ZIP;
    * ``zipfile`` incrementally deflating to a real file is stdlib; incrementally
      deflating to a generator is not.

    The cost is disk headroom: the archive is staged on the *same* volume as the
    media it copies, so a download transiently needs about as much free space as the
    album occupies, and the rate limits on the three download routes bound how many
    such stagings can pile up. Nothing yet refuses an album that is too large to
    stage — see the recommendation in issue #41.

    **Cleanup is a kernel guarantee, not a teardown hook.** ``tempfile.TemporaryFile``
    unlinks the file the instant it is created (on Linux it may never get a directory
    entry at all), so the bytes are reclaimed when the last descriptor closes and
    there is no name for anything to leak. A worker killed mid-download, an exception
    between here and ``send_file``, a client that disconnects halfway — all of them
    release the space, where an ``@after_this_request`` unlink would only cover the
    first of those. The descriptor is closed by the WSGI server when it closes the
    response iterator; the ``except`` below covers the window before it becomes one.

    Returns the open, rewound file object. Callers that want a response should use
    :func:`send_album_zip` rather than assembling the headers themselves.
    """
    upload_dir = Path(current_app.config['UPLOAD_FOLDER'])
    staging = upload_dir / ZIP_STAGING_SUBDIR
    staging.mkdir(parents=True, exist_ok=True)

    handle = tempfile.TemporaryFile(dir=staging, prefix='album-', suffix='.zip')
    try:
        seen = {}
        # Exiting this `with` writes the central directory and leaves `handle`
        # open: ZipFile only closes descriptors it opened itself, and this one was
        # passed in. That is what lets the finished archive be returned as a
        # still-open file rather than reopened by a name it does not have.
        with zipfile.ZipFile(handle, 'w', zipfile.ZIP_DEFLATED) as zf:
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
                # zf.write streams the source file through a fixed-size buffer; it
                # never holds a whole member in memory, which is the other half of
                # why this function's footprint no longer scales with the album.
                zf.write(str(src), name)
    except BaseException:
        handle.close()
        raise

    handle.seek(0)
    return handle


def send_album_zip(album):
    """Return a file-download response carrying a ZIP of the album.

    Lives here rather than in the three routes that need it (owner, upload-share,
    view-share) so the archive's memory behaviour, its ``Content-Length`` and its
    filename are decided once. The routes' job is authorisation; this is plumbing.

    ``send_file`` only derives a length when it is handed a *path*, and the whole
    point of :func:`build_album_zip` is that there is no path — so the length is
    taken from the descriptor with ``fstat`` and set explicitly. Without that the
    response falls back to chunked encoding and the browser shows an unbounded
    progress bar for a download that may run into gigabytes.

    ``conditional=False`` because there is nothing to be conditional about: the
    archive is rebuilt per request and has no stable identity, so advertising
    ``Accept-Ranges`` would promise a resumability that does not exist.
    """
    handle = build_album_zip(album)
    try:
        size = os.fstat(handle.fileno()).st_size
        zip_name = (secure_filename(album.name or 'album') or 'album') + '.zip'
        response = send_file(
            handle,
            mimetype='application/zip',
            as_attachment=True,
            download_name=zip_name,
            conditional=False,
        )
    except BaseException:
        handle.close()
        raise
    response.content_length = size
    return response

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