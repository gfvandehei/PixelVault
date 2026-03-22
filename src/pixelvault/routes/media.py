from pathlib import Path

from flask import abort, session, send_from_directory, make_response
from flask_login import login_required, current_user

from ..extensions import db
from ..models import Album, Photo

_CACHE_HEADERS = 'private, max-age=31536000, immutable'


def _cached(response):
    response.headers['Cache-Control'] = _CACHE_HEADERS
    return response


def register(app):

    @app.route('/media/<path:filename>')
    def serve_media(filename):
        """
        Serve a media file to an authenticated album owner or a session-authorised share visitor.

        Access is granted if the current user owns the album or uploaded the photo, or if their
        session holds the album's upload token. Returns 403 otherwise.
        """
        if '/' in filename or '..' in filename:
            abort(400)

        stored_name = filename.replace('thumb_', '', 1) if filename.startswith('thumb_') else filename
        photo = db.session.query(Photo).filter_by(stored_filename=stored_name).one_or_none()
        if photo is None:
            abort(404)
        album = db.session.get(Album, photo.album_id)

        if current_user.is_authenticated and (
            album.owner_id == current_user.id or photo.uploader_id == current_user.id
        ):
            pass
        else:
            allowed_token = session.get('album_access_token')
            if allowed_token != album.token:
                abort(403)

        upload_dir = Path(app.config['UPLOAD_FOLDER']).resolve()
        return _cached(make_response(send_from_directory(str(upload_dir), filename)))

    @app.route('/share/<token>/media/<path:filename>')
    def serve_share_media(token, filename):
        """
        Serve a media file to anyone with a valid upload share token.

        Also writes the token into the session so subsequent requests to serve_media
        are authorised without requiring the token in the URL each time.
        """
        if '/' in filename or '..' in filename:
            abort(400)
        album = db.session.query(Album).filter_by(token=token).one_or_none()
        if album is None:
            abort(404)
        stored_name = filename.replace('thumb_', '', 1) if filename.startswith('thumb_') else filename
        photo = db.session.query(Photo).filter_by(stored_filename=stored_name, album_id=album.id).one_or_none()
        if photo is None:
            abort(404)
        session['album_access_token'] = token
        upload_dir = Path(app.config['UPLOAD_FOLDER']).resolve()
        return _cached(make_response(send_from_directory(str(upload_dir), filename)))

    @app.route('/view/<view_token>/media/<path:filename>')
    @login_required
    def serve_view_media(view_token, filename):
        """Serve a media file to an authenticated user accessing the album via its view-only share token."""
        if '/' in filename or '..' in filename:
            abort(400)
        album = db.session.query(Album).filter_by(view_token=view_token).one_or_none()
        if album is None:
            abort(404)
        stored_name = filename.replace('thumb_', '', 1) if filename.startswith('thumb_') else filename
        photo = db.session.query(Photo).filter_by(stored_filename=stored_name, album_id=album.id).one_or_none()
        if photo is None:
            abort(404)
        upload_dir = Path(app.config['UPLOAD_FOLDER']).resolve()
        return _cached(make_response(send_from_directory(str(upload_dir), filename)))
