from flask import render_template, request, redirect, url_for, flash, abort, jsonify, session
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from ..extensions import db, limiter
from ..models import Album, Photo, AlbumAccess
from ..utils import delete_photo_files, build_album_zip
from flask import send_file
from sqlalchemy.exc import NoResultFound


def register(app):

    @app.route('/dashboard')
    @login_required
    def dashboard():
        """
        Render the user's dashboard.

        Shows albums the current user owns, plus any albums they have contributed
        uploads to (but don't own).
        """
        albums = db.session.query(Album).filter_by(owner_id=current_user.id).order_by(Album.created_at.desc()).all()

        # Collect album IDs from both uploads and share-link visits
        contributed_ids = {row.album_id for row in db.session.query(Photo.album_id).filter(
            Photo.uploader_id == current_user.id
        ).distinct()}
        accessed_ids = {row.album_id for row in db.session.query(AlbumAccess.album_id).filter(
            AlbumAccess.user_id == current_user.id
        )}
        guest_ids = list(contributed_ids | accessed_ids)
        shared = db.session.query(Album).filter(
            Album.id.in_(guest_ids),
            Album.owner_id != current_user.id
        ).order_by(Album.created_at.desc()).all() if guest_ids else []

        return render_template('dashboard.html', albums=albums, shared=shared)

    @app.route('/album/create', methods=['GET', 'POST'])
    @login_required
    @limiter.limit("30 per hour")
    def create_album():
        """
        Handle album creation.

        GET  — render the creation form.
        POST — create a new album owned by the current user and redirect to its view page.
        """
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
    def album_view(token):
        """Render the album view. Owners see full management UI; guests with the share token see gallery and upload. Unauthenticated visitors see the request-permission page."""
        if not current_user.is_authenticated:
            return render_template('request_permission.html')

        try:
            album = db.session.query(Album).filter_by(token=token).one()
        except NoResultFound:
            abort(404)

        is_owner = album.owner_id == current_user.id
        photos = db.session.query(Photo).filter_by(album_id=album.id).order_by(Photo.uploaded_at.desc()).all()

        if is_owner:
            share_url = url_for('album_view', token=token, _external=True)
            view_share_url = url_for('album_view_only', view_token=album.view_token, _external=True) if album.view_token else None
            download_url = url_for('download_album', token=token)
            can_upload = True
            accesses = db.session.query(AlbumAccess).filter_by(album_id=album.id).all()
        else:
            # Grant media access for this session so serve_media works for the guest
            session['album_access_token'] = album.token
            share_url = None
            view_share_url = None
            download_url = url_for('download_album_share', token=token)
            accesses = []
            # On first visit, set access_type based on which link was used
            access_record = db.session.query(AlbumAccess).filter_by(
                user_id=current_user.id, album_id=album.id
            ).first()
            if not access_record:
                came_via_upload = session.get('album_upload_token') == album.token
                access_record = AlbumAccess(
                    user_id=current_user.id, album_id=album.id,
                    access_type='upload' if came_via_upload else 'view'
                )
                db.session.add(access_record)
                db.session.commit()
            can_upload = album.allow_upload and access_record.access_type == 'upload'

        return render_template('album_view.html', album=album, photos=photos,
                               share_url=share_url, view_share_url=view_share_url,
                               download_url=download_url, is_owner=is_owner,
                               can_upload=can_upload, accesses=accesses)

    @app.route('/album/<token>/access/<int:user_id>', methods=['POST'])
    @login_required
    def set_album_access(token, user_id):
        """Allow the album owner to change a guest's access type between 'upload' and 'view'."""
        try:
            album = db.session.query(Album).filter_by(token=token).one()
        except NoResultFound:
            abort(404)
        if album.owner_id != current_user.id:
            abort(403)
        access_record = db.session.query(AlbumAccess).filter_by(
            user_id=user_id, album_id=album.id
        ).first()
        if access_record is None:
            abort(404)
        new_type = request.form.get('access_type')
        if new_type not in ('upload', 'view'):
            abort(400)
        access_record.access_type = new_type
        db.session.commit()
        return redirect(url_for('album_view', token=token))

    @app.route('/album/<token>/delete', methods=['POST'])
    @login_required
    def delete_album(token):
        """Permanently delete an album and all of its uploaded files from disk and the database."""
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
        """Delete a single photo from disk and the database. Only the album owner may delete photos. Returns JSON."""
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
        """Stream a ZIP archive of all photos in the album to the owner as a file download."""
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
        """Update album settings (currently the allow_upload toggle) and redirect back to the album view."""
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
