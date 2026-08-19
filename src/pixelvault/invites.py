"""Invite lifecycle — issuing, rotating, validating and consuming registration links.

An ``AllowedEmail`` row is two facts at once: the whitelist entry that permits an
address to register, and the bearer credential that carries that permission to a
person. This module owns the second half — minting a token, deciding whether a
presented one is still good, and turning it into exactly one account.

It is deliberately free of Flask. Nothing here imports ``request``, flashes, or
aborts, for the same reason ``uploads.py`` does not: the rules below have to hold
identically for an admin request, a CLI command and a test with no HTTP client at
all, and they are far easier to audit when they are not interleaved with rendering.
The routes in steps 5 and 6 decide presentation; every fault here surfaces as an
:class:`InviteError` subclass and they map it.

Two invariants earn most of the comments in this file:

* **The plaintext token exists exactly once.** :func:`issue` and :func:`rotate`
  return it and then it is gone — only its SHA-256 is stored. That is what makes a
  leaked database backup or a screenshot of the admin panel useless, and it is also
  why a resend cannot re-show the old link and must mint a new one instead.
* **Nothing token-shaped is ever logged.** The token creates an account bound to a
  real person's email address; a copy in a log file is a copy of the credential.
  Log lines here carry the invite id and the address, never the secret.

See docs/invite_registration_design.md §4 (token handling), §7 (request flows) and
§13 (the signatures below, which are a contract with the modules built around them).
"""

import hashlib
import hmac
import logging
import math
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import INVITE_RESEND_COOLDOWN_SECONDS, INVITE_TTL_HOURS
from .models import AllowedEmail, User

logger = logging.getLogger(__name__)

#: Bytes of entropy per token. 32 bytes is 256 bits, rendered by
#: ``secrets.token_urlsafe`` as 43 URL-safe characters.
TOKEN_BYTES = 32

#: ``AllowedEmail.last_send_error`` is ``String(256)``, and SQLite does not enforce
#: VARCHAR length — an over-long relay error would be stored in full and then
#: silently break on a backend that does enforce it. Truncation happens here so
#: every writer gets it, rather than at each call site.
MAX_SEND_ERROR_LEN = 256


class InviteError(Exception):
    """Base for every invite fault. Routes catch this and choose the wording."""


class InvalidInvite(InviteError):
    """No live invite matches the presented token.

    Covers a garbage or guessed token, a link from a database that has since been
    reset, and — because :func:`consume` nulls ``token_hash`` — a token that was
    already used. The last case is worth knowing about at the route layer: a
    consumed link is unrecoverable by design, so it cannot be told apart from a
    typo and the acceptance page has to word this one for both.
    """


class ExpiredInvite(InviteError):
    """The token is real but its TTL has run out. The fix is rotate-and-resend."""


class AlreadyAccepted(InviteError):
    """This invite has already produced an account.

    Raised by :func:`consume` when a double-submit or a shared link loses the race,
    and by :func:`rotate` because acceptance is terminal — re-minting a token there
    would reopen a flow that is finished.
    """


class ResendTooSoon(InviteError):
    """Another send was attempted inside ``INVITE_RESEND_COOLDOWN_SECONDS``.

    Carries :attr:`seconds_remaining` so the caller can say *when* rather than just
    *no*. The cooldown is not about admin patience: an invite is mail this server
    sends to a third party on request, so an unthrottled resend button is a
    mail-bomb primitive pointed at whatever address was typed in.
    """

    def __init__(self, seconds_remaining):
        seconds_remaining = max(0, int(seconds_remaining))
        super().__init__(
            f"An invite was just sent; wait {seconds_remaining}s before sending another."
        )
        self.seconds_remaining = seconds_remaining


# ── Tokens ─────────────────────────────────────────────────────────────────

def hash_token(token: str) -> str:
    """Return the stored form of an invite token: a plain SHA-256 hexdigest.

    Plain SHA-256 and not bcrypt, deliberately. A password hash is slow to defeat
    guessing a human-chosen secret from a dictionary; there is no dictionary here,
    because the token is 256 bits from ``secrets``. Slowing the hash would only
    slow the one legitimate lookup on every click.

    The exact form is load-bearing beyond this module: the column is ``String(64)``,
    which fits a hexdigest and nothing longer.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def _mint(invite, ttl_hours, now=None):
    """Stamp a fresh token onto ``invite`` and return the plaintext, once.

    The three columns move together on purpose. ``AllowedEmail.state`` reads
    ``expires_at`` directly and never re-derives it from ``token_issued_at``, so a
    row carrying a token with a NULL ``expires_at`` would report ISSUED or SENT
    forever — an immortal credential that the admin panel swears is fine. Keeping
    the assignment in one private function is what stops a future caller from
    setting two of the three.
    """
    now = now or datetime.utcnow()
    token = secrets.token_urlsafe(TOKEN_BYTES)
    invite.token_hash = hash_token(token)
    invite.token_issued_at = now
    invite.expires_at = now + timedelta(hours=ttl_hours)
    return token


# ── Issuing ────────────────────────────────────────────────────────────────

def issue(session, email, *, note='', prefill_username='',
          invited_by_id=None, ttl_hours=INVITE_TTL_HOURS):
    """Authorize ``email`` and mint its first invite; return ``(invite, plaintext_token)``.

    For an address the app has never seen. An address that already has a row — an
    expired invite, a delivery failure, or a LEGACY whitelist entry from before
    invites existed — is renewed with :func:`rotate` instead, which is also what
    the admin panel's *Resend* and *Send invite* buttons call.

    The address is normalised here rather than trusted from the caller. It is the
    lookup key for the whole whitelist and, after acceptance, the account's
    identity; letting ``Alice@Example.com `` and ``alice@example.com`` become two
    rows would mean an invite that silently authorizes a second address.

    The row is committed before anything is emailed (design §7.1), so an invite
    survives a relay outage and stays resendable.

    Raises :class:`InviteError` if the address already has a row. That is a
    backstop for two admins clicking at once — the route checks first and offers
    resend — not a flow anyone should reach by design.
    """
    email = (email or '').strip().lower()
    invite = AllowedEmail(
        email=email,
        note=note,
        prefill_username=prefill_username,
        invited_by_id=invited_by_id,
    )
    token = _mint(invite, ttl_hours)
    session.add(invite)
    try:
        session.commit()
    except IntegrityError:
        # Unique on `email`. Roll back so the caller's session is usable — an
        # un-rolled-back failed flush poisons every later statement on it.
        session.rollback()
        raise InviteError(f"{email} has already been invited.")

    logger.info("Issued invite %s for %s", invite.id, invite.email)
    return invite, token


def rotate(session, invite, *, ttl_hours=INVITE_TTL_HOURS):
    """Mint a replacement token for an existing invite and return the plaintext, once.

    Every renewal path goes through here: *Resend*, the copy-link fallback, and
    *Send invite* on a LEGACY row that never had a token at all. The old link stops
    working the moment this commits.

    That the old link dies is a consequence of hashing rather than a feature choice
    — a link cannot be re-shown, so renewal has to mint — but it is the safer
    default anyway: a resend usually means the first link was lost or went
    somewhere it should not have.

    ``send_count`` and ``last_send_error`` are left alone. The count is history the
    admin panel reports ("resent 4x, still not accepted"), and the error is still
    an accurate statement about the last delivery attempt until another one
    happens; :func:`mark_sent` clears it when one succeeds.

    Raises :class:`AlreadyAccepted` if the invite has been consumed — acceptance is
    terminal, and reopening it would put a live credential on a finished row.
    """
    if invite.accepted_at is not None:
        raise AlreadyAccepted(f"{invite.email} has already registered.")

    token = _mint(invite, ttl_hours)
    session.commit()
    logger.info("Rotated invite %s for %s", invite.id, invite.email)
    return token


# ── Validation ─────────────────────────────────────────────────────────────

def validate(session, token, *, now=None):
    """Return the invite a presented token belongs to, or raise why it cannot be used.

    The three refusals are distinct because the acceptance page gives different
    advice for each: :class:`InvalidInvite` ("check the link"),
    :class:`ExpiredInvite` ("ask for a new one") and :class:`AlreadyAccepted`
    ("you already have an account — sign in").

    Called on both halves of acceptance. The GET's verdict is never carried over to
    the POST: the invite can expire, be rotated, or be consumed by another tab in
    between, and only re-validating catches that.
    """
    now = now or datetime.utcnow()
    if not token:
        raise InvalidInvite("No invite token was presented.")

    presented = hash_token(token)
    # An indexed equality match on the hash, so this is one index probe rather than
    # a scan. Guessing is not a threat model worth defending here — 256 bits means
    # an attacker's odds are indistinguishable from zero however many times they
    # try, and the endpoint is rate-limited on top — so the query is allowed to be
    # an ordinary lookup.
    invite = session.execute(
        select(AllowedEmail).where(AllowedEmail.token_hash == presented)
    ).scalar_one_or_none()
    if invite is None:
        raise InvalidInvite("This invite link is not valid.")

    # Constant-time on the final compare even though the row was already found by
    # equality on the same value. It costs one comparison of 64 hex characters and
    # removes the question entirely, which is cheaper than having to reason about
    # whether some future backend's index lookup leaks a prefix through timing.
    if not hmac.compare_digest(invite.token_hash, presented):
        raise InvalidInvite("This invite link is not valid.")

    # Checked in the same order as AllowedEmail.state, so the admin panel and the
    # acceptance page can never disagree about which fault a row has.
    if invite.accepted_at is not None:
        raise AlreadyAccepted(f"{invite.email} has already registered.")

    # A token with no expiry is a row this module never writes — `_mint` sets all
    # three columns together — so it is corruption or a hand-edited database.
    # Refused rather than honoured: the failure mode of trusting it is a bearer
    # credential that never dies.
    if invite.expires_at is None or now >= invite.expires_at:
        # Inclusive, matching AllowedEmail.state. A strict `>` would open a window
        # in which the panel reports EXPIRED and the link still works.
        raise ExpiredInvite("This invite link has expired.")

    return invite


# ── Acceptance ─────────────────────────────────────────────────────────────

def consume(session, invite, *, username, password):
    """Create the account this invite authorizes and burn the invite. Returns the ``User``.

    The email comes from ``invite.email`` and from nowhere else. This is the single
    most important line in the feature: taking it from a submitted form would let
    the holder of an invite for one address register as another and defeat the
    whitelist entirely.

    ``username`` and ``password`` are the caller's to validate for shape — the
    route already does exactly that, and duplicating the rules here would mean two
    places to change them.

    Everything lands in **one transaction**: insert the user, flush to learn its id,
    stamp ``accepted_at`` / ``accepted_user_id``, null ``token_hash``. One commit is
    what makes a crash safe. Split across two, a crash between them leaves either an
    account beside a still-live link that can be replayed, or a burnt invite with no
    account and an invitee who is now locked out with nothing to click.

    Raises :class:`AlreadyAccepted` when a double-submit or a shared link loses the
    race, :class:`ExpiredInvite` / :class:`InvalidInvite` if the invite is not
    usable, and :class:`InviteError` if the username collides with an existing
    account.
    """
    if invite.accepted_at is not None:
        raise AlreadyAccepted(f"{invite.email} has already registered.")
    if invite.token_hash is None:
        raise InvalidInvite("This address has no live invite.")
    if invite.expires_at is None or datetime.utcnow() >= invite.expires_at:
        raise ExpiredInvite("This invite link has expired.")

    invite_id = invite.id
    email = invite.email

    user = User(username=username, email=email)
    user.set_password(password)  # the model owns the hashing parameters, not this module
    session.add(user)
    try:
        # Flush rather than commit: the id is needed for accepted_user_id, and the
        # transaction must stay open until the invite is burnt in it too.
        session.flush()
        invite.accepted_at = datetime.utcnow()
        invite.accepted_user_id = user.id
        # Nulling the hash is what enforces single use. Safe even though a tokenless
        # row otherwise reads LEGACY, because ACCEPTED is tested first (design §4).
        invite.token_hash = None
        session.commit()
    except IntegrityError:
        session.rollback()
        # The unique constraint on `user.email` is what decides a race: two consumes
        # of one invite necessarily agree on the address, so the second insert
        # cannot land whatever username it chose. Re-read the row to tell that apart
        # from a plain username collision, because the two need opposite advice —
        # "sign in" versus "pick another name".
        current = session.get(AllowedEmail, invite_id)
        if current is not None and current.accepted_at is not None:
            raise AlreadyAccepted(f"{email} has already registered.")
        raise InviteError("That username is already taken.")

    logger.info("Invite %s accepted by %s as user %s", invite_id, email, user.id)
    return user


# ── Delivery bookkeeping ───────────────────────────────────────────────────

def mark_sent(session, invite, error=''):
    """Record that a send was attempted, whether or not the relay accepted it.

    Called on both outcomes. ``send_count`` counts attempts rather than successes,
    because "resent 4x, still not accepted" is the thing an admin needs to see, and
    an attempt that failed is exactly the one worth counting.

    A **successful** send clears ``last_send_error`` back to ``''``. Without that a
    row that failed once reads SEND_FAILED forever, so a later successful resend
    leaves the panel telling the admin to keep retrying a delivery that already
    worked.

    ``error`` is truncated to fit the column. It is rendered back to the admin and
    may reach a log, so callers must pass the relay's complaint and never anything
    from the message body.
    """
    invite.last_sent_at = datetime.utcnow()
    invite.send_count = (invite.send_count or 0) + 1
    invite.last_send_error = (error or '')[:MAX_SEND_ERROR_LEN]
    session.commit()
    if error:
        logger.warning("Invite %s to %s failed to send", invite.id, invite.email)


def check_resend_allowed(invite, *, cooldown_seconds=INVITE_RESEND_COOLDOWN_SECONDS,
                         now=None):
    """Raise :class:`ResendTooSoon` if this invite was sent within the cooldown.

    A pure check — it takes no session and writes nothing, so a route can ask
    before it does any of the work a send involves.

    An invite that has never been sent is always allowed, which is what makes the
    first send after a copy-link handover, and the *Send invite* on a LEGACY row,
    immediate rather than mysteriously refused.
    """
    if invite.last_sent_at is None:
        return

    now = now or datetime.utcnow()
    elapsed = (now - invite.last_sent_at).total_seconds()
    remaining = cooldown_seconds - elapsed
    if remaining > 0:
        # Rounded up: reporting "0 seconds" while still refusing sends the admin
        # back to a button that will refuse again.
        raise ResendTooSoon(math.ceil(remaining))
