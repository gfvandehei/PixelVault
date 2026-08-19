"""Authentication routes — logging in, logging out, and accepting an invitation.

Registration used to be a public form that checked a whitelist at the very end.
It is now reachable **only** through an invite link, so ``/register`` is gone
(design §8) and the three routes below replace it:

``GET /invite/<token>``  validate the link, stash it in the session, redirect
``GET /invite``          render the acceptance form from the stashed token
``POST /invite``         re-validate, create the account, sign the person in

Two rules in here are load-bearing and easy to erode:

* **The email address comes from the invite row and from nowhere else.** The form
  shows it read-only and does not even submit it; ``invites.consume`` reads it off
  the row. Anything else would let the holder of an invite for one address register
  as another and defeat the whitelist entirely — design §7.2 calls this the single
  most important line in the feature.
* **The secret leaves the URL at the first opportunity.** ``/invite/<token>``
  validates, moves the token into ``session['invite_token']`` and redirects to a
  path with no secret in it (design §11 Q5). ``Referrer-Policy`` already stops it
  leaking cross-origin, but a token in the path still lands in nginx access logs
  and in browser history, which is the other half of what hashing at rest buys.

The GET's verdict is never carried into the POST. An invite can expire, be rotated
by an admin, or be consumed in another tab between the two requests, and only
re-validating catches that.
"""

from flask import render_template, request, redirect, url_for, flash, session
from flask_login import login_user, logout_user, login_required, current_user

from .. import invites
from ..config import ADMIN_CONTACT, RE_USERNAME, MAX_PASSWORD_LEN
from ..extensions import db, limiter
from ..models import User, AllowedEmail
from ..utils import is_safe_redirect


#: Where the plaintext invite token lives between the click and the form.
#: Fixed by design §13 alongside the endpoint names — the session cookie is signed,
#: so this is a server-authenticated copy of the token rather than one the browser
#: can rewrite, which is precisely why the form carries no hidden token field.
INVITE_SESSION_KEY = 'invite_token'

#: Username shape, identical to what /register enforced. Kept as constants rather
#: than inlined so the acceptance form and any future admin-side check cannot drift.
MIN_USERNAME_LEN = 3
MAX_USERNAME_LEN = 64
MIN_PASSWORD_LEN = 8


def _refuse(message, status):
    """Flash ``message`` and answer with the login page under an honest status code.

    The login page rather than a bespoke error template because every one of these
    refusals ends in the same two pieces of advice — sign in, or ask an admin for a
    fresh link — and the sign-in half is right there on the page.

    The status is not decoration. These responses are the public face of a bearer
    credential, and a monitoring or crawling client that sees 200 for a dead link
    learns the opposite of the truth. Returned as a tuple rather than via ``abort``
    so the app's generic error page does not swallow the specific advice.
    """
    flash(message, 'error')
    return render_template('login.html'), status


def _refuse_invite_error(exc):
    """Map an :class:`invites.InviteError` onto wording and a status.

    The four faults get four different messages because they need four different
    actions from the reader, and a single "invalid link" for all of them is what
    made the old whitelist a dead end.

    ``InvalidInvite`` is deliberately worded for two audiences at once.
    ``consume`` nulls ``token_hash``, so a link that has already been used is
    *indistinguishable* from a typo — the row it pointed at no longer matches
    anything (design §13). Claiming to know which one happened would be a guess,
    and guessing wrong strands whichever reader we guessed against.

    ``ResendTooSoon`` cannot reach here: it is raised only by the admin panel's
    resend path, which sends mail. Nothing on this side of the flow sends anything.
    """
    if isinstance(exc, invites.AlreadyAccepted):
        return _refuse(
            'That invitation has already been accepted and the account exists. '
            'Sign in below, or use "Forgot your password" with an admin if you '
            'cannot get in.',
            409,
        )
    if isinstance(exc, invites.ExpiredInvite):
        return _refuse(
            'This invitation link has expired. Ask an admin to send you a new one — '
            'it takes them one click.',
            410,
        )
    if isinstance(exc, invites.InvalidInvite):
        return _refuse(
            'This invitation link is not valid. Check that you copied the whole link, '
            'including the part after the last slash. If you have already used it to '
            'create your account, sign in below instead — a link stops working the '
            'moment it is accepted.',
            404,
        )
    # The base InviteError, which validate() never raises today. Kept so a future
    # subclass surfaces as a refusal rather than a 500.
    return _refuse(str(exc), 400)


def register(app):

    @app.context_processor
    def inject_admin_contact():
        """Expose ``ADMIN_CONTACT`` to every template.

        ``request_permission.html`` is rendered from ``routes/albums.py`` and now has
        to name someone to ask for an invitation (design §8); a context processor is
        how it gets the address without every render call learning about mail config.
        Registered here because this module owns the invite-only story.
        """
        return {'admin_contact': ADMIN_CONTACT}

    @app.route('/')
    def index():
        """Redirect authenticated users to the dashboard, everyone else to the login page."""
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))
        return redirect(url_for('login'))

    # ── Accepting an invitation ────────────────────────────────────────────

    @app.route('/invite/<token>')
    @limiter.limit("60 per hour")
    def invite_link(token):
        """Validate an invite link, move the token into the session, and redirect.

        The redirect is the point of the route (design §11 Q5). The token is a
        bearer credential that creates an account bound to someone's real email
        address, and a URL carrying it is copied into nginx access logs, browser
        history, and any bookmark or shared "here's the page I'm on" message. One
        redirect later the address bar holds nothing worth stealing.

        Validation happens **before** the stash so a bad link is answered here, with
        advice specific to what is wrong with it, instead of being carried forward
        into a form that fails on submit for reasons the reader cannot see.
        """
        if current_user.is_authenticated:
            return _refuse_live_session()

        try:
            invites.validate(db.session, token)
        except invites.InviteError as exc:
            return _refuse_invite_error(exc)

        session[INVITE_SESSION_KEY] = token
        return redirect(url_for('invite_form'))

    @app.route('/invite')
    @limiter.limit("60 per hour")
    def invite_form():
        """Render the acceptance form for the invite stashed in the session.

        Re-validates rather than trusting the stash. The session survives for days;
        the invite it names may have expired or been rotated by an admin since the
        click, and finding that out now is better than after a password has been
        typed twice.
        """
        if current_user.is_authenticated:
            return _refuse_live_session()

        invite, refusal = _stashed_invite()
        if refusal is not None:
            return refusal

        return render_template(
            'register.html',
            email=invite.email,
            # A suggestion from the admin, not a decision: the invitee may change it
            # (design §11 Q8).
            username=invite.prefill_username or '',
        )

    @app.route('/invite', methods=['POST'])
    @limiter.limit("20 per hour")
    def invite_submit():
        """Create the account an invitation authorizes and sign the new user in.

        The username and password rules are exactly the ones ``/register`` enforced,
        down to the wording — this route replaces that one, and quietly relaxing a
        rule during a move is how a password minimum disappears without anyone
        deciding to remove it.

        What is *not* taken from the form is the email address. It is read off the
        invite row inside ``invites.consume`` and the form does not even submit a
        field for it, so a hand-crafted POST carrying ``email=someone.else@...``
        changes nothing. That is the whole security property of the feature: the
        whitelist means nothing if the holder of a link can pick the address it
        authorizes.
        """
        if current_user.is_authenticated:
            return _refuse_live_session()

        invite, refusal = _stashed_invite()
        if refusal is not None:
            return refusal

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        # request.form['email'] is deliberately never read. See the docstring.

        errors = []
        if not username or len(username) < MIN_USERNAME_LEN:
            errors.append("Username must be at least 3 characters.")
        elif len(username) > MAX_USERNAME_LEN:
            errors.append("Username must be 64 characters or fewer.")
        elif not RE_USERNAME.match(username):
            errors.append("Username may only contain letters, numbers, hyphens, and underscores.")
        if len(password) < MIN_PASSWORD_LEN:
            errors.append("Password must be at least 8 characters.")
        elif len(password) > MAX_PASSWORD_LEN:
            # Not a style rule: set_password runs 600k PBKDF2 rounds, so an
            # unbounded password is a way to spend a worker thread on demand.
            errors.append("Password is too long.")
        if password != confirm:
            errors.append("Passwords do not match.")

        if not errors:
            if db.session.query(User).filter_by(username=username).first():
                errors.append("Username already taken.")
            if db.session.query(User).filter_by(email=invite.email).first():
                # Reachable when an admin created the account by hand after issuing
                # the invite. Caught here because consume() would otherwise fail on
                # the unique index and be reported as a username collision, sending
                # the reader off to invent names that will never work.
                errors.append("An account already exists for this address. Please log in instead.")
            if not db.session.query(AllowedEmail).filter_by(email=invite.email).first():
                # Belt and braces (design §8). A valid token already implies the
                # whitelist row, because the token *lives on* that row — but this
                # assertion is what keeps the whitelist the thing registration is
                # gated on, rather than a comment claiming it once was.
                errors.append("This email address has not been authorized to register.")

        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('register.html', username=username, email=invite.email)

        try:
            user = invites.consume(db.session, invite, username=username, password=password)
        except invites.InviteError as exc:
            if isinstance(exc, (invites.InvalidInvite, invites.ExpiredInvite,
                                invites.AlreadyAccepted)):
                # A double-submit or a shared link losing the race between the
                # check above and the commit. The invite is spent either way, so
                # the session copy is worthless now.
                session.pop(INVITE_SESSION_KEY, None)
                return _refuse_invite_error(exc)
            # The base InviteError is the username-collision backstop; it is worth
            # re-showing the form for, because a different name will work.
            flash(str(exc), 'error')
            return render_template('register.html', username=username, email=invite.email)

        # The token is spent. Dropping it stops a browser-history "back" from
        # replaying the form against a row that can only refuse it now.
        session.pop(INVITE_SESSION_KEY, None)
        login_user(user, remember=True)
        flash('Welcome to PixelVault!', 'success')
        return redirect(url_for('dashboard'))

    # ── Sessions ───────────────────────────────────────────────────────────

    @app.route('/login', methods=['GET', 'POST'])
    @limiter.limit("20 per hour")
    def login():
        """
        Handle user login.

        GET  — render the login form.
        POST — verify credentials and start a session. Redirects to the 'next' query-param URL
               if present and safe, otherwise to the dashboard.
        """
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            username = request.form.get('username', '').strip()[:64]
            password = request.form.get('password', '')
            remember = bool(request.form.get('remember'))

            if len(password) > MAX_PASSWORD_LEN:
                flash('Invalid username or password.', 'error')
                return render_template('login.html', username=username)

            user = db.session.query(User).filter_by(username=username).first()
            if not user or not user.check_password(password):
                flash('Invalid username or password.', 'error')
                return render_template('login.html', username=username)

            login_user(user, remember=remember)
            next_page = request.args.get('next')
            return redirect(next_page if is_safe_redirect(next_page) else url_for('dashboard'))

        return render_template('login.html')

    @app.route('/logout')
    @login_required
    def logout():
        """Clear the current user's session and redirect to the login page."""
        logout_user()
        flash('You have been logged out.', 'info')
        return redirect(url_for('login'))


# ── Helpers shared by the three invite routes ──────────────────────────────

def _refuse_live_session():
    """Refuse to run an invitation into a session that already belongs to someone.

    Design §7.2 allows either logging the visitor out or refusing. **Refusing** is
    the choice here, for two reasons:

    * Logging them out is a side effect any stranger can trigger in someone else's
      browser by getting them to open a URL. A drive-by logout that discards an
      in-progress upload is a worse outcome than a message.
    * The server cannot tell whether the signed-in person is the invitee. Silently
      swapping sessions would be guessing about identity at exactly the moment the
      answer matters, and the person best placed to decide is the one reading the
      screen.

    The token is not stashed, not validated and not consumed, so the link keeps
    working for whoever it was actually sent to.
    """
    flash('You are already signed in. An invitation creates a new account, so log out '
          'first if this invitation is for someone else.', 'info')
    return redirect(url_for('dashboard'))


def _stashed_invite():
    """Return ``(invite, None)`` for a live stashed token, or ``(None, response)``.

    Both halves of the form need the same three-step check — is there a token, is
    it still good, and if not what should the reader be told — and splitting it
    would let the GET and the POST answer the same broken link differently.

    A dead token is dropped from the session on the way out. Leaving it there turns
    every later visit to ``/invite`` into the same refusal with no way to clear it
    short of the reader knowing to delete a cookie.
    """
    token = session.get(INVITE_SESSION_KEY)
    if not token:
        # Someone typed /invite, or came back long after their cookie expired.
        # Worth its own wording: there is nothing wrong with any link, they just
        # have not opened one.
        return None, _refuse(
            'Open the invitation link you were sent to create your account. '
            'PixelVault is invite-only, so there is no public sign-up page.',
            403,
        )

    try:
        return invites.validate(db.session, token), None
    except invites.InviteError as exc:
        session.pop(INVITE_SESSION_KEY, None)
        return None, _refuse_invite_error(exc)
