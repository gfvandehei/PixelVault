"""Media serving — the narrow surfaces, held to the same rule as the wide ones.

Every file leaves the app through one of the three endpoints below, and all three
ask ``share.may_read_album``: owner, contributor, or holder of an ``AlbumAccess``
grant. A share token in the URL says *which* album and photo are meant; it never
says who may have them.

That rule replaces an older one in which a media fetch could hand out access.
``serve_share_media`` took no login and wrote the token into the caller's session on
the way out, so a single leaked media URL — a thing that appears in browser history,
in ``Referer`` on any outbound link, in screenshots, in proxy logs, and in the
``full_url`` of every photo listing already shared — promoted an anonymous stranger
into the state ``serve_media`` and the photo API both treated as authorisation, and
with it the whole album (#40). Nothing here writes to the session now, and a leaked
URL is worth at most the one file it names, to a caller who already has both an
account and a grant.
"""

from pathlib import Path

from flask import abort, send_from_directory, make_response
from flask_login import login_required

from ..extensions import db
from ..models import Album, Photo
from .share import may_read_album

_CACHE_HEADERS = 'private, max-age=31536000, immutable'


def _cached(response):
    response.headers['Cache-Control'] = _CACHE_HEADERS
    return response


def _photo_for(filename, album_id=None):
    """Return the Photo behind a requested filename, or 404.

    ``thumb_`` is a naming convention on disk, not a row of its own: a thumbnail
    belongs to the same Photo as its original, and therefore to the same album and
    the same access decision.
    """
    stored_name = filename.replace('thumb_', '', 1) if filename.startswith('thumb_') else filename
    criteria = {'stored_filename': stored_name}
    if album_id is not None:
        criteria['album_id'] = album_id
    photo = db.session.query(Photo).filter_by(**criteria).one_or_none()
    if photo is None:
        abort(404)
    return photo


def _serve(app, filename):
    upload_dir = Path(app.config['UPLOAD_FOLDER']).resolve()
    return _cached(make_response(send_from_directory(str(upload_dir), filename)))


def register(app):

    @app.route('/media/<path:filename>')
    @login_required
    def serve_media(filename):
        """
        Serve a media file to the album's owner, the photo's uploader, or a grant holder.

        This is the endpoint every rendered album page points at, guests included,
        which is why its URL carries no token — and it accepts none. Authorisation is
        the caller's ``AlbumAccess`` row, minted when they opened the album through a
        share link. It used to also accept ``session['album_access_token']``; that key
        no longer exists anywhere in the app, because it was a capability living in a
        cookie its holder could decode (#35) and reachable without an account (#40).
        """
        if '/' in filename or '..' in filename:
            abort(400)

        photo = _photo_for(filename)
        album = db.session.get(Album, photo.album_id)
        if album is None or not may_read_album(album, photo):
            abort(403)

        return _serve(app, filename)

    @app.route('/share/<token>/media/<path:filename>')
    @login_required
    def serve_share_media(token, filename):
        """
        Serve one media file to a grant holder, named by the album's upload share token.

        Kept as its own endpoint because ``api_album_photos`` hands token-scoped URLs
        to share-link visitors, but it grants nothing and leaves no trace in the
        session: an account and a grant are required exactly as on ``/media/``, and
        the token only scopes the photo lookup to this album.
        """
        if '/' in filename or '..' in filename:
            abort(400)
        album = db.session.query(Album).filter_by(token=token).one_or_none()
        if album is None:
            abort(404)
        photo = _photo_for(filename, album_id=album.id)
        if not may_read_album(album, photo):
            abort(403)
        return _serve(app, filename)

    @app.route('/view/<view_token>/media/<path:filename>')
    @login_required
    def serve_view_media(view_token, filename):
        """Serve one media file to a grant holder, named by the album's view-only share token."""
        if '/' in filename or '..' in filename:
            abort(400)
        album = db.session.query(Album).filter_by(view_token=view_token).one_or_none()
        if album is None:
            abort(404)
        photo = _photo_for(filename, album_id=album.id)
        if not may_read_album(album, photo):
            abort(403)
        return _serve(app, filename)
