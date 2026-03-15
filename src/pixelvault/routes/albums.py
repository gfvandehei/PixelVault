from flask import render_template, request, redirect, url_for, flash, abort, jsonify
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from ..extensions import db, limiter
from ..models import Album, Photo
from ..utils import delete_photo_files, build_album_zip
from flask import send_file
from sqlalchemy.exc import NoResultFound


def register(app):

    @app.route('/dashboard')
    @login_required
    def dashboard():
        albums = db.session.query(Album).filter_by(owner_id=current_user.id).order_by(Album.created_at.desc()).all()

        contributed_ids = db.session.query(Photo.album_id).filter(
            Photo.uploader_id == current_user.id
        ).distinct().subquery()
        contributed = db.session.query(Album).filter(
            Album.id.in_(contributed_ids),
            Album.owner_id != current_user.id
        ).order_by(Album.created_at.desc()).all()

        return render_template('dashboard.html', albums=albums, contributed=contributed)

    @app.route('/album/create', methods=['GET', 'POST'])
    @login_required
    @limiter.limit("30 per hour")
    def create_album():
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            description = request.form.get('description', '').strip()
            allow_anonymous = request.form.get('allow_anonymous') == 'on'
            allow_upload = request.form.get('allow_upload') == 'on'

            if not name:
                flash('Album name is required.', 'error')
                return render_template('create_album.html')

            album = Album(
                name=name,
                description=description,
                owner_id=current_user.id,
                allow_anonymous=allow_anonymous,
                allow_upload=allow_upload,
            )
            db.session.add(album)
            db.session.commit()
            flash(f'Album "{name}" created!', 'success')
            return redirect(url_for('album_view', token=album.token))

        return render_template('create_album.html')

    @app.route('/album/<token>')
    @login_required
    def album_view(token):
        try:
            album = db.session.query(Album).filter_by(token=token).one()
        except NoResultFound:
            abort(404)
        if album.owner_id != current_user.id:
            abort(403)
        photos = db.session.query(Photo).filter_by(album_id=album.id).order_by(Photo.uploaded_at.desc()).all()
        share_url = url_for('album_upload', token=token, _external=True)
        view_share_url = url_for('album_view_only', view_token=album.view_token, _external=True) if album.view_token else None
        return render_template('album_view.html', album=album, photos=photos,
                               share_url=share_url, view_share_url=view_share_url)

    @app.route('/album/<token>/delete', methods=['POST'])
    @login_required
    def delete_album(token):
        try:
            album = db.session.query(Album).filter_by(token=token).one()
        except NoResultFound:
            abort(404)
        if album.owner_id != current_user.id:
            abort(403)
        for photo in album.photos:
            delete_photo_files(photo)
        db.session.delete(album)
        db.session.commit()
        flash('Album deleted.', 'info')
        return redirect(url_for('dashboard'))

    @app.route('/photo/<int:photo_id>/delete', methods=['POST'])
    @login_required
    def delete_photo(photo_id):
        photo = db.session.get(Photo, photo_id)
        if not photo:
            abort(404)
        album = db.session.get(Album, photo.album_id)
        if album.owner_id != current_user.id:
            abort(403)
        delete_photo_files(photo)
        db.session.delete(photo)
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/album/<token>/download')
    @login_required
    def download_album(token):
        try:
            album = db.session.query(Album).filter_by(token=token).one()
        except NoResultFound:
            abort(404)
        if album.owner_id != current_user.id:
            abort(403)
        buf = build_album_zip(album)
        zip_name = secure_filename(album.name or 'album') + '.zip'
        return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=zip_name)

    @app.route('/album/<token>/settings', methods=['POST'])
    @login_required
    def album_settings(token):
        try:
            album = db.session.query(Album).filter_by(token=token).one()
        except NoResultFound:
            abort(404)
        if album.owner_id != current_user.id:
            abort(403)
        album.allow_upload = request.form.get('allow_upload') == 'on'
        db.session.commit()
        flash('Album settings updated.', 'success')
        return redirect(url_for('album_view', token=token))
