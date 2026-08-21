"""Message composition — turning an invite into something a person can read.

The third of the three concerns the mail feature is split into, and the smallest.
``mailer.py`` knows how to put an ``EmailMessage`` on the wire and nothing about
what is in it; ``invites.py`` owns the credential's lifecycle and never renders;
this module is the only place that knows an invite has *wording*. Swapping SMTP
for a provider's HTTP API touches ``mailer.py`` alone, and rewording the invite
touches ``templates/email/`` alone — neither edit can reach the other.

Three rules here are load-bearing rather than stylistic:

* **The text part is authored first.** The message is ``multipart/alternative``
  and the HTML is garnish. A client picks the last part it understands, so a
  malformed HTML part costs nothing that matters as long as the plain-text one
  still carries a complete, clickable URL. That ordering is why
  :func:`build_invite_email` calls ``set_content`` before ``add_alternative``.
* **The link is built from a configured origin, never from the request.** Behind
  Cloudflare -> nginx -> Gunicorn the URL ``url_for(_external=True)`` reconstructs
  is only as trustworthy as the forwarded headers, so an attacker-controlled
  ``Host`` on the *add-email* request would otherwise steer a real invite — sent
  by this server, to a real person, over a genuine address — at a domain they own.
  ``PUBLIC_BASE_URL`` removes the question (design §6).
* **Nothing token-shaped is ever logged**, the same rule ``mailer.py`` and
  ``invites.py`` run under. The token creates an account bound to someone's email
  address; a copy in a log file is a copy of the credential. Log lines here carry
  the invite id and the address, never the secret and never the body.

See docs/invite_registration_design.md §6 and §13.
"""

import logging
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from flask import render_template

from . import invites
from .config import ADMIN_CONTACT, MAIL_FROM, MAIL_FROM_NAME, PUBLIC_BASE_URL
from .mailer import MailError

logger = logging.getLogger(__name__)

#: Subject line for the invite. Kept here rather than in the templates because a
#: subject is a header, not body copy — it has to be set on the message before
#: either part exists, and it must read identically whichever part a client shows.
INVITE_SUBJECT = 'You have been invited to PixelVault'

#: Subject of the password-change notice. Written as a statement of fact rather
#: than a question ("Was this you?") so it reads correctly in a preview pane, which
#: is where most recipients will see the whole of it.
PASSWORD_CHANGED_SUBJECT = 'Your PixelVault password was changed'

#: Path the invite token is presented at. Must match the ``invite_link`` route in
#: design §13's endpoint table; the email is written before that route exists, so
#: this constant is the only place the two can drift.
INVITE_PATH = '/invite'


# ── Human-readable expiry ──────────────────────────────────────────────────

def _format_instant(moment: datetime) -> str:
    """Render an instant as a date a person can act on.

    Used for an invite's expiry and for the moment a password changed — both are
    UTC timestamps out of the database being read by someone who may not be in UTC.

    Explicitly labelled UTC, because that is what the column holds and an
    unlabelled time is worse than none — an invitee in UTC+10 reading a bare
    "14:00" has been told the wrong thing, not an ambiguous thing.

    ``%d`` is avoided so the day is not zero-padded; the ``%-d`` that would do
    that directly is glibc-only and would break the moment anyone runs this on a
    non-Linux host.
    """
    return f"{moment.day} {moment:%B %Y} at {moment:%H:%M} UTC"


def _humanise_remaining(expires_at: datetime, now: datetime) -> str:
    """Render the time left as a rough duration, or ``''`` if there is none.

    Deliberately coarse. The exact figure is stale the moment the message is
    queued, and "about 3 days" survives an hour in a relay's retry queue in a way
    "in 71 hours and 48 minutes" does not. The absolute timestamp beside it is the
    precise statement; this is the one that gets read.

    Empty for an expiry already in the past so the templates can drop the phrase
    rather than promise time that does not exist.
    """
    seconds = (expires_at - now).total_seconds()
    if seconds <= 0:
        return ''
    if seconds < 3600:
        return 'less than an hour'
    hours = round(seconds / 3600)
    if hours < 48:
        return f"about {hours} hour{'s' if hours != 1 else ''}"
    days = round(seconds / 86400)
    return f"about {days} day{'s' if days != 1 else ''}"


# ── Composition ────────────────────────────────────────────────────────────

def build_invite_email(invite, token, *, base_url=PUBLIC_BASE_URL,
                       from_addr=MAIL_FROM, from_name=MAIL_FROM_NAME) -> EmailMessage:
    """Compose the invite message for ``invite``, carrying the plaintext ``token``.

    Returns a ``multipart/alternative`` message with the plain-text part first and
    the HTML part second, both rendered from ``templates/email/invite.*`` through
    the app's Jinja environment — so the copy is reviewable as text in the same
    place as every other template, and needs an app context to build.

    ``base_url`` defaults to ``PUBLIC_BASE_URL`` and is the *only* source of the
    link's origin; it is a parameter so a caller can be explicit and a test can
    prove the value is not coming from a request. The path is ``/invite/<token>``.

    Raises ``ValueError`` if the invite has no ``expires_at``. That combination is
    not reachable through :mod:`~pixelvault.invites` — ``_mint`` writes the token
    and both timestamps together — so it means a hand-edited or corrupted row, and
    ``validate`` will refuse the link anyway. Sending it would deliver a dead link
    with no expiry stated on it, which is exactly the silent failure this email
    exists to prevent; failing here is the loud version.
    """
    if not base_url:
        raise ValueError(
            "PUBLIC_BASE_URL is unset, so an invite link would have no origin. "
            "See docs/configuration.md."
        )
    if invite.expires_at is None:
        raise ValueError(
            f"Invite {invite.id} has no expires_at; refusing to send a link "
            "that cannot state when it stops working."
        )

    invite_url = f"{base_url.rstrip('/')}{INVITE_PATH}/{token}"
    context = {
        'invite_url': invite_url,
        'email': invite.email,
        'site_url': base_url.rstrip('/'),
        'username': invite.prefill_username or '',
        'expires_at_text': _format_instant(invite.expires_at),
        'expires_in_text': _humanise_remaining(invite.expires_at, datetime.utcnow()),
    }

    message = EmailMessage()
    message['Subject'] = INVITE_SUBJECT
    # Address() rather than a hand-built "Name <addr>" string: it encodes a display
    # name containing a comma, a quote or a non-ASCII character correctly, and a
    # broken From is a message the relay rejects outright.
    local, _, domain = from_addr.partition('@')
    message['From'] = Address(display_name=from_name, username=local, domain=domain)
    message['To'] = invite.email
    # Date and Message-ID are set here rather than left to the relay. Both are
    # absent-header spam signals, and an invite is precisely the mail that must not
    # land in a spam folder. The Message-ID domain is the sender's, so the host's
    # internal name — which is what make_msgid() would otherwise use — stays private.
    message['Date'] = formatdate(localtime=True)
    message['Message-ID'] = make_msgid(domain=domain or None)

    # Text first, HTML second. set_content makes the plain part the body; the
    # add_alternative call is what promotes the message to multipart/alternative
    # with that part already in place. Reversing these two lines would put the
    # garnish where the guaranteed-readable copy belongs.
    message.set_content(render_template('email/invite.txt', **context))
    message.add_alternative(render_template('email/invite.html', **context),
                            subtype='html')
    return message


def send_invite(mailer, session, invite, token) -> None:
    """Build the invite email, send it, and record the attempt on the row either way.

    ``mailer`` is anything with ``Mailer.send`` — in a route that is the
    ``extensions.mailer`` proxy, in a test a ``MemoryMailer``.

    The bookkeeping is the part worth reading. :func:`invites.mark_sent` is called
    on **both** outcomes, per design §13: it counts *attempts*, so skipping it on
    the failure path would leave ``send_count`` undercounting and — worse — leave
    ``last_send_error`` empty, so the row would read SENT and the admin panel would
    show a delivered invite that never arrived. On success the same call clears any
    error left by an earlier attempt, which is what moves a SEND_FAILED row to SENT
    after a resend works.

    ``MailError`` is re-raised once the row is stamped, so the route can flash the
    real reason and offer the copy-link fallback (§7.3). Nothing is swallowed here:
    this module has no view on what an admin should be told.
    """
    message = build_invite_email(invite, token)
    try:
        mailer.send(message)
    except MailError as exc:
        # str(exc) is the relay's complaint, which mailer.py builds from the fault
        # alone and never from the message — so it is safe to store and render.
        # Truncation to the column width happens inside mark_sent.
        invites.mark_sent(session, invite, error=str(exc))
        raise

    invites.mark_sent(session, invite)
    logger.info("Invite %s emailed to %s", invite.id, invite.email)


# ── Password-change notice ─────────────────────────────────────────────────

def build_password_changed_email(user, *, changed_at=None, base_url=PUBLIC_BASE_URL,
                                 from_addr=MAIL_FROM, from_name=MAIL_FROM_NAME,
                                 contact=ADMIN_CONTACT) -> EmailMessage:
    """Compose the "your password was changed" notice for ``user``.

    Same shape as :func:`build_invite_email` — text part first, HTML alternative
    second, ``Date`` and ``Message-ID`` set here rather than left to the relay —
    and two deliberate differences:

    * **It carries no link and no token.** There is no reset flow yet (issue #33),
      so a recipient who did not make this change is told to contact a human. A
      message that arrives unexpectedly is exactly the message an attacker would
      want to imitate with a link in it; this one has nothing to click.
    * **``base_url`` is decorative.** The invite refuses to compose without an
      origin because its whole payload is a URL. Here the origin only names which
      site is being talked about, so an unset ``PUBLIC_BASE_URL`` degrades to
      "PixelVault" rather than raising — an account with no notice is worse than a
      notice with no hostname.

    ``changed_at`` defaults to now and is stated in labelled UTC, because that is
    what the database holds and an unlabelled time tells a reader in another zone
    something false rather than something vague.
    """
    changed_at = changed_at or datetime.utcnow()
    site_url = (base_url or '').rstrip('/')
    context = {
        'username': user.username,
        'email': user.email,
        'site_url': site_url,
        'site_name': site_url or 'PixelVault',
        'changed_at_text': _format_instant(changed_at),
        'contact': contact or '',
    }

    message = EmailMessage()
    message['Subject'] = PASSWORD_CHANGED_SUBJECT
    local, _, domain = from_addr.partition('@')
    message['From'] = Address(display_name=from_name, username=local, domain=domain)
    message['To'] = user.email
    message['Date'] = formatdate(localtime=True)
    message['Message-ID'] = make_msgid(domain=domain or None)

    message.set_content(render_template('email/password_changed.txt', **context))
    message.add_alternative(render_template('email/password_changed.html', **context),
                            subtype='html')
    return message


def send_password_changed(mailer, user, *, changed_at=None) -> None:
    """Tell ``user`` their password changed. Never the reason a change is lost.

    Called from ``routes/account.py`` *after* the new password is committed, so
    nothing here can undo it — which is why the one configuration fault that would
    raise is handled instead of propagated: with ``MAIL_FROM`` empty there is no
    sender to build a message from, and crashing a completed password change over
    an unconfigured relay would turn a missing courtesy email into a 500 on the
    account page.

    ``MailError`` *is* allowed out, because the caller can say something useful
    about a relay that refused delivery. Nothing token-shaped is logged here; there
    is nothing secret in this message to begin with.
    """
    if not MAIL_FROM:
        logger.info("Password changed for user %s; no MAIL_FROM configured, notice skipped",
                    user.id)
        return

    message = build_password_changed_email(user, changed_at=changed_at)
    mailer.send(message)
    logger.info("Password-change notice sent to user %s", user.id)
