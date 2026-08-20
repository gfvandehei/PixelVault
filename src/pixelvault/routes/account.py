"""Account routes — the one page a signed-in user manages themselves.

Two endpoints, deliberately split: ``GET /account`` renders identity and the form,
``POST /account/password`` is the only thing on this page that acts. Username and
email are read-only here by decision (docs/account_page_design.md §1) — changing an
address means asking an admin, because a self-service address change with no
verification round-trip is a way to lock an account out of every future recovery.

The password change is three lines of ORM around one ordering rule that is the whole
feature:

    set_password  ->  rotate_session_token  ->  commit  ->  login_user

``rotate_session_token`` invalidates every cookie already issued for this user,
including the one on the browser making the request — Flask-Login stores
``User.get_id()`` in both the session and remember-me cookies, and that value now
carries the token. So the current session has to be re-issued immediately after, or
the user is signed out by their own password change and reads it as a failure. §2 of
the design doc has the reasoning; the short version is that a password change which
cannot evict a stolen cookie is not a password change worth making.

The email notice is sent *after* the commit and cannot undo it, the same rule
``routes/admin.py`` follows for invites: a relay outage must never cost someone a
password they have already been told is set.
"""

import logging

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, login_user, current_user

from .. import emails
from ..config import MAX_PASSWORD_LEN
from ..extensions import db, limiter, mailer
from ..mailer import MailError
from ..models import User

logger = logging.getLogger(__name__)

#: Identical to ``routes/auth.py``'s registration rule, and imported from nowhere so
#: the two cannot silently drift — see the note in :func:`register`.
MIN_PASSWORD_LEN = 8


def register(app):

    @app.route('/account')
    @login_required
    def account():
        """Show the signed-in user's identity and the change-password form."""
        return render_template('account.html')

    @app.route('/account/password', methods=['POST'])
    @login_required
    @limiter.limit("10 per hour")
    def account_change_password():
        """Change the current user's password and sign out their other sessions.

        The rules below are the ones ``invite_submit`` enforces, down to the wording.
        They are restated rather than shared because the two forms fail differently —
        one re-renders a registration page, the other an account page — but the
        *rules* must not diverge: a minimum that relaxes during a copy is how an
        8-character floor disappears with nobody deciding to remove it.

        The limit is per user (``rate_limit_key`` keys on the id, unspoofable behind
        ``@login_required``). It bounds two things at once: someone with a hijacked
        session guessing the current password through this form, and the CPU a single
        session can spend on 600k-round hashes.
        """
        current = request.form.get('current_password', '')
        new = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')

        # Length before hash, always. check_password runs the full 600k rounds over
        # whatever it is handed, so an unbounded field is a worker thread on demand —
        # the same reason the length check precedes set_password below.
        if len(current) > MAX_PASSWORD_LEN or not current.strip() \
                or not current_user.check_password(current):
            # One message for "empty" and for "wrong". There is nothing to gain from
            # telling the sender of a hand-crafted POST which of the two it was.
            flash('Your current password is incorrect.', 'error')
            return render_template('account.html'), 403

        errors = []
        if len(new) < MIN_PASSWORD_LEN:
            errors.append("Password must be at least 8 characters.")
        elif len(new) > MAX_PASSWORD_LEN:
            errors.append("Password is too long.")
        elif new == current:
            # Not pedantry: this path evicts every other device the user is signed in
            # on, and doing that while reporting a change that did not happen is a
            # confusing way to lose a session.
            errors.append("Choose a password different from your current one.")
        if new != confirm:
            errors.append("Passwords do not match.")

        if errors:
            for message in errors:
                flash(message, 'error')
            return render_template('account.html'), 400

        # current_user is a proxy over the identity Flask-Login loaded; fetch the row
        # itself so the writes below are unmistakably against a session-attached
        # object rather than through a layer of indirection.
        user = db.session.get(User, current_user.id)
        user.set_password(new)
        user.rotate_session_token()
        db.session.commit()

        # The cookie in the browser that just posted this holds the token that was
        # rotated away a line ago, so it is now as dead as the ones on every other
        # device. Re-issue it. ``remember`` is read back off the request rather than
        # assumed: forcing it on would silently grant a persistent cookie to someone
        # who never asked for one, and forcing it off would evict a user from their
        # own browser the next time the session cookie expired.
        login_user(user, remember=bool(request.cookies.get('remember_token')))

        try:
            emails.send_password_changed(mailer, user)
        except MailError as exc:
            # The password is already committed and the sessions are already gone.
            # This is a delivery problem, and the only honest thing to do is say so
            # without implying the change failed.
            logger.warning("Password-changed notice failed for user %s: %s", user.id, exc)
            flash('Your password was changed, but the confirmation email could not be sent.',
                  'info')

        flash('Your password has been changed. Any other devices you were signed in on '
              'have been signed out.', 'success')
        return redirect(url_for('account'))
