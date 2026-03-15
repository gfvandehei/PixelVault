from flask import render_template, request, url_for, abort, jsonify, send_file
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from ..extensions import db, limiter
from ..models import Album, Photo
from ..utils import validate_file, save_file, build_album_zip


def register(app):

    @app.route('/share/<token>', methods=['GET'])
    @login_required
    def album_upload(token):
        album = db.session.query(Album).filter_by(token=token).one_or_none()
        if album is None:
            abort(404)
        return render_template('album_upload.html', album=album,
            read_only=False,
            api_url=url_for('api_album_photos', token=token),
            upload_url=url_for('do_upload', token=token),
            download_url=url_for('download_album_share', token=token))

    @app.route('/view/<view_token>', methods=['GET'])
    @login_required
    def album_view_only(view_token):
        album = db.session.query(Album).filter_by(view_token=view_token).one_or_none()
        if album is None:
            abort(404)
        return render_template('album_upload.html', album=album,
            read_only=True,
            api_url=url_for('api_album_view_photos', view_token=view_token),
            upload_url=None,
            download_url=url_for('download_album_view', view_token=view_token))

    @app.route('/share/<token>/upload', methods=['POST'])
    @login_required
    @limiter.limit("60 per hour")
    def do_upload(token):
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
                stored_name, file_size, has_thumbnail = save_file(file, mime_type)
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
            )
            db.session.add(photo)
            results.append({'filename': file.filename, 'success': True})

        db.session.commit()
        return jsonify({'results': results})

    @app.route('/share/<token>/download')
    @login_required
    def download_album_share(token):
        album = db.session.query(Album).filter_by(token=token).one_or_none()
        if album is None:
            abort(404)
        buf = build_album_zip(album)
        zip_name = secure_filename(album.name or 'album') + '.zip'
        return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=zip_name)

    @app.route('/view/<view_token>/download')
    @login_required
    def download_album_view(view_token):
        album = db.session.query(Album).filter_by(view_token=view_token).one_or_none()
        if album is None:
            abort(404)
        buf = build_album_zip(album)
        zip_name = secure_filename(album.name or 'album') + '.zip'
        return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=zip_name)
