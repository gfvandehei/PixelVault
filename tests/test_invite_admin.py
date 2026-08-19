"""The admin half of invites: issuing, resending, the copy-link fallback, revoking.

These are the routes where the three invite modules meet, so the tests are about
the *seams* rather than about any one of them:

* the lifecycle (``invites.py``) and delivery (``emails.py``) are ordered so that a
  relay outage cannot cost an invite — the row commits first, and the token minted
  before a failed send still validates afterwards;
* a renewal always rotates, so proving the new token works is only half the
  assertion — the old one must have stopped working in the same request;
* the copy-link fallback exists precisely for the case where mail is broken, so it
  is asserted to send *nothing* and to build its URL from ``PUBLIC_BASE_URL``
  rather than from the request's ``Host`` (design §6);
* and every one of these routes creates or hands out a credential, so the
  authorisation boundary gets direct tests rather than being assumed from the
  decorators.

Tokens are read back out of the ``MemoryMailer`` outbox and out of the flashed
link, because that is the only place a plaintext token ever exists — the database
holds a SHA-256. Anything a test cannot get that way, an admin cannot get either.

Cooldown and TTL come from ``conftest._TEST_ENV`` (60s, 72h) and are moved by
backdating rows, never by sleeping.
"""

import re
from datetime import datetime, timedelta

import pytest

from pixelvault import invites
from pixelvault.extensions import db
from pixelvault.mailer import MailError
from pixelvault.models import AllowedEmail, InviteState, User

from tests.conftest import TEST_PUBLIC_BASE_URL, Ref, _make_user, login

#: The link as it appears in both an email body and a flashed message. The token
#: alphabet is ``secrets.token_urlsafe``'s, which needs no HTML escaping — so the
#: same pattern works against rendered markup.
INVITE_LINK_RE = re.compile(
    re.escape(TEST_PUBLIC_BASE_URL) + r"/invite/([A-Za-z0-9_-]{20,})"
)

ADDRESS = "newcomer@example.com"


# ── Helpers ────────────────────────────────────────────────────────────────

class RefusingMailer:
    """A relay that accepts the connection and then rejects the message.

    The interesting failure shape: composition succeeded, so the token exists and
    the row is stamped, and only delivery is lost. It keeps what it was handed in
    :attr:`attempted` so a test can still read the token an invitee never received
    — which is how "the invite survived the outage" is proved rather than assumed.
    """

    def __init__(self, reason="relay refused: 550 5.7.1 message rejected"):
        self.attempted = []
        self.reason = reason

    def send(self, message):
        self.attempted.append(message)
        raise MailError(self.reason)


@pytest.fixture
def broken_mailer(app):
    """Swap in a relay that always refuses, the way the ``mailer`` fixture swaps in memory."""
    previous = app.extensions.get("mailer")
    refusing = RefusingMailer()
    app.extensions["mailer"] = refusing
    yield refusing
    app.extensions["mailer"] = previous


def token_in(message):
    """Return the invite token carried by an ``EmailMessage``'s plain-text part.

    The text part specifically: it is the one the design guarantees is complete
    and clickable, so reading the token from anywhere else would let a broken
    text part pass unnoticed.
    """
    body = message.get_body(preferencelist=("plain",)).get_content()
    match = INVITE_LINK_RE.search(body)
    assert match, f"no invite link in the text part:\n{body}"
    return match.group(1)


def token_in_page(response):
    """Return the invite token flashed onto a rendered page."""
    match = INVITE_LINK_RE.search(response.get_data(as_text=True))
    assert match, "no invite link on the page"
    return match.group(1)


def row_for(app, email):
    """Snapshot the ``AllowedEmail`` row for an address, or None."""
    with app.app_context():
        row = db.session.query(AllowedEmail).filter_by(email=email).one_or_none()
        if row is None:
            return None
        return Ref(
            id=row.id,
            email=row.email,
            note=row.note,
            prefill_username=row.prefill_username,
            state=row.state.value,
            has_token=row.token_hash is not None,
            token_hash=row.token_hash,
            send_count=row.send_count,
            last_send_error=row.last_send_error,
            last_sent_at=row.last_sent_at,
            expires_at=row.expires_at,
            invited_by_id=row.invited_by_id,
        )


def validates(app, token):
    """Return the address a token authorizes, or None if it no longer works."""
    with app.app_context():
        try:
            return invites.validate(db.session, token).email
        except invites.InviteError:
            return None


def make_legacy(app, email="oldtimer@example.com", note="from before invites"):
    """Create a whitelist row of the kind that predates this feature: no token at all.

    Written straight to the model rather than through ``invites.issue``, because
    ``issue`` cannot produce one — which is the whole point. Every allowed_email row
    in the existing production database looks like this, and both admin actions have
    to work on it or those people are stranded once /register goes away.
    """
    with app.app_context():
        row = AllowedEmail(email=email, note=note)
        db.session.add(row)
        db.session.commit()
        assert row.state is InviteState.LEGACY
        return Ref(id=row.id, email=row.email)


def make_invite(app, email=ADDRESS, **stamps):
    """Issue an invite directly and return ``(Ref, plaintext_token)``.

    For tests about resend and copy-link, which need a row that already exists
    without going through the add form first. ``stamps`` are attributes written
    onto the row afterwards — backdating ``last_sent_at`` past the cooldown, or
    ``expires_at`` into the past.
    """
    with app.app_context():
        invite, token = invites.issue(db.session, email)
        for field, value in stamps.items():
            setattr(invite, field, value)
        db.session.commit()
        return Ref(id=invite.id, email=invite.email), token


def cooled_down():
    """A ``last_sent_at`` far enough in the past that the resend cooldown has lapsed."""
    return datetime.utcnow() - timedelta(hours=1)


# ── Issuing (design §7.1) ──────────────────────────────────────────────────

def test_adding_an_email_issues_an_invite_and_sends_exactly_one_message(
        app, admin_client, admin_user, mailer):
    """The whole point of the change: one admin action, one live invite, one email."""
    response = admin_client.post('/admin/email/add', data={
        'email': ADDRESS,
        'note': 'Alice from the climbing gym',
        'prefill_username': 'alice',
    })

    assert response.status_code == 302
    assert len(mailer.outbox) == 1

    message = mailer.outbox[0]
    assert message['To'] == ADDRESS
    # The token in the email is the token the app will accept. Anything less than
    # this round trip would pass with a message that carries a stale or wrong link.
    assert validates(app, token_in(message)) == ADDRESS

    row = row_for(app, ADDRESS)
    assert row.state == 'sent'
    assert row.send_count == 1
    assert row.last_send_error == ''
    assert row.note == 'Alice from the climbing gym'
    assert row.prefill_username == 'alice'
    # Audit trail: which admin issued it (design §4).
    assert row.invited_by_id == admin_user.id


def test_the_address_is_normalised_before_it_becomes_a_row(app, admin_client, mailer):
    """Case and surrounding whitespace must not be able to mint a second whitelist entry."""
    admin_client.post('/admin/email/add', data={'email': '  NewComer@Example.COM  '})

    assert row_for(app, ADDRESS) is not None


@pytest.mark.parametrize('bad', ['not-an-email', 'missing@tld', 'two@@at.example.com', ''])
def test_an_address_registration_would_reject_is_refused_here_too(
        app, admin_client, mailer, bad):
    """The add form now enforces RE_EMAIL, the pattern acceptance enforces.

    The old ``'@' in email`` check was the weaker of the two, so an address could be
    authorized that could never be used — and now, one that an invite email would be
    fired at pointlessly.
    """
    admin_client.post('/admin/email/add', data={'email': bad})

    with app.app_context():
        assert db.session.query(AllowedEmail).count() == 0
    assert mailer.outbox == []


def test_a_send_failure_still_leaves_a_usable_invite(app, admin_client, broken_mailer):
    """An SMTP outage must cost the delivery, never the invite (design §7.1).

    The row is committed before anything is sent, so what is left behind is a live
    token, an honest SEND_FAILED state, and the relay's own complaint — everything
    an admin needs to resend or to hand the link over by other means.
    """
    response = admin_client.post('/admin/email/add', data={'email': ADDRESS},
                                 follow_redirects=True)

    row = row_for(app, ADDRESS)
    assert row is not None
    assert row.state == 'send_failed'
    assert row.send_count == 1
    assert broken_mailer.reason in row.last_send_error
    # The credential itself is untouched: the token composed into the message the
    # relay bounced is still the one the app would accept.
    assert validates(app, token_in(broken_mailer.attempted[0])) == ADDRESS
    # And the admin is pointed at the fallback rather than left to guess.
    assert 'Copy link' in response.get_data(as_text=True)


def test_adding_an_address_that_already_has_an_account_is_refused(
        app, admin_client, mailer):
    """Nothing to invite — and no email fired at someone who is already a member."""
    with app.app_context():
        _make_user('bob', ADDRESS)

    response = admin_client.post('/admin/email/add', data={'email': ADDRESS},
                                 follow_redirects=True)

    page = response.get_data(as_text=True)
    assert 'already has an account' in page
    assert mailer.outbox == []
    with app.app_context():
        assert db.session.query(AllowedEmail).count() == 0


def test_adding_an_address_that_is_already_invited_points_at_resend(
        app, admin_client, mailer):
    """The distinct refusal: the invite exists, so the action wanted is Resend.

    Answering this with the same message as the already-registered case is what
    the old route did, and it is a dead end — the admin is told "no" without being
    told what to do instead.
    """
    make_invite(app)

    response = admin_client.post('/admin/email/add', data={'email': ADDRESS},
                                 follow_redirects=True)

    page = response.get_data(as_text=True)
    assert 'already been invited' in page
    assert 'Resend' in page
    # Distinct from the already-registered wording, not a shared shrug.
    assert 'already has an account' not in page
    assert mailer.outbox == []


# ── Resend (design §7.4) ───────────────────────────────────────────────────

def test_resend_rotates_the_token_and_sends_again(app, admin_client, mailer):
    """Rotation is half the assertion: the link that was superseded must be dead."""
    invite, first_token = make_invite(app, last_sent_at=cooled_down())

    response = admin_client.post(f'/admin/invite/{invite.id}/resend')

    assert response.status_code == 302
    assert len(mailer.outbox) == 1
    second_token = token_in(mailer.outbox[0])

    assert second_token != first_token
    assert validates(app, second_token) == ADDRESS
    # Hashing means a link cannot be re-shown, so a resend has to mint — and the
    # previous one stops working the moment it does.
    assert validates(app, first_token) is None

    row = row_for(app, ADDRESS)
    assert row.state == 'sent'
    # One attempt, not two: make_invite issues without sending, so this resend is
    # the row's first actual delivery. mark_sent counts attempts and is called
    # exactly once per send — by emails.send_invite, never by the route.
    assert row.send_count == 1


def test_resend_inside_the_cooldown_says_when_and_does_not_rotate(
        app, admin_client, mailer):
    """A refusal must cost the invitee nothing: the link they hold keeps working.

    The cooldown is checked before rotating for exactly this reason. Rotating first
    and then refusing would break a live invite as a side effect of a throttle.
    """
    invite, token = make_invite(app, last_sent_at=datetime.utcnow())
    before = row_for(app, ADDRESS)

    response = admin_client.post(f'/admin/invite/{invite.id}/resend',
                                 follow_redirects=True)

    page = response.get_data(as_text=True)
    assert 'more seconds' in page  # names the wait rather than just saying no
    assert re.search(r'Wait \d+ more seconds', page)
    assert mailer.outbox == []

    after = row_for(app, ADDRESS)
    assert after.token_hash == before.token_hash
    assert after.send_count == before.send_count
    assert validates(app, token) == ADDRESS


def test_resend_on_a_failed_row_clears_the_error_when_it_works(
        app, admin_client, mailer, broken_mailer):
    """SEND_FAILED must be recoverable, or the panel nags about a delivery that worked.

    ``emails.send_invite`` owns that bookkeeping through ``mark_sent`` — the routes
    never call it themselves, which is what keeps ``send_count`` honest.
    """
    admin_client.post('/admin/email/add', data={'email': ADDRESS})
    assert row_for(app, ADDRESS).state == 'send_failed'

    invite = row_for(app, ADDRESS)
    with app.app_context():
        row = db.session.get(AllowedEmail, invite.id)
        row.last_sent_at = cooled_down()
        db.session.commit()

    app.extensions['mailer'] = mailer  # the relay comes back
    admin_client.post(f'/admin/invite/{invite.id}/resend')

    row = row_for(app, ADDRESS)
    assert row.state == 'sent'
    assert row.last_send_error == ''
    assert row.send_count == 2


def test_resend_on_an_accepted_invite_is_a_no_op(app, admin_client, mailer):
    """Acceptance is terminal; re-minting there would put a live credential on a finished row."""
    invite, _ = make_invite(app, last_sent_at=cooled_down())
    with app.app_context():
        row = db.session.get(AllowedEmail, invite.id)
        user = _make_user('newcomer', ADDRESS)
        row.accepted_at = datetime.utcnow()
        row.accepted_user_id = user.id
        row.token_hash = None
        db.session.commit()

    response = admin_client.post(f'/admin/invite/{invite.id}/resend',
                                 follow_redirects=True)

    assert 'already registered' in response.get_data(as_text=True)
    assert mailer.outbox == []
    assert row_for(app, ADDRESS).state == 'accepted'


# ── Copy-link fallback (design §7.3) ───────────────────────────────────────

def test_copy_link_rotates_and_flashes_a_url_without_sending_mail(
        app, admin_client, mailer):
    """The path that makes SMTP optional rather than a hard dependency of registration."""
    invite, first_token = make_invite(app)

    response = admin_client.post(f'/admin/invite/{invite.id}/link',
                                 follow_redirects=True)

    assert mailer.outbox == []  # nothing left the process

    shown = token_in_page(response)
    assert shown != first_token
    assert validates(app, shown) == ADDRESS
    assert validates(app, first_token) is None

    # Nothing was sent, so the row must not claim otherwise.
    row = row_for(app, ADDRESS)
    assert row.state == 'issued'
    assert row.send_count == 0
    assert row.last_sent_at is None


def test_the_flashed_link_is_built_from_public_base_url(app, admin_client, mailer):
    """From the configured origin, not the one the request arrived on (design §6).

    ``url_for(_external=True)`` would reconstruct ``http://localhost`` here, and in
    production it reconstructs whatever ``Host`` and the forwarded headers claim —
    which is how a spoofed header aims a real invite token at someone else's domain.
    The absence of the request's own origin from the flashed link is the assertion.
    """
    invite, _ = make_invite(app)

    response = admin_client.post(f'/admin/invite/{invite.id}/link',
                                 follow_redirects=True)

    page = response.get_data(as_text=True)
    assert f'{TEST_PUBLIC_BASE_URL}/invite/' in page
    assert 'localhost/invite/' not in page


def test_copy_link_is_available_immediately_after_a_failed_send(
        app, admin_client, broken_mailer):
    """The fallback must not be gated by the send cooldown.

    It sends nothing, so the mail-bomb argument the cooldown exists for does not
    apply — and gating it would disable the fallback during the exact minute after
    a failed send, which is when an admin reaches for it.
    """
    admin_client.post('/admin/email/add', data={'email': ADDRESS})
    invite = row_for(app, ADDRESS)

    response = admin_client.post(f'/admin/invite/{invite.id}/link',
                                 follow_redirects=True)

    assert validates(app, token_in_page(response)) == ADDRESS


# ── LEGACY rows: the existing production whitelist ─────────────────────────

def test_send_invite_works_on_a_legacy_row(app, admin_client, mailer):
    """A whitelist entry with no token is un-stranded by the same route Resend uses.

    This is the migration path for every address already in the database. Without
    it those people are silently unable to register once /register goes away in
    step 7 (design §11 Q9).
    """
    legacy = make_legacy(app)

    response = admin_client.post(f'/admin/invite/{legacy.id}/resend')

    assert response.status_code == 302
    assert len(mailer.outbox) == 1
    assert validates(app, token_in(mailer.outbox[0])) == legacy.email

    row = row_for(app, legacy.email)
    assert row.state == 'sent'
    assert row.note == 'from before invites'  # the row is renewed, not replaced


def test_copy_link_works_on_a_legacy_row(app, admin_client, mailer):
    """The same un-stranding without a relay, for a self-hoster who has no SMTP at all."""
    legacy = make_legacy(app)

    response = admin_client.post(f'/admin/invite/{legacy.id}/link',
                                 follow_redirects=True)

    assert validates(app, token_in_page(response)) == legacy.email
    assert mailer.outbox == []
    assert row_for(app, legacy.email).state == 'issued'


# ── Revoking (design §7.5) ─────────────────────────────────────────────────

def test_removing_a_pending_invite_kills_its_link(app, admin_client, mailer):
    """Revocation needs no separate mechanism — the token hash lives on the deleted row."""
    invite, token = make_invite(app)

    admin_client.post(f'/admin/email/{invite.id}/remove')

    assert row_for(app, ADDRESS) is None
    assert validates(app, token) is None


def test_removing_an_accepted_invite_does_not_touch_the_account(app, admin_client):
    """Deleting the record of an invite must not delete the person it produced.

    ``accepted_user_id`` is a plain nullable FK with no cascade behind it, and this
    test is what keeps it that way: adding a relationship with
    ``cascade='all, delete-orphan'`` later would turn "remove from the list" into
    "delete the user and every album they own".
    """
    invite, _ = make_invite(app)
    with app.app_context():
        user = _make_user('newcomer', ADDRESS)
        row = db.session.get(AllowedEmail, invite.id)
        row.accepted_at = datetime.utcnow()
        row.accepted_user_id = user.id
        row.token_hash = None
        db.session.commit()
        user_id = user.id

    response = admin_client.post(f'/admin/email/{invite.id}/remove',
                                 follow_redirects=True)

    assert row_for(app, ADDRESS) is None
    with app.app_context():
        survivor = db.session.get(User, user_id)
        assert survivor is not None
        assert survivor.email == ADDRESS
    assert 'account is untouched' in response.get_data(as_text=True)


# ── The panel ──────────────────────────────────────────────────────────────

def _one_of_each_state(app):
    """Create six rows, one in each InviteState, and return them by state name."""
    rows = {}
    with app.app_context():
        accepted, _ = invites.issue(db.session, 'accepted@example.com')
        accepted.accepted_at = datetime.utcnow()
        accepted.token_hash = None

        invites.issue(db.session, 'issued@example.com')

        sent, _ = invites.issue(db.session, 'sent@example.com')
        sent.last_sent_at = datetime.utcnow()
        sent.send_count = 1

        failed, _ = invites.issue(db.session, 'failed@example.com')
        failed.last_sent_at = datetime.utcnow()
        failed.send_count = 3
        failed.last_send_error = 'relay refused: 550 mailbox full'

        expired, _ = invites.issue(db.session, 'expired@example.com')
        expired.expires_at = datetime.utcnow() - timedelta(hours=1)

        db.session.add(AllowedEmail(email='legacy@example.com'))
        db.session.commit()

        for row in db.session.query(AllowedEmail).all():
            rows[row.state.value] = row.email
    return rows


def test_the_panel_shows_every_state_and_the_actions_that_fit_it(app, admin_client):
    """Six states, six legible badges, and only the buttons that can do anything."""
    states = _one_of_each_state(app)
    assert set(states) == {'accepted', 'issued', 'sent', 'send_failed', 'expired', 'legacy'}

    page = admin_client.get('/admin').get_data(as_text=True)

    for label in ('accepted', 'not emailed', 'sent', 'send failed', 'expired', 'no invite'):
        assert f'>{label}</span>' in page, f'missing badge for {label}'

    # A row that has never been emailed offers "Send invite"; one that has offers
    # "Resend". Same route, and the label is the only thing that differs.
    assert 'Send invite' in page
    assert 'Resend' in page
    # Delivery trouble is visible without opening a log.
    assert 'relay refused: 550 mailbox full' in page
    assert '3 attempts' in page

    with app.app_context():
        accepted_id = db.session.query(AllowedEmail).filter_by(
            email=states['accepted']).one().id
    # An accepted invite has no live token, so neither renewal button is offered.
    assert f'/admin/invite/{accepted_id}/resend' not in page
    assert f'/admin/invite/{accepted_id}/link' not in page


def test_admin_supplied_text_and_relay_errors_are_escaped(app, admin_client):
    """Everything on this page is either admin input or a string from a remote relay."""
    with app.app_context():
        row = AllowedEmail(
            email='hostile@example.com',
            note='<script>alert(1)</script>',
            prefill_username='<img src=x onerror=alert(2)>',
        )
        row.last_send_error = '<b>550</b> "rejected"'
        db.session.add(row)
        db.session.commit()

    page = admin_client.get('/admin').get_data(as_text=True)

    assert '<script>alert(1)</script>' not in page
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page
    # The attribute payload survives as text; what must not survive is the tag
    # around it, which is the difference between displayed and executed.
    assert '<img src=x' not in page
    assert '&lt;img src=x onerror=alert(2)&gt;' in page
    assert '<b>550</b>' not in page
    assert '&lt;b&gt;550&lt;/b&gt;' in page


# ── Authorisation ──────────────────────────────────────────────────────────

#: Every route this step adds or reworks, as (path template, form data).
INVITE_ROUTES = [
    ('/admin/email/add', {'email': 'intruder@example.com'}),
    ('/admin/invite/{id}/resend', {}),
    ('/admin/invite/{id}/link', {}),
    ('/admin/email/{id}/remove', {}),
]


@pytest.mark.parametrize('path,data', INVITE_ROUTES)
def test_a_non_admin_cannot_reach_any_invite_route(app, client, mailer, path, data):
    """The boundary that matters: these routes mint credentials and send mail.

    Tested directly rather than inferred from ``@admin_required`` being present,
    because a decorator can be dropped in a refactor and nothing else here would
    notice — an ordinary user could then invite themselves an accomplice.
    """
    invite, token = make_invite(app)

    response = client.post(path.format(id=invite.id), data=data)

    assert response.status_code == 403
    assert mailer.outbox == []
    # Nothing moved: no new row, and the existing token neither rotated nor died.
    with app.app_context():
        assert db.session.query(AllowedEmail).filter_by(
            email='intruder@example.com').count() == 0
    assert validates(app, token) == ADDRESS


@pytest.mark.parametrize('path,data', INVITE_ROUTES)
def test_an_anonymous_caller_cannot_reach_any_invite_route(
        app, anon_client, mailer, path, data):
    """Logged out gets the login page, not an invite."""
    invite, token = make_invite(app)

    response = anon_client.post(path.format(id=invite.id), data=data)

    assert response.status_code == 302
    assert '/login' in response.headers['Location']
    assert mailer.outbox == []
    with app.app_context():
        assert db.session.query(AllowedEmail).filter_by(
            email='intruder@example.com').count() == 0
    assert validates(app, token) == ADDRESS


def test_an_unknown_invite_id_is_a_404_not_a_500(app, admin_client, mailer):
    """Both new routes take an id straight off the URL."""
    assert admin_client.post('/admin/invite/9999/resend').status_code == 404
    assert admin_client.post('/admin/invite/9999/link').status_code == 404


# ── Rate limits (design §10) ───────────────────────────────────────────────

#: Both new admin routes are 30/hour, keyed on the user by ``rate_limit_key``.
INVITE_ACTION_BUDGET = 30


def test_resend_is_capped_at_thirty_an_hour(app, admin_client, mailer):
    """A resend button is a mail-send button pointed at a third party.

    The cooldown bounds how *often* one invite can be mailed; this bounds how many
    times the button can be pressed at all, across every row, from one account.
    """
    invite, _ = make_invite(app, last_sent_at=cooled_down())

    statuses = [admin_client.post(f'/admin/invite/{invite.id}/resend').status_code
                for _ in range(INVITE_ACTION_BUDGET + 1)]

    assert statuses[INVITE_ACTION_BUDGET - 1] == 302
    assert statuses[INVITE_ACTION_BUDGET] == 429


def test_copy_link_is_capped_at_thirty_an_hour(app, admin_client, mailer):
    """Sends no mail, but every press mints a credential and revokes the last one."""
    invite, _ = make_invite(app)

    statuses = [admin_client.post(f'/admin/invite/{invite.id}/link').status_code
                for _ in range(INVITE_ACTION_BUDGET + 1)]

    assert statuses[INVITE_ACTION_BUDGET - 1] == 302
    assert statuses[INVITE_ACTION_BUDGET] == 429


def test_the_limits_are_per_user_not_global(app, admin_client, mailer):
    """One admin exhausting their budget must not lock the other admins out.

    ``rate_limit_key`` buckets on the user id for authenticated routes, so this is
    really a check that these two routes did not opt into an IP key by accident —
    every admin in a self-hosted deployment shares an office IP.
    """
    invite, _ = make_invite(app, last_sent_at=cooled_down())
    for _ in range(INVITE_ACTION_BUDGET + 1):
        admin_client.post(f'/admin/invite/{invite.id}/resend')

    with app.app_context():
        second_admin = _make_user('root2', 'root2@example.com', is_admin=True)
    other = app.test_client()
    login(other, second_admin)

    assert other.post(f'/admin/invite/{invite.id}/link').status_code == 302
