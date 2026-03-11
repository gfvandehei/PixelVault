from flask import jsonify, abort, session, url_for
from flask_login import login_required, current_user

from ..models import Album, Photo


def register(app):

    @app.route('/api/album/<token>/photos')
    def api_album_photos(token):
        album = Album.query.filter_by(token=token).first_or_404()

        if not current_user.is_authenticated or album.owner_id != current_user.id:
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

    @app.route('/api/view/<view_token>/photos')
    @login_required
    def api_album_view_photos(view_token):
        album = Album.query.filter_by(view_token=view_token).first_or_404()
        photos = Photo.query.filter_by(album_id=album.id).order_by(Photo.uploaded_at.desc()).all()
        result = []
        for p in photos:
            result.append({
                'id': p.id,
                'original_filename': p.original_filename,
                'uploader_name': p.uploader_name,
                'uploaded_at': p.uploaded_at.strftime('%b %d, %Y %H:%M'),
                'file_size': p.file_size_human,
                'is_video': p.is_video,
                'thumb_url': url_for('serve_view_media', view_token=view_token,
                                     filename=p.thumbnail_filename if p.has_thumbnail else p.stored_filename),
                'full_url': url_for('serve_view_media', view_token=view_token, filename=p.stored_filename),
            })
        return jsonify(result)
