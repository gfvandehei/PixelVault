from datetime import datetime, timedelta

from flask import render_template, request, redirect, url_for, flash, abort, jsonify, session
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from ..extensions import db, limiter
from ..models import Album, Photo, AlbumAccess, User
from ..uploads import discard_sessions_for_album
from ..utils import delete_photo_files, build_album_zip
from flask import current_app, send_file
from sqlalchemy.exc import NoResultFound


def _owned_and_shared_ids(user):
    """Return (owned_album_ids, shared_album_ids) visible to the given user.

    Shared albums are ones the user contributed uploads to or visited via a
    share link, excluding anything they own.
    """
    owned_ids = {row.id for row in db.session.query(Album.id).filter_by(owner_id=user.id)}
    contributed_ids = {row.album_id for row in db.session.query(Photo.album_id).filter(
        Photo.uploader_id == user.id
    ).distinct()}
    accessed_ids = {row.album_id for row in db.session.query(AlbumAccess.album_id).filter(
        AlbumAccess.user_id == user.id
    )}
    shared_ids = (contributed_ids | accessed_ids) - owned_ids
    return owned_ids, shared_ids


def register(app):

    @app.route('/dashboard')
    @login_required
    def dashboard():
        """
        Render the user's dashboard.

        Shows albums the current user owns, plus any albums they have contributed
        uploads to (but don't own). Supports filtering by owner(s), contributor(s),
        a taken-date range, a name search, and an owned/shared/all scope, all via
        query params so results stay bookmarkable.
        """
        owner_ids = [int(v) for v in request.args.getlist('owners') if v.isdigit()]
        contributor_ids = [int(v) for v in request.args.getlist('contributors') if v.isdigit()]
        start_date = request.args.get('start_date', '').strip()
        end_date = request.args.get('end_date', '').strip()
        q = request.args.get('q', '').strip()
        scope = request.args.get('scope', 'all')
        if scope not in ('owned', 'shared', 'all'):
            scope = 'all'

        owned_ids, shared_ids = _owned_and_shared_ids(current_user)
        if scope == 'owned':
            visible_ids = owned_ids
        elif scope == 'shared':
            visible_ids = shared_ids
        else:
            visible_ids = owned_ids | shared_ids

        albums, shared = [], []
        if visible_ids:
            query = db.session.query(Album).filter(Album.id.in_(visible_ids))

            if owner_ids:
                query = query.filter(Album.owner_id.in_(owner_ids))

            if contributor_ids:
                contributor_album_ids = db.session.query(Photo.album_id).filter(
                    Photo.uploader_id.in_(contributor_ids)
                ).distinct()
                query = query.filter(Album.id.in_(contributor_album_ids))

            if start_date or end_date:
                photo_ids = db.session.query(Photo.album_id)
                try:
                    if start_date:
                        photo_ids = photo_ids.filter(Photo.taken_at >= datetime.strptime(start_date, '%Y-%m-%d'))
                    if end_date:
                        photo_ids = photo_ids.filter(
                            Photo.taken_at < datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
                        )
                    query = query.filter(Album.id.in_(photo_ids.distinct()))
                except ValueError:
                    pass

            if q:
                query = query.filter(Album.name.ilike(f'%{q}%'))

            results = query.order_by(Album.created_at.desc()).all()
            albums = [a for a in results if a.id in owned_ids]
            shared = [a for a in results if a.id not in owned_ids]

        filters = {
            'owners': owner_ids,
            'contributors': contributor_ids,
            'start_date': start_date,
            'end_date': end_date,
            'q': q,
            'scope': scope,
        }
        return render_template('dashboard.html', albums=albums, shared=shared, filters=filters)

    @app.route('/api/dashboard/filter-options')
    @login_required
    def dashboard_filter_options():
        """Return the owners, contributors, and taken-date bounds available for the current user's dashboard filters."""
        owned_ids, shared_ids = _owned_and_shared_ids(current_user)
        visible_ids = owned_ids | shared_ids
        if not visible_ids:
            return jsonify({'owners': [], 'contributors': [], 'min_date': None, 'max_date': None})

        owners = db.session.query(User.id, User.username).join(Album, Album.owner_id == User.id).filter(
            Album.id.in_(visible_ids)
        ).distinct().order_by(User.username).all()

        contributors = db.session.query(User.id, User.username).join(Photo, Photo.uploader_id == User.id).filter(
            Photo.album_id.in_(visible_ids)
        ).distinct().order_by(User.username).all()

        bounds = db.session.query(
            db.func.min(Photo.taken_at), db.func.max(Photo.taken_at)
        ).filter(Photo.album_id.in_(visible_ids)).one()

        return jsonify({
            'owners': [{'id': u.id, 'username': u.username} for u in owners],
            'contributors': [{'id': u.id, 'username': u.username} for u in contributors],
            'min_date': bounds[0].strftime('%Y-%m-%d') if bounds[0] else None,
            'max_date': bounds[1].strftime('%Y-%m-%d') if bounds[1] else None,
        })

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
        """Render the album view. Owners see full management UI; guests with the share token see gallery and upload. Unauthenticated visitors see the request-permission page.

        This is where a share token is spent: the non-owner branch mints the
        ``AlbumAccess`` grant that every later request is judged against, with its
        ``access_type`` fixed by which of the album's two links the visitor followed.
        Nothing is written to the session — the grant is a server-side row precisely
        so that no capability travels in a cookie the holder can read (see the module
        docstring in routes/share.py).
        """
        if not current_user.is_authenticated:
            return render_template('request_permission.html')

        try:
            album = db.session.query(Album).filter_by(token=token).one()
        except NoResultFound:
            abort(404)

        is_owner = album.owner_id == current_user.id
        photos = db.session.query(Photo).filter_by(album_id=album.id).order_by(Photo.uploaded_at.desc()).all()

        if is_owner:
            share_url = url_for('album_upload', token=token, _external=True)
            view_share_url = url_for('album_view_only', view_token=album.view_token, _external=True) if album.view_token else None
            download_url = url_for('download_album', token=token)
            can_upload = True
            accesses = db.session.query(AlbumAccess).filter_by(album_id=album.id).all()
        else:
            # No session write here. serve_media authorises the guest on the
            # AlbumAccess row minted below, which is the same fact stored where the
            # guest cannot read or replay it.
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
        """Allow the album owner to change a guest's access type between 'upload' and 'view'.

        The row this writes is enforced server-side by ``share.may_upload_to`` on every
        upload endpoint, so a downgrade revokes the capability rather than only hiding
        the widget (#38). What it does *not* do is invalidate the link: a guest who
        copied ``/share/<token>`` still holds a working share URL, and re-visiting the
        album page will not re-mint them an ``upload`` grant — the first-visit branch
        only fires when no row exists. Rotating ``album.token`` on downgrade is the
        stronger move and is deliberately left out of this change.
        """
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
        # Uploads still in flight go with the album. There is no FK cascade to lean on
        # — SQLite runs with foreign keys off — and a surviving session is not merely
        # untidy: it can never complete (its share token stops resolving) yet it keeps
        # charging its uploader a concurrency slot and its whole declared size against
        # the in-flight byte quota until the TTL sweep notices, a day later.
        discard_sessions_for_album(db.session, album.id,
                                   current_app.config['UPLOAD_FOLDER'])
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
