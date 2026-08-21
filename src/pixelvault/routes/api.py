"""JSON photo listings for the gallery, authorised on the grant like everything else.

Both endpoints answer with an album's whole index, media URLs included, so both ask
``share.may_read_album`` — the same question ``routes/media.py`` asks about a single
file. A listing that were easier to obtain than one photo would simply be the way to
obtain every photo.
"""

from flask import jsonify, abort, url_for
from flask_login import login_required, current_user

from ..models import Album, Photo
from ..extensions import db
from .share import may_read_album


def _listing(photo, thumb_url, full_url):
    return {
        'id': photo.id,
        'original_filename': photo.original_filename,
        'uploader_name': photo.uploader_name,
        'uploaded_at': photo.uploaded_at.strftime('%b %d, %Y %H:%M'),
        'file_size': photo.file_size_human,
        'is_video': photo.is_video,
        'thumb_url': thumb_url,
        'full_url': full_url,
    }


def register(app):

    @app.route('/api/album/<token>/photos')
    @login_required
    def api_album_photos(token):
        """
        Return a JSON array of photo metadata for an album, named by its upload share token.

        Accessible to the album owner and to anyone holding an ``AlbumAccess`` grant on
        the album; everyone else gets 403. The check used to read::

            if not current_user.is_authenticated or album.owner_id != current_user.id:
                if session.get('album_access_token') != token and not current_user.is_authenticated:
                    abort(403)

        — where the trailing clause made the inner test unreachable for any logged-in
        caller, so possession of the token was the only thing an authenticated stranger
        needed for the full index and a set of working media URLs (#39). Media URLs are
        scoped to the endpoint that matches how the caller reached the album.
        """
        album = db.session.query(Album).filter_by(token=token).one_or_none()
        if album is None:
            abort(404)
        if not may_read_album(album):
            abort(403)

        is_owner = album.owner_id == current_user.id
        photos = db.session.query(Photo).filter_by(album_id=album.id).order_by(Photo.uploaded_at.desc()).all()
        result = []
        for p in photos:
            thumb_name = p.thumbnail_filename if p.has_thumbnail else p.stored_filename
            if is_owner:
                thumb_url = url_for('serve_media', filename=thumb_name)
                full_url = url_for('serve_media', filename=p.stored_filename)
            else:
                thumb_url = url_for('serve_share_media', token=token, filename=thumb_name)
                full_url = url_for('serve_share_media', token=token, filename=p.stored_filename)
            result.append(_listing(p, thumb_url, full_url))
        return jsonify(result)

    @app.route('/api/view/<view_token>/photos')
    @login_required
    def api_album_view_photos(view_token):
        """Return a JSON array of photo metadata for an album, named by its view-only share token.

        The grant check is not inherited from ``@login_required``: being signed in says
        nothing about this album, and without it any account holding a view URL — or a
        listing that quoted one — read the whole index.
        """
        album = db.session.query(Album).filter_by(view_token=view_token).one_or_none()
        if album is None:
            abort(404)
        if not may_read_album(album):
            abort(403)
        photos = db.session.query(Photo).filter_by(album_id=album.id).order_by(Photo.uploaded_at.desc()).all()
        result = []
        for p in photos:
            thumb_name = p.thumbnail_filename if p.has_thumbnail else p.stored_filename
            result.append(_listing(
                p,
                url_for('serve_view_media', view_token=view_token, filename=thumb_name),
                url_for('serve_view_media', view_token=view_token, filename=p.stored_filename),
            ))
        return jsonify(result)
