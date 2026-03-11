from pathlib import Path

from flask import abort, session, send_from_directory
from flask_login import login_required, current_user

from ..extensions import db
from ..models import Album, Photo


def register(app):

    @app.route('/media/<path:filename>')
    def serve_media(filename):
        if '/' in filename or '..' in filename:
            abort(400)

        stored_name = filename.replace('thumb_', '', 1) if filename.startswith('thumb_') else filename
        photo = Photo.query.filter_by(stored_filename=stored_name).first_or_404()
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
        return send_from_directory(str(upload_dir), filename)

    @app.route('/share/<token>/media/<path:filename>')
    def serve_share_media(token, filename):
        if '/' in filename or '..' in filename:
            abort(400)
        album = Album.query.filter_by(token=token).first_or_404()
        stored_name = filename.replace('thumb_', '', 1) if filename.startswith('thumb_') else filename
        Photo.query.filter_by(stored_filename=stored_name, album_id=album.id).first_or_404()
        session['album_access_token'] = token
        upload_dir = Path(app.config['UPLOAD_FOLDER']).resolve()
        return send_from_directory(str(upload_dir), filename)

    @app.route('/view/<view_token>/media/<path:filename>')
    @login_required
    def serve_view_media(view_token, filename):
        if '/' in filename or '..' in filename:
            abort(400)
        album = Album.query.filter_by(view_token=view_token).first_or_404()
        stored_name = filename.replace('thumb_', '', 1) if filename.startswith('thumb_') else filename
        Photo.query.filter_by(stored_filename=stored_name, album_id=album.id).first_or_404()
        upload_dir = Path(app.config['UPLOAD_FOLDER']).resolve()
        return send_from_directory(str(upload_dir), filename)
