"""The invite lifecycle in ``pixelvault.invites`` — mint, validate, burn.

These tests are deliberately HTTP-free. ``invites.py`` is plain functions over the
ORM session precisely so the rules can be exercised without a request context, and
testing them here rather than through a route is what keeps them honest when steps
5 and 6 wrap them in flashes and redirects: an assertion about a status code would
pass just as happily against a route that forgot to re-validate.

Two habits from the rest of the suite carry over:

* **Time moves by writing a timestamp, never by sleeping.** ``config.py`` binds the
  TTL and the cooldown at import (see ``conftest.py``), so they are 72 hours and 60
  seconds for the whole run and waiting one out is not an option. Every "later"
  below is a backdated column or an explicit ``now=`` argument.
* **The plaintext token is checked for absence, not just presence.** It is a bearer
  credential that creates an account bound to a real person's address, so several
  tests assert where it *isn't* — on the row, in a log line, or still working after
  a rotation.
"""

import hashlib
import logging
import re
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session as SASession

from pixelvault import invites
from pixelvault.extensions import db
from pixelvault.models import AllowedEmail, InviteState, User

from tests.conftest import TEST_INVITE_COOLDOWN_SECONDS, TEST_INVITE_TTL_HOURS

PASSWORD = "correct horse battery staple"


@pytest.fixture
def session(app):
    """The ORM session every function under test takes as its first argument.

    An app context is pushed for its lifetime because Flask-SQLAlchemy scopes
    ``db.session`` to one, and because ``db.engine`` — needed by the race test —
    resolves through the same context.
    """
    with app.app_context():
        yield db.session


def _issue(session, email="invitee@example.com", **kwargs):
    return invites.issue(session, email, **kwargs)


def _backdate(session, invite, **delta):
    """Move an invite's clock into the past by rewriting the columns that carry it."""
    shift = timedelta(**delta)
    if invite.token_issued_at is not None:
        invite.token_issued_at -= shift
    if invite.expires_at is not None:
        invite.expires_at -= shift
    if invite.last_sent_at is not None:
        invite.last_sent_at -= shift
    session.commit()


# ── Issuing ────────────────────────────────────────────────────────────────

def test_hash_token_is_a_plain_sha256_hexdigest(app):
    """Pinned because the column is ``String(64)`` and the model tests assume this form.

    Anything else — a salted hash, a different digest, base64 — either overflows
    the column or silently stops matching rows written by an earlier release.
    """
    digest = invites.hash_token("a-token")
    assert digest == hashlib.sha256(b"a-token").hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_issue_returns_a_plaintext_whose_hash_is_what_the_row_stores(session):
    """The whole point of hashing at rest: the row cannot reproduce the link.

    The returned string is the only copy that will ever exist; from here it lives
    in an email and nowhere else.
    """
    invite, token = _issue(session)

    assert invite.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert invite.token_hash != token
    assert token not in (invite.email, invite.note, invite.prefill_username)
    assert invite.state is InviteState.ISSUED


def test_issue_stamps_the_token_its_issue_time_and_its_expiry_together(session):
    """All three or none. ``state`` reads ``expires_at`` directly and never re-derives
    it, so a row holding a token with a NULL expiry reports ISSUED forever — an
    immortal credential the admin panel swears is fine."""
    invite, _ = _issue(session)

    assert invite.token_hash is not None
    assert invite.token_issued_at is not None
    assert invite.expires_at is not None
    assert invite.expires_at - invite.token_issued_at == timedelta(hours=TEST_INVITE_TTL_HOURS)


def test_issue_normalises_the_address_it_authorizes(session):
    """The address is the whitelist key and, after acceptance, the account identity.

    Storing it as typed would let one person's invite authorize a second row that
    looks like a different address to every lookup that follows.
    """
    invite, _ = _issue(session, "  Invitee@Example.COM ")
    assert invite.email == "invitee@example.com"


def test_two_invites_never_share_a_token(session):
    first, first_token = _issue(session, "one@example.com")
    second, second_token = _issue(session, "two@example.com")

    assert first_token != second_token
    assert first.token_hash != second.token_hash


def test_issuing_an_address_twice_is_refused_and_leaves_the_session_usable(session):
    """The backstop for two admins clicking at once.

    The rollback is the load-bearing half: an un-rolled-back failed flush poisons
    every later statement on the session, so the admin request would fail on
    something unrelated further down instead of flashing "already invited".
    """
    _issue(session, "dup@example.com")

    with pytest.raises(invites.InviteError):
        _issue(session, "dup@example.com")

    assert session.query(AllowedEmail).count() == 1


# ── Validation ─────────────────────────────────────────────────────────────

def test_validate_returns_the_row_a_live_token_belongs_to(session):
    invite, token = _issue(session)
    assert invites.validate(session, token).id == invite.id


def test_validate_rejects_a_token_no_row_holds(session):
    _issue(session)
    with pytest.raises(invites.InvalidInvite):
        invites.validate(session, "not-a-real-token")


@pytest.mark.parametrize("token", ["", None])
def test_validate_rejects_an_absent_token_without_touching_the_database(session, token):
    """A missing session key reaches ``validate`` as ``None``; hashing it would
    raise ``AttributeError`` and surface as a 500 rather than as "check your link"."""
    with pytest.raises(invites.InvalidInvite):
        invites.validate(session, token)


def test_validate_reports_an_elapsed_ttl_as_expired_not_invalid(session):
    """The two need opposite advice — "ask for a new link" versus "check the one you have"."""
    invite, token = _issue(session)
    _backdate(session, invite, hours=TEST_INVITE_TTL_HOURS + 1)

    with pytest.raises(invites.ExpiredInvite):
        invites.validate(session, token)


def test_expiry_is_inclusive_of_the_boundary(session):
    """At exactly ``expires_at`` the link is already dead, matching ``AllowedEmail.state``.

    A strict ``>`` here would open a window in which the admin panel says EXPIRED
    and the link still works — the one disagreement between the two views that
    nobody could diagnose from the outside.
    """
    invite, token = _issue(session)
    deadline = invite.expires_at

    assert invites.validate(session, token, now=deadline - timedelta(microseconds=1))

    with pytest.raises(invites.ExpiredInvite):
        invites.validate(session, token, now=deadline)


def test_validate_refuses_a_token_with_no_expiry_at_all(session):
    """A row ``_mint`` never writes, so it is corruption or a hand-edited database.

    Fail closed: honouring it would mean a bearer credential that never dies, which
    is a worse outcome than refusing a row that should not exist.
    """
    invite, token = _issue(session)
    invite.expires_at = None
    session.commit()

    with pytest.raises(invites.ExpiredInvite):
        invites.validate(session, token)


def test_validate_reports_an_accepted_invite_as_accepted(session):
    """Acceptance is checked before expiry, in the same order as ``AllowedEmail.state``.

    The row is stamped by hand because the honest path cannot produce it: ``consume``
    nulls ``token_hash`` in the same transaction, so a replayed link takes the
    branch in the next test instead. This one pins the precedence, so an admin who
    clears an account's ``token_hash`` by hand — or a future path that stamps
    acceptance without burning the token — still gets "you already registered"
    rather than an expiry message about a row nobody can renew.
    """
    invite, token = _issue(session)
    invite.accepted_at = datetime.utcnow()
    invite.expires_at = datetime.utcnow() - timedelta(hours=1)
    session.commit()

    with pytest.raises(invites.AlreadyAccepted):
        invites.validate(session, token)


def test_a_consumed_link_stops_validating_entirely(session):
    """Documents the accepted cost of nulling the hash: a used link is indistinguishable
    from a typo, because there is nothing left on any row to match it against. The
    acceptance page in step 6 has to word ``InvalidInvite`` for both audiences."""
    invite, token = _issue(session)
    invites.consume(session, invite, username="invitee", password=PASSWORD)

    with pytest.raises(invites.InvalidInvite):
        invites.validate(session, token)


# ── Rotation ───────────────────────────────────────────────────────────────

def test_rotate_returns_a_different_token_and_kills_the_previous_link(session):
    """The consequence of hashing, and the safer default: a resend usually means the
    first link was lost or went somewhere it should not have."""
    invite, old_token = _issue(session)
    new_token = invites.rotate(session, invite)

    assert new_token != old_token
    assert invites.validate(session, new_token).id == invite.id
    with pytest.raises(invites.InvalidInvite):
        invites.validate(session, old_token)


def test_rotate_restarts_the_ttl_so_an_expired_invite_becomes_usable(session):
    """The EXPIRED -> resend path. Without a fresh expiry the new link would be born dead."""
    invite, _ = _issue(session)
    _backdate(session, invite, hours=TEST_INVITE_TTL_HOURS + 1)
    assert invite.state is InviteState.EXPIRED

    token = invites.rotate(session, invite)

    assert invites.validate(session, token).id == invite.id
    assert invite.expires_at > datetime.utcnow()


def test_rotate_issues_a_first_token_to_a_legacy_row(session):
    """The *Send invite* button on a whitelist entry that predates the feature.

    Those rows have no token and, once registration is link-only, no way in at all
    until an admin clicks. ``rotate`` is that click — there is no separate "issue on
    an existing row" path to get wrong.
    """
    legacy = AllowedEmail(email="old@example.com")
    session.add(legacy)
    session.commit()
    assert legacy.state is InviteState.LEGACY

    token = invites.rotate(session, legacy)

    assert legacy.state is InviteState.ISSUED
    assert invites.validate(session, token).id == legacy.id


def test_rotate_preserves_the_send_history(session):
    """``send_count`` is what tells an admin "resent 4x, still not accepted", and the
    recorded error stays true until another delivery is actually attempted."""
    invite, _ = _issue(session)
    invites.mark_sent(session, invite, error="450 mailbox unavailable")

    invites.rotate(session, invite)

    assert invite.send_count == 1
    assert invite.last_send_error == "450 mailbox unavailable"


def test_rotate_refuses_an_accepted_invite(session):
    """Acceptance is terminal. Re-minting here would hang a live credential off a
    finished row and put a *Resend* button in front of someone who already logs in."""
    invite, _ = _issue(session)
    invites.consume(session, invite, username="invitee", password=PASSWORD)

    with pytest.raises(invites.AlreadyAccepted):
        invites.rotate(session, invite)

    assert invite.token_hash is None


# ── Acceptance ─────────────────────────────────────────────────────────────

def test_consume_creates_a_working_account_with_the_email_from_the_invite_row(session):
    """The highest-value assertion in the feature.

    The address comes off the server-side row and is not a parameter at all, so
    there is no form field a holder of an invite for one address could use to
    register as another. The password is checked through ``check_password`` rather
    than by inspecting the hash, because ``consume`` must go through
    ``User.set_password`` and not hand-roll the hashing parameters.
    """
    invite, _ = _issue(session, "invitee@example.com")

    user = invites.consume(session, invite, username="invitee", password=PASSWORD)

    assert user.email == "invitee@example.com"
    assert user.username == "invitee"
    assert user.check_password(PASSWORD)
    assert user.is_admin is False


def test_consume_burns_the_invite_in_the_same_breath(session):
    """Acceptance stamped, token nulled, the account linked back — all committed once.

    Split across two transactions, a crash between them leaves either an account
    beside a still-live link that can be replayed, or a burnt invite with no account
    and an invitee locked out with nothing to click.
    """
    invite, _ = _issue(session)

    user = invites.consume(session, invite, username="invitee", password=PASSWORD)

    assert invite.accepted_at is not None
    assert invite.accepted_user_id == user.id
    assert invite.token_hash is None
    assert invite.state is InviteState.ACCEPTED
    assert invite.is_pending is False


def test_an_invite_can_only_be_consumed_once(session):
    invite, _ = _issue(session)
    invites.consume(session, invite, username="invitee", password=PASSWORD)

    with pytest.raises(invites.AlreadyAccepted):
        invites.consume(session, invite, username="invitee-again", password=PASSWORD)

    assert session.query(User).count() == 1


def test_consume_refuses_an_expired_invite_even_if_validate_was_skipped(session):
    """``consume`` re-checks rather than trusting the caller's earlier verdict: the
    GET that validated the link and the POST that accepts it are separate requests,
    and the TTL can lapse between them."""
    invite, _ = _issue(session)
    _backdate(session, invite, hours=TEST_INVITE_TTL_HOURS + 1)

    with pytest.raises(invites.ExpiredInvite):
        invites.consume(session, invite, username="invitee", password=PASSWORD)

    assert session.query(User).count() == 0


def test_two_consumes_racing_on_one_invite_produce_exactly_one_user(session, app):
    """A double-submit, or a link forwarded to a second person, must lose cleanly.

    The loser is driven from its own ORM session holding a copy of the row read
    *before* the winner committed — which is exactly what a second browser tab has.
    Its in-memory ``accepted_at`` is still None, so it sails past the guard and is
    stopped by the database instead: both consumes necessarily agree on the email,
    because the email comes from the invite, so the unique index on ``user.email``
    refuses the second insert whatever username it picked.
    """
    invite, _ = _issue(session, "race@example.com")
    invite_id = invite.id

    with SASession(bind=db.engine) as loser_session:
        stale = loser_session.get(AllowedEmail, invite_id)
        assert stale.accepted_at is None

        winner = invites.consume(session, invite, username="winner", password=PASSWORD)

        with pytest.raises(invites.AlreadyAccepted):
            invites.consume(loser_session, stale, username="loser", password=PASSWORD)

    users = session.query(User).all()
    assert [u.username for u in users] == ["winner"]
    assert invite.accepted_user_id == winner.id


def test_a_username_collision_is_not_reported_as_an_accepted_invite(session):
    """Both faults surface as an IntegrityError and need opposite advice — "sign in"
    versus "pick another name" — so ``consume`` re-reads the row to tell them apart."""
    session.add(User(username="taken", email="someone@example.com",
                     password_hash="pbkdf2:sha256:600000$test$deadbeef"))
    session.commit()
    invite, _ = _issue(session)

    with pytest.raises(invites.InviteError) as caught:
        invites.consume(session, invite, username="taken", password=PASSWORD)

    assert not isinstance(caught.value, invites.AlreadyAccepted)
    assert invite.accepted_at is None
    assert invite.token_hash is not None  # the link still works; only the name was bad


# ── Delivery bookkeeping ───────────────────────────────────────────────────

def test_mark_sent_records_a_successful_send(session):
    invite, _ = _issue(session)

    invites.mark_sent(session, invite)

    assert invite.send_count == 1
    assert invite.last_sent_at is not None
    assert invite.last_send_error == ""
    assert invite.state is InviteState.SENT


def test_a_successful_send_clears_a_previous_failure(session):
    """Otherwise a row that failed once reads SEND_FAILED forever, and the panel keeps
    telling the admin to retry a delivery that has already worked."""
    invite, _ = _issue(session)
    invites.mark_sent(session, invite, error="SMTPAuthenticationError: 535")
    assert invite.state is InviteState.SEND_FAILED

    invites.mark_sent(session, invite)

    assert invite.state is InviteState.SENT
    assert invite.last_send_error == ""
    assert invite.send_count == 2  # attempts, not successes


def test_a_failed_send_is_counted_and_its_error_truncated_to_the_column(session):
    """The attempt counts because a failed one is exactly the attempt worth counting,
    and SQLite would happily store an over-long relay complaint that then breaks on a
    backend which enforces VARCHAR length."""
    invite, _ = _issue(session)

    invites.mark_sent(session, invite, error="x" * 4000)

    assert len(invite.last_send_error) == invites.MAX_SEND_ERROR_LEN
    assert invite.send_count == 1
    assert invite.last_sent_at is not None
    assert invite.state is InviteState.SEND_FAILED


def test_mark_sent_survives_a_round_trip(session):
    """The stamps have to persist, not just live on the instance the caller holds."""
    invite, _ = _issue(session)
    invites.mark_sent(session, invite, error="boom")
    invite_id = invite.id
    session.expunge_all()

    reloaded = session.get(AllowedEmail, invite_id)
    assert reloaded.send_count == 1
    assert reloaded.last_send_error == "boom"


# ── Resend cooldown ────────────────────────────────────────────────────────

def test_a_never_sent_invite_may_always_be_sent(session):
    """Covers the first send after a copy-link handover and *Send invite* on a LEGACY
    row — neither should be refused by a cooldown that has nothing to measure from."""
    invite, _ = _issue(session)
    invites.check_resend_allowed(invite)  # must not raise


def test_a_second_send_inside_the_cooldown_is_refused(session):
    """The cooldown is not about admin patience: this is mail the server sends to a
    third party on request, so an unthrottled button is a mail-bomb primitive."""
    invite, _ = _issue(session)
    invites.mark_sent(session, invite)

    with pytest.raises(invites.ResendTooSoon) as caught:
        invites.check_resend_allowed(invite)

    assert 0 < caught.value.seconds_remaining <= TEST_INVITE_COOLDOWN_SECONDS


def test_the_refusal_says_how_long_is_left(session):
    """So the route can say "try again in 40 seconds" instead of an unqualified no."""
    invite, _ = _issue(session)
    invites.mark_sent(session, invite)
    _backdate(session, invite, seconds=TEST_INVITE_COOLDOWN_SECONDS - 40)

    with pytest.raises(invites.ResendTooSoon) as caught:
        invites.check_resend_allowed(invite)

    assert caught.value.seconds_remaining == 40


def test_the_cooldown_lapses(session):
    invite, _ = _issue(session)
    invites.mark_sent(session, invite)
    _backdate(session, invite, seconds=TEST_INVITE_COOLDOWN_SECONDS + 1)

    invites.check_resend_allowed(invite)  # must not raise


def test_the_cooldown_boundary_permits_the_send(session):
    """At exactly the cooldown nothing is remaining, so refusing would leave a button
    that says "wait 0 seconds" and then refuses again."""
    invite, _ = _issue(session)
    invites.mark_sent(session, invite)
    now = invite.last_sent_at + timedelta(seconds=TEST_INVITE_COOLDOWN_SECONDS)

    invites.check_resend_allowed(invite, now=now)  # must not raise


# ── The token never escapes ────────────────────────────────────────────────

def test_no_lifecycle_call_ever_logs_the_token(session, caplog):
    """The invariant the module's docstring opens with, asserted rather than asserted-to.

    A token in a log file is a copy of a credential that creates an account bound to
    someone's email address — and logs are the one place secrets travel furthest,
    into aggregators and crash reports. Log lines may name the invite or the address;
    never the secret.
    """
    caplog.set_level(logging.DEBUG, logger="pixelvault.invites")

    invite, first_token = _issue(session, "quiet@example.com")
    second_token = invites.rotate(session, invite)
    invites.mark_sent(session, invite, error="550 5.1.1 user unknown")
    invites.mark_sent(session, invite)
    invites.consume(session, invite, username="quiet", password=PASSWORD)

    assert caplog.text  # the calls above really did log something
    assert first_token not in caplog.text
    assert second_token not in caplog.text
