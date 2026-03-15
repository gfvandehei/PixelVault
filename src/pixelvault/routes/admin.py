from flask import render_template, request, redirect, url_for, flash, abort
from flask_login import login_required

from ..extensions import db, limiter
from ..models import AllowedEmail, User
from ..utils import admin_required


def register(app):

    @app.route('/admin')
    @login_required
    @admin_required
    def admin_panel():
        allowed_emails = db.session.query(AllowedEmail).order_by(AllowedEmail.added_at.desc()).all()
        users = db.session.query(User).order_by(User.created_at.desc()).all()
        return render_template('admin.html', allowed_emails=allowed_emails, users=users)

    @app.route('/admin/email/add', methods=['POST'])
    @login_required
    @admin_required
    @limiter.limit("60 per hour")
    def admin_add_email():
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

    @app.route('/admin/email/<int:entry_id>/remove', methods=['POST'])
    @login_required
    @admin_required
    def admin_remove_email(entry_id):
        entry = db.session.get(AllowedEmail, entry_id)
        if not entry:
            abort(404)
        email = entry.email
        db.session.delete(entry)
        db.session.commit()
        flash(f'{email} has been removed from the allowed list.', 'info')
        return redirect(url_for('admin_panel'))
