from flask import render_template, request, url_for, abort, jsonify, send_file, redirect, session
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from ..extensions import db, limiter
from ..models import Album, Photo, AlbumAccess
from ..utils import validate_file, save_file, build_album_zip


def register(app):

    @app.route('/share/<token>', methods=['GET'])
    def album_upload(token):
        """Redirect the share link to the album page. Sets the upload session token so the guest can upload."""
        album = db.session.query(Album).filter_by(token=token).one_or_none()
        if album is None:
            abort(404)
        session['album_upload_token'] = token
        return redirect(url_for('album_view', token=token))

    @app.route('/view/<view_token>', methods=['GET'])
    @login_required
    def album_view_only(view_token):
        """Render the view-only shared page for an album. Guests can browse photos but cannot upload."""
        album = db.session.query(Album).filter_by(view_token=view_token).one_or_none()
        if album is None:
            abort(404)
        session['album_access_token'] = album.token
        photos = db.session.query(Photo).filter_by(album_id=album.id).order_by(Photo.uploaded_at.desc()).all()
        if not db.session.query(AlbumAccess).filter_by(
            user_id=current_user.id, album_id=album.id
        ).first():
            db.session.add(AlbumAccess(user_id=current_user.id, album_id=album.id, access_type='view'))
            db.session.commit()
        return render_template('album_view.html', album=album, photos=photos,
            can_upload=False,
            is_owner=False,
            share_url=None,
            view_share_url=None,
            download_url=url_for('download_album_view', view_token=view_token))

    @app.route('/share/<token>/upload', methods=['POST'])
    @login_required
    @limiter.limit("600 per hour")
    def do_upload(token):
        """
        Accept a batch of files uploaded via the share link and save them to the album.

        Validates each file by extension and MIME type, converts HEIC images to JPEG,
        generates thumbnails, and records each file in the database. Returns a JSON
        array of per-file results indicating success or a descriptive error.
        Rejects the entire request if uploads are disabled on the album.
        """
        album = db.session.query(Album).filter_by(token=token).one_or_none()
        if album is None:
            abort(404)

        if not album.allow_upload:
            return jsonify({'error': 'Uploads are disabled for this album.'}), 403

        files = request.files.getlist('files')
        if not files:
            return jsonify({'error': 'No files provided.'}), 400

        results = []
        for file in files[:20]:
            if not file or not file.filename:
                continue

            mime_type, err = validate_file(file)
            if err:
                results.append({'filename': file.filename, 'error': err})
                continue

            try:
                stored_name, file_size, has_thumbnail, taken_at = save_file(file, mime_type)
            except Exception:
                results.append({'filename': file.filename, 'error': 'Upload failed.'})
                continue

            photo = Photo(
                album_id=album.id,
                uploader_id=current_user.id,
                uploader_name=current_user.username,
                stored_filename=stored_name,
                original_filename=secure_filename(file.filename)[:200],
                mime_type=mime_type,
                file_size=file_size,
                has_thumbnail=has_thumbnail,
                taken_at=taken_at,
            )
            db.session.add(photo)
            results.append({'filename': file.filename, 'success': True})

        if not db.session.query(AlbumAccess).filter_by(
            user_id=current_user.id, album_id=album.id
        ).first():
            db.session.add(AlbumAccess(user_id=current_user.id, album_id=album.id))
        db.session.commit()
        return jsonify({'results': results})

    @app.route('/share/<token>/download')
    @login_required
    def download_album_share(token):
        """Stream a ZIP of all album photos to a user accessing the album via its upload share link."""
        album = db.session.query(Album).filter_by(token=token).one_or_none()
        if album is None:
            abort(404)
        buf = build_album_zip(album)
        zip_name = secure_filename(album.name or 'album') + '.zip'
        return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=zip_name)

    @app.route('/view/<view_token>/download')
    @login_required
    def download_album_view(view_token):
        """Stream a ZIP of all album photos to a user accessing the album via its view-only share link."""
        album = db.session.query(Album).filter_by(view_token=view_token).one_or_none()
        if album is None:
            abort(404)
        buf = build_album_zip(album)
        zip_name = secure_filename(album.name or 'album') + '.zip'
        return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=zip_name)
