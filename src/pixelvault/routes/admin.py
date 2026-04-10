from flask import render_template, request, redirect, url_for, flash, abort
from flask_login import login_required

from ..extensions import db, limiter
from ..models import AllowedEmail, User, Album
from ..utils import admin_required, delete_photo_files


def register(app):

    @app.route('/admin')
    @login_required
    @admin_required
    def admin_panel():
        """Render the admin dashboard showing all allowed emails, registered users, and all albums."""
        allowed_emails = db.session.query(AllowedEmail).order_by(AllowedEmail.added_at.desc()).all()
        users = db.session.query(User).order_by(User.created_at.desc()).all()
        albums = db.session.query(Album).order_by(Album.created_at.desc()).all()
        user_storage = {
            user.id: sum(photo.file_size for album in user.albums for photo in album.photos)
            for user in users
        }
        return render_template('admin.html', allowed_emails=allowed_emails, users=users, albums=albums, user_storage=user_storage)

    @app.route('/admin/email/add', methods=['POST'])
    @login_required
    @admin_required
    @limiter.limit("60 per hour")
    def admin_add_email():
        """Add an email address to the registration whitelist so the user can create an account."""
        email = request.form.get('email', '').strip().lower()
        note = request.form.get('note', '').strip()

        if not email or '@' not in email:
            flash('Please enter a valid email address.', 'error')
            return redirect(url_for('admin_panel'))

        if db.session.query(AllowedEmail).filter_by(email=email).first():
            flash(f'{email} is already on the allowed list.', 'info')
            return redirect(url_for('admin_panel'))

        entry = AllowedEmail(email=email, note=note)
        db.session.add(entry)
        db.session.commit()
        flash(f'{email} has been authorized.', 'success')
        return redirect(url_for('admin_panel'))

    @app.route('/admin/album/<int:album_id>/delete', methods=['POST'])
    @login_required
    @admin_required
    def admin_delete_album(album_id):
        """Delete any album and all its files as an admin action."""
        album = db.session.get(Album, album_id)
        if not album:
            abort(404)
        name = album.name
        for photo in album.photos:
            delete_photo_files(photo)
        db.session.delete(album)
        db.session.commit()
        flash(f'Album "{name}" has been deleted.', 'info')
        return redirect(url_for('admin_panel'))

    @app.route('/admin/email/<int:entry_id>/remove', methods=['POST'])
    @login_required
    @admin_required
    def admin_remove_email(entry_id):
        """Remove an email address from the registration whitelist by its database ID."""
        entry = db.session.get(AllowedEmail, entry_id)
        if not entry:
            abort(404)
        email = entry.email
        db.session.delete(entry)
        db.session.commit()
        flash(f'{email} has been removed from the allowed list.', 'info')
        return redirect(url_for('admin_panel'))
