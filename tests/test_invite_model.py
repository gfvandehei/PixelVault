"""The invite state machine on ``AllowedEmail``.

``state`` is a derived property, not a column, so these tests are about one thing:
that six mutually exclusive situations each map to the right name, and that the
*order* the branches are tried in survives edits. Precedence is the part worth
testing, because every pair below is a row where two branches are simultaneously
true and only the earlier one may win — an accepted invite whose TTL has since
lapsed is still ACCEPTED, a row with a delivery error is EXPIRED once its token
dies, and so on. Get the order wrong and every individual state still passes.

Rows are built by hand rather than through ``invites.py``, which does not exist
yet at this step and which these tests must not depend on either way: the model
is the contract, and ``issue`` / ``rotate`` / ``consume`` are just three of its
callers. The shapes assembled here are exactly the ones those functions will
produce — see docs/invite_registration_design.md §4 and §13.

Time moves by writing a timestamp, never by sleeping; the TTL is 72 hours in
this suite (``TEST_INVITE_TTL_HOURS``), so waiting one out is not an option.
"""

import hashlib
from datetime import datetime, timedelta

import pytest

from tests.conftest import TEST_INVITE_TTL_HOURS


def _token_hash(token="an-invite-token"):
    """The stored form of a token: sha256 hex, as ``invites.hash_token`` will produce."""
    return hashlib.sha256(token.encode()).hexdigest()


def _invite(email="invitee@example.com", **overrides):
    """An unsaved ``AllowedEmail`` in the shape ``issue()`` leaves behind.

    The default is the freshly-minted invite — token live, never emailed — because
    every other state is that row plus one or two more stamps, which is how the
    real lifecycle reaches them too.
    """
    from pixelvault.models import AllowedEmail

    now = datetime.utcnow()
    fields = dict(
        email=email,
        note='',
        added_at=now,
        token_hash=_token_hash(),
        token_issued_at=now,
        expires_at=now + timedelta(hours=TEST_INVITE_TTL_HOURS),
        prefill_username='',
        last_sent_at=None,
        send_count=0,
        last_send_error='',
        accepted_at=None,
        accepted_user_id=None,
        invited_by_id=None,
    )
    fields.update(overrides)
    return AllowedEmail(**fields)


def _past(hours=1):
    return datetime.utcnow() - timedelta(hours=hours)


# ── The six states ─────────────────────────────────────────────────────────

def test_a_row_with_no_token_is_legacy(app):
    """A whitelist entry that predates invites — the shape already in production.

    Nothing but ``email`` is set, which is all the old schema had and all the
    migration leaves behind. It must not read as ISSUED or SENT: an admin looking
    at this row has to be told the address can no longer register until someone
    issues it a link.
    """
    from pixelvault.models import InviteState

    row = _invite(token_hash=None, token_issued_at=None, expires_at=None)
    assert row.state is InviteState.LEGACY
    assert row.is_pending is False


def test_a_freshly_minted_token_that_was_never_emailed_is_issued(app):
    """The copy-link path, and what an invite issued with MAIL_ENABLED=false looks like."""
    from pixelvault.models import InviteState

    assert _invite().state is InviteState.ISSUED


def test_a_delivered_invite_is_sent(app):
    from pixelvault.models import InviteState

    row = _invite(last_sent_at=datetime.utcnow(), send_count=1)
    assert row.state is InviteState.SENT


def test_a_relay_failure_with_a_live_token_is_send_failed(app):
    """Delivery failed, the credential did not — which is why this is still pending.

    ``last_sent_at`` is stamped as well, because ``mark_sent`` records the attempt
    whether or not the relay accepted it. So this row is simultaneously "sent" and
    "failed", and the error must win.
    """
    from pixelvault.models import InviteState

    row = _invite(last_sent_at=datetime.utcnow(), send_count=1,
                  last_send_error='SMTPAuthenticationError: 535')
    assert row.state is InviteState.SEND_FAILED


def test_an_unclicked_token_past_its_ttl_is_expired(app):
    from pixelvault.models import InviteState

    row = _invite(expires_at=_past(), last_sent_at=_past(hours=TEST_INVITE_TTL_HOURS + 2),
                  send_count=1)
    assert row.state is InviteState.EXPIRED
    assert row.is_pending is False


def test_a_consumed_invite_is_accepted(app):
    """The post-``consume`` shape: token nulled, acceptance stamped.

    Nulling ``token_hash`` is what makes the link single-use, so this row would
    read as LEGACY if ACCEPTED did not come first — it would offer the admin a
    "Send invite" button for an address that already has an account.
    """
    from pixelvault.models import InviteState

    row = _invite(token_hash=None, accepted_at=datetime.utcnow(), accepted_user_id=1)
    assert row.state is InviteState.ACCEPTED
    assert row.is_pending is False


# ── Precedence between them ────────────────────────────────────────────────

def test_acceptance_outranks_an_elapsed_ttl(app):
    """``expires_at`` is not cleared on acceptance, so it keeps lapsing afterwards.

    Every accepted invite eventually reaches this state — the TTL passes days
    later while the account is in daily use. Reporting EXPIRED there would tell an
    admin to resend an invite to someone who already has a login.
    """
    from pixelvault.models import InviteState

    row = _invite(token_hash=None, expires_at=_past(), accepted_at=datetime.utcnow(),
                  accepted_user_id=1)
    assert row.state is InviteState.ACCEPTED


def test_acceptance_outranks_a_recorded_send_failure(app):
    """A failed send followed by a successful copy-link handover ends ACCEPTED.

    ``last_send_error`` is only cleared by another send, and the copy-link path
    never sends, so a row can be both accepted and carrying a stale error.
    """
    from pixelvault.models import InviteState

    row = _invite(token_hash=None, last_send_error='Connection refused',
                  accepted_at=datetime.utcnow(), accepted_user_id=1)
    assert row.state is InviteState.ACCEPTED


def test_a_legacy_row_is_never_reported_as_expired(app):
    """A tokenless row with a stale ``expires_at`` — a lapsed invite that was consumed
    by nothing and then had its token cleared. There is no live credential to
    expire, and the fix is to issue, not to resend."""
    from pixelvault.models import InviteState

    row = _invite(token_hash=None, expires_at=_past())
    assert row.state is InviteState.LEGACY


def test_an_expired_token_outranks_its_delivery_error(app):
    """Both branches are true; the dead token is the one that matters.

    Retrying delivery of a link that no longer opens is wasted mail. Ranking
    EXPIRED first points the admin at rotate-and-resend instead of resend alone.
    """
    from pixelvault.models import InviteState

    row = _invite(expires_at=_past(), last_sent_at=_past(hours=99), send_count=2,
                  last_send_error='450 mailbox unavailable')
    assert row.state is InviteState.EXPIRED


def test_a_delivery_error_outranks_a_successful_looking_send(app):
    """Distinguishes "the relay took it" from "the relay refused it".

    Without SEND_FAILED ranking above SENT, a bounced invite is indistinguishable
    in the admin panel from one sitting in someone's inbox, and nobody resends it.
    """
    from pixelvault.models import InviteState

    row = _invite(last_sent_at=datetime.utcnow(), send_count=3,
                  last_send_error='550 5.1.1 user unknown')
    assert row.state is InviteState.SEND_FAILED


def test_expiry_is_inclusive_of_the_boundary(app):
    """At exactly ``expires_at`` the invite is already dead, per ``now >= expires_at``.

    Pinned because ``validate()`` in step 3 must refuse the same instant this
    property calls expired; a strict ``>`` here would leave a one-tick window in
    which the panel says EXPIRED and the link still works.
    """
    from pixelvault.models import InviteState

    row = _invite(expires_at=datetime.utcnow() - timedelta(microseconds=1))
    assert row.state is InviteState.EXPIRED


# ── is_pending ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("overrides, expected_state, pending", [
    ({}, "ISSUED", True),
    ({"last_sent_at": datetime.utcnow(), "send_count": 1}, "SENT", True),
    ({"last_send_error": "boom"}, "SEND_FAILED", True),
    ({"expires_at": _past()}, "EXPIRED", False),
    ({"token_hash": None, "expires_at": None}, "LEGACY", False),
    ({"token_hash": None, "accepted_at": datetime.utcnow()}, "ACCEPTED", False),
])
def test_is_pending_covers_exactly_the_states_with_a_usable_token(
        app, overrides, expected_state, pending):
    """Pending means: a link exists, it still works, nobody has used it.

    The three false cases each need a different admin action — issue, rotate, or
    nothing at all — which is why they are grouped apart from the three that only
    need patience.
    """
    from pixelvault.models import InviteState

    row = _invite(**overrides)
    assert row.state is getattr(InviteState, expected_state)
    assert row.is_pending is pending


# ── Derivation, not storage ────────────────────────────────────────────────

def test_state_follows_the_clock_with_no_write_to_the_row(app):
    """The whole argument for a property: SENT becomes EXPIRED unattended.

    A stored column would still read SENT here, because nothing wrote to the row
    between the two assertions — the TTL simply passed.
    """
    from pixelvault.models import InviteState

    row = _invite(last_sent_at=datetime.utcnow(), send_count=1)
    assert row.state is InviteState.SENT

    row.expires_at = _past()
    assert row.state is InviteState.EXPIRED


def test_state_survives_a_round_trip_through_the_database(app):
    """Every column the property reads must actually persist.

    Building rows in memory would not catch a column missing from the table or
    dropped by the migration; this reloads from SQLite and asks again.
    """
    from pixelvault.extensions import db
    from pixelvault.models import AllowedEmail, InviteState

    with app.app_context():
        db.session.add(_invite(last_sent_at=datetime.utcnow(), send_count=2,
                               prefill_username='invitee',
                               last_send_error='timed out'))
        db.session.commit()
        db.session.expunge_all()

        row = db.session.query(AllowedEmail).filter_by(email='invitee@example.com').one()
        assert row.state is InviteState.SEND_FAILED
        assert row.is_pending is True
        assert row.send_count == 2
        assert row.prefill_username == 'invitee'
        assert row.token_hash == _token_hash()


def test_a_row_inserted_with_only_an_email_reads_as_legacy(app):
    """What ``POST /admin/email/add`` produced before this feature, and what the
    migration leaves in a live database: no invite columns touched at all.

    The model defaults have to land on their own — ``send_count`` at 0 and
    ``last_send_error`` at ``''``, not NULL — or the property's truthiness tests
    would misfire on a row nobody stamped.
    """
    from pixelvault.extensions import db
    from pixelvault.models import AllowedEmail, InviteState

    with app.app_context():
        db.session.add(AllowedEmail(email='old@example.com'))
        db.session.commit()
        db.session.expunge_all()

        row = db.session.query(AllowedEmail).filter_by(email='old@example.com').one()
        assert row.state is InviteState.LEGACY
        assert row.is_pending is False
        assert row.send_count == 0
        assert row.last_send_error == ''
        assert row.token_hash is None


# ── The enum itself is part of the §13 contract ────────────────────────────

def test_invite_state_members_are_the_six_from_the_design(app):
    """Pinned because step 5's admin panel and its templates key off these names."""
    from pixelvault.models import InviteState

    assert {s.name for s in InviteState} == {
        'ACCEPTED', 'LEGACY', 'EXPIRED', 'SEND_FAILED', 'ISSUED', 'SENT',
    }


def test_invite_state_is_a_string_enum(app):
    """So a Jinja template can compare a state to a plain string without ``.value``."""
    from pixelvault.models import InviteState

    assert isinstance(InviteState.SENT, str)
    assert InviteState.SENT == 'sent'
