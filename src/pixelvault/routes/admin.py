"""Admin panel routes — the whitelist, which is now the invite desk.

Adding an address used to be a one-line insert into a passive whitelist. It is now
the act that mints a bearer credential and mails it to a stranger, so the four
routes below are the HTTP layer over ``invites.py`` (lifecycle) and ``emails.py``
(delivery) in the same way ``routes/share.py`` is a layer over ``uploads.py``:
they parse a form, choose wording, and own nothing else.

Two sequences in here are contracts rather than preferences, both from
docs/invite_registration_design.md §13:

* **Issue, commit, then send.** ``invites.issue`` commits before anything is
  emailed, so a relay outage cannot throw away a valid invite (§7.1). Every send
  failure is therefore recoverable by *Resend* or the copy-link fallback (§7.3),
  which is why the failure flash names that fallback instead of apologising.
* **Renewal always goes through ``invites.rotate``**, never ``issue``. Resend,
  copy-link, and *Send invite* on a LEGACY row are the same operation with
  different wording. ``mark_sent`` is deliberately never called from here —
  ``emails.send_invite`` owns it on both outcomes, and a second call would
  double-count ``send_count``.
"""

from flask import render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from .. import emails, invites
from ..config import PUBLIC_BASE_URL, RE_EMAIL
from ..extensions import db, limiter, mailer
from ..mailer import MailError
from ..models import AllowedEmail, User, Album
from ..utils import admin_required, delete_photo_files


#: Longest address the ``allowed_email.email`` column can hold. Checked here as
#: well as by RE_EMAIL because SQLite does not enforce VARCHAR length: an
#: over-long address would store fine now and fail on any other backend later.
MAX_EMAIL_LEN = 120


def _invite_url(token):
    """Return the acceptance URL for a plaintext token, or ``None`` if it cannot be built.

    Built from ``PUBLIC_BASE_URL`` and ``emails.INVITE_PATH`` — the same two pieces
    ``emails.build_invite_email`` uses, and deliberately **not** from
    ``url_for(_external=True)``. Behind Cloudflare -> nginx -> Gunicorn the origin
    Flask reconstructs comes from forwarded headers, so a spoofed ``Host`` on the
    admin request would put an attacker's domain in front of a real invite token
    (design §6). A configured origin has no such input.

    ``None`` rather than a relative URL when the origin is unset: half a link that
    an admin pastes into a chat window is worse than a refusal, because it fails
    in the invitee's hands instead of the admin's.
    """
    if not PUBLIC_BASE_URL:
        return None
    return f"{PUBLIC_BASE_URL.rstrip('/')}{emails.INVITE_PATH}/{token}"


def _flash_link(entry, token):
    """Flash the one and only rendering of ``token`` as a copyable invite URL.

    The plaintext exists exactly once (only its SHA-256 is stored), so this
    message is the whole artifact — closing the page loses it and the fix is
    another rotation. The wording says so, because an admin who assumes they can
    come back for it later hands out a link they no longer have.
    """
    url = _invite_url(token)
    if url is None:
        flash('PUBLIC_BASE_URL is not configured, so an invite link has no origin. '
              'Set it and try again — see docs/configuration.md.', 'error')
        return
    flash(f'Invite link for {entry.email} — copy it now, it cannot be shown again: {url}',
          'success')


def register(app):

    @app.route('/admin')
    @login_required
    @admin_required
    def admin_panel():
        """Render the admin dashboard showing all invites, registered users, and all albums."""
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
        """Authorize an address, mint its first invite, and email the link (design §7.1).

        The two "already known" branches are separated on purpose. An address that
        already has an account needs nothing at all; an address that already has an
        invite needs *Resend*, and saying so is the difference between a dead end
        and an instruction. The old route answered both with the same shrug.

        Validation uses ``RE_EMAIL``, the same pattern registration enforces. The
        old ``'@' in email`` check was weaker than the form the address would later
        have to satisfy, so a typo that passed here produced an invite that could
        never be accepted — and, now, an email sent into the void.
        """
        email = request.form.get('email', '').strip().lower()
        note = request.form.get('note', '').strip()
        # Optional, and only ever a *suggestion*: the invitee may change it. There
        # is deliberately no is_admin flag beside it (design §11 Q8) — a mistyped
        # checkbox on this form must never be able to mint an administrator.
        prefill_username = request.form.get('prefill_username', '').strip()

        if not email or not RE_EMAIL.match(email) or len(email) > MAX_EMAIL_LEN:
            flash('Please enter a valid email address.', 'error')
            return redirect(url_for('admin_panel'))

        if db.session.query(User).filter_by(email=email).first():
            flash(f'{email} already has an account, so there is nothing to invite.', 'info')
            return redirect(url_for('admin_panel'))

        if db.session.query(AllowedEmail).filter_by(email=email).first():
            flash(f'{email} has already been invited. Use Resend on their row to send '
                  'a fresh link.', 'info')
            return redirect(url_for('admin_panel'))

        try:
            # Commits the row and hands back the plaintext token once. Anything
            # that goes wrong after this point leaves a resendable invite behind
            # rather than nothing.
            invite, token = invites.issue(
                db.session, email, note=note, prefill_username=prefill_username,
                invited_by_id=current_user.id,
            )
        except invites.InviteError as exc:
            # Backstop for two admins adding one address in the same second; the
            # duplicate check above catches every non-racing case.
            flash(str(exc), 'error')
            return redirect(url_for('admin_panel'))

        try:
            emails.send_invite(mailer, db.session, invite, token)
        except MailError as exc:
            # The invite is already committed and its token is live, so this is a
            # delivery problem, not an invite problem. send_invite has recorded the
            # error on the row (it reads SEND_FAILED in the panel); all that is left
            # is to point at the fallback that does not need a relay.
            flash(f'{email} was invited, but the email could not be sent: {exc} '
                  'Use "Copy link" on their row to hand the link over directly.', 'error')
        except ValueError as exc:
            # build_invite_email refuses to compose a link with no origin. Reachable
            # on a host with mail enabled but PUBLIC_BASE_URL unset — a boot check
            # covers the SMTP case, but ConsoleMailer and NullMailer boot without it.
            # Nothing was sent, so the row stays ISSUED and Copy link still works
            # once the origin is configured.
            flash(f'{email} was invited, but no email could be composed: {exc}', 'error')
        else:
            flash(f'{email} has been invited — an invitation is on its way.', 'success')

        return redirect(url_for('admin_panel'))

    @app.route('/admin/invite/<int:entry_id>/resend', methods=['POST'])
    @login_required
    @admin_required
    @limiter.limit("30 per hour")
    def admin_resend_invite(entry_id):
        """Mint a fresh token for an existing invite and email it again (design §7.4).

        Serves four rows with one code path: SENT (never clicked), EXPIRED,
        SEND_FAILED, and LEGACY — a whitelist entry from before invites existed,
        which has no token at all. ``rotate`` covers all four because it is the only
        renewal path there is (design §13); there is deliberately no "issue onto an
        existing row" variant to get out of step with it.

        The old link dies here. That is a consequence of storing only the hash — it
        cannot be re-shown, so renewal must mint — and the safer default anyway: a
        resend usually means the first link was lost or went somewhere it should
        not have.

        The cooldown is checked *before* rotating, so a refusal costs the invitee
        nothing: the link they already hold keeps working.
        """
        entry = db.session.get(AllowedEmail, entry_id)
        if not entry:
            abort(404)

        try:
            invites.check_resend_allowed(entry)
        except invites.ResendTooSoon as exc:
            # Naming the wait is the whole point: "try again later" sends the admin
            # back to a button that will refuse again with no idea when it will not.
            flash(f'An invitation to {entry.email} was just sent. Wait '
                  f'{exc.seconds_remaining} more seconds before sending another.', 'error')
            return redirect(url_for('admin_panel'))

        try:
            token = invites.rotate(db.session, entry)
        except invites.AlreadyAccepted:
            flash(f'{entry.email} has already registered, so there is nothing to resend.',
                  'info')
            return redirect(url_for('admin_panel'))

        try:
            # send_invite calls mark_sent on both outcomes — success clears any
            # earlier error, failure records the new one. Calling it here as well
            # would count one attempt twice.
            emails.send_invite(mailer, db.session, entry, token)
        except MailError as exc:
            flash(f'The invitation to {entry.email} could not be sent: {exc} '
                  'Use "Copy link" on their row to hand the link over directly.', 'error')
        except ValueError as exc:
            flash(f'No invitation to {entry.email} could be composed: {exc}', 'error')
        else:
            flash(f'Invitation resent to {entry.email}. Any earlier link has stopped working.',
                  'success')

        return redirect(url_for('admin_panel'))

    @app.route('/admin/invite/<int:entry_id>/link', methods=['POST'])
    @login_required
    @admin_required
    @limiter.limit("30 per hour")
    def admin_invite_link(entry_id):
        """Rotate the invite and show its link once, without sending mail (design §7.3).

        The fallback that makes SMTP optional rather than a hard dependency of
        registration: a relay that is down, misconfigured, or deliberately absent
        on a self-hosted box, or an invite sitting in someone's spam folder. The
        admin gets the URL and hands it over however they like.

        No cooldown check, unlike *Resend*. The cooldown exists because a resend
        button that mails a third party on demand is a mail-bomb primitive
        (design §7.4); this route sends nothing, and gating it would disable the
        fallback in precisely the minute after a failed send — the moment it is
        most needed. The 30/hour limit still bounds it.

        The origin is checked before rotating so a misconfigured host cannot kill a
        working link and give nothing back for it.
        """
        entry = db.session.get(AllowedEmail, entry_id)
        if not entry:
            abort(404)

        if not PUBLIC_BASE_URL:
            flash('PUBLIC_BASE_URL is not configured, so an invite link has no origin. '
                  'Set it and try again — see docs/configuration.md.', 'error')
            return redirect(url_for('admin_panel'))

        try:
            token = invites.rotate(db.session, entry)
        except invites.AlreadyAccepted:
            flash(f'{entry.email} has already registered, so there is no link to give out.',
                  'info')
            return redirect(url_for('admin_panel'))

        # No mark_sent: nothing was sent. The row reads ISSUED, which is exactly
        # "a live token nobody has emailed" — the state this path exists to create.
        _flash_link(entry, token)
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
        """Revoke an invite by deleting its row — which also kills its token (design §7.5).

        Revocation needs no separate mechanism: the token hash lives on this row, so
        deleting it makes any outstanding link unmatchable on the next lookup.

        Deletion does **not** touch the account an accepted invite produced.
        ``accepted_user_id`` is a plain nullable FK with no relationship or cascade
        behind it, so the row is bookkeeping about a past event and removing it
        removes only that. The confirm dialog in admin.html says so, because
        "remove alice@example.com" reads like an account deletion and an admin who
        believes that is one click from a surprise.
        """
        entry = db.session.get(AllowedEmail, entry_id)
        if not entry:
            abort(404)
        email = entry.email
        was_accepted = entry.accepted_at is not None
        db.session.delete(entry)
        db.session.commit()
        if was_accepted:
            flash(f'The invite record for {email} has been removed. Their account is untouched.',
                  'info')
        else:
            flash(f'The invitation for {email} has been revoked; its link no longer works.',
                  'info')
        return redirect(url_for('admin_panel'))
