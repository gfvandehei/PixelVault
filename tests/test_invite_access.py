"""The acceptance flow as an authorisation boundary.

``test_invite_lifecycle.py`` proves the state machine in ``invites.py`` is correct.
This module proves the three HTTP routes over it cannot be talked out of it, which
is a different question: every test here is written from the position of someone
holding one invite link and wanting something it does not entitle them to.

The property the whole module exists for is the first section. An invitation
authorizes **one address**, and the address the account is created with is read off
the server-side row — never from the submitted form. If that ever stops being true
the whitelist becomes decorative: anyone who receives any invite can register as
anyone, including an address an admin has deliberately never authorized.

Everything else here is the supporting cast: the token must leave the URL, a dead
link must say *which* kind of dead it is, a live session must not be able to absorb
an invitation, and the rules ``/register`` used to enforce must still be enforced
now that it is gone.
"""

from datetime import datetime, timedelta

import pytest

from pixelvault import invites
from pixelvault.extensions import db
from pixelvault.models import AllowedEmail, User

from tests.conftest import Ref

#: The address the invite in these tests is issued for.
INVITED_EMAIL = "invitee@example.com"
#: The address an attacker would rather have. Never authorized by anyone.
ATTACKER_EMAIL = "attacker@evil.example"

GOOD_PASSWORD = "correct horse battery staple"
GOOD_USERNAME = "newcomer"

#: Rate limits from design §10. Asserted behaviourally — the point is that a
#: caller is actually refused, not that a decorator is present.
INVITE_LINK_BUDGET = 60
INVITE_SUBMIT_BUDGET = 20


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def invite(app):
    """A live, unaccepted invitation. ``.token`` is the plaintext, readable once."""
    with app.app_context():
        row, token = invites.issue(
            db.session, INVITED_EMAIL, prefill_username="suggested",
        )
        return Ref(id=row.id, email=row.email, token=token,
                   prefill_username=row.prefill_username)


@pytest.fixture
def invite_row(app):
    """Return a callable re-reading an invite row as a detached snapshot."""
    def _get(invite_id):
        with app.app_context():
            row = db.session.get(AllowedEmail, invite_id)
            if row is None:
                return None
            return Ref(id=row.id, email=row.email, state=row.state,
                       token_hash=row.token_hash, accepted_at=row.accepted_at,
                       accepted_user_id=row.accepted_user_id)
    return _get


@pytest.fixture
def accounts(app):
    """Return a callable listing every User as ``(username, email)`` pairs."""
    def _list():
        with app.app_context():
            return [(u.username, u.email) for u in db.session.query(User).all()]
    return _list


@pytest.fixture
def expire(app):
    """Backdate an invite past its TTL, which is indistinguishable from waiting."""
    def _expire(invite_id):
        with app.app_context():
            row = db.session.get(AllowedEmail, invite_id)
            row.token_issued_at = datetime.utcnow() - timedelta(days=30)
            row.expires_at = datetime.utcnow() - timedelta(hours=1)
            db.session.commit()
    return _expire


def open_link(client, token):
    """Click an invite link: the GET that moves the token into the session."""
    return client.get(f"/invite/{token}")


def submit(client, username=GOOD_USERNAME, password=GOOD_PASSWORD, confirm=None, **extra):
    """POST the acceptance form. ``extra`` lets a test add fields a browser never sends."""
    data = {
        "username": username,
        "password": password,
        "confirm_password": password if confirm is None else confirm,
    }
    data.update(extra)
    return client.post("/invite", data=data)


def logged_in_id(client):
    """The user id Flask-Login has on this client's session, or None."""
    with client.session_transaction() as sess:
        return sess.get("_user_id")


def stashed_token(client):
    """The invite token currently held in this client's session, or None."""
    with client.session_transaction() as sess:
        return sess.get("invite_token")


# ── The property the feature exists for ────────────────────────────────────

def test_a_posted_email_is_ignored_in_favour_of_the_invited_address(
        anon_client, invite, accounts, invite_row):
    """The single most important assertion in the suite (design §7.2).

    The form does not render a named email field, so a browser cannot send one —
    but a form is not a security boundary, and anyone can post whatever they like.
    What makes the address trustworthy is that the server reads it off the invite
    row inside ``invites.consume`` and never looks at ``request.form['email']``.

    If this test ever fails, the whitelist is over: the holder of an invite for any
    address can mint an account for any other, including one an admin refused.
    """
    open_link(anon_client, invite.token)

    response = submit(anon_client, email=ATTACKER_EMAIL)

    assert response.status_code == 302
    assert accounts() == [(GOOD_USERNAME, INVITED_EMAIL)]
    assert invite_row(invite.id).state.name == "ACCEPTED"


def test_a_posted_email_cannot_be_smuggled_past_a_direct_post(
        anon_client, invite, accounts):
    """The same attack without ever loading the form, in case a future refactor
    starts trusting fields it believes only its own template can produce."""
    open_link(anon_client, invite.token)

    submit(anon_client, email=ATTACKER_EMAIL, Email=ATTACKER_EMAIL.upper(),
           invite_email=ATTACKER_EMAIL, token="anything at all")

    assert [email for _, email in accounts()] == [INVITED_EMAIL]


def test_the_rendered_form_submits_no_email_field(anon_client, invite):
    """Defence in depth for the test above: the address is displayed, not posted.

    A named-but-readonly input would still be submitted, which puts an
    attacker-editable copy of the most important value in the flow one careless
    ``request.form.get('email')`` away from being believed.
    """
    open_link(anon_client, invite.token)
    body = anon_client.get("/invite").get_data(as_text=True)

    assert INVITED_EMAIL in body
    assert 'name="email"' not in body
    assert "readonly" in body


# ── The token leaves the URL ───────────────────────────────────────────────

def test_the_link_redirects_to_a_url_that_carries_no_token(anon_client, invite):
    """Design §11 Q5. ``Referrer-Policy`` stops the token leaking cross-origin, but
    a token in the path is still copied into nginx access logs, browser history and
    every "here is the page I'm on" message a confused invitee sends."""
    response = open_link(anon_client, invite.token)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/invite")
    assert invite.token not in response.headers["Location"]


def test_the_flow_completes_through_the_session_alone(anon_client, invite, accounts):
    """The redirect target has no token, so the form and the POST have to work from
    the session copy. If they did not, the clean URL would be cosmetic and the
    token would have to come back into the query string to make the flow work."""
    open_link(anon_client, invite.token)
    assert stashed_token(anon_client) == invite.token

    assert anon_client.get("/invite").status_code == 200
    assert submit(anon_client).status_code == 302
    assert accounts() == [(GOOD_USERNAME, INVITED_EMAIL)]


# ── Happy path ─────────────────────────────────────────────────────────────

def test_click_form_submit_creates_the_account_and_signs_the_user_in(
        anon_client, invite, invite_row, accounts):
    """The whole flow end to end, asserting each of the four things it must do."""
    assert open_link(anon_client, invite.token).status_code == 302

    form = anon_client.get("/invite").get_data(as_text=True)
    # The admin's suggestion is offered, and is editable — design §11 Q8.
    assert INVITED_EMAIL in form
    assert 'value="suggested"' in form

    response = submit(anon_client, username="chosen-name")

    assert response.headers["Location"].endswith("/dashboard")
    assert accounts() == [("chosen-name", INVITED_EMAIL)]

    row = invite_row(invite.id)
    assert row.state.name == "ACCEPTED"
    assert row.accepted_at is not None
    # Single use is enforced by nulling the hash, in the same transaction.
    assert row.token_hash is None

    with anon_client.session_transaction() as sess:
        assert sess.get("_user_id") is not None
        # The spent token is dropped, so a browser-history "back" cannot replay it.
        assert "invite_token" not in sess
    assert row.accepted_user_id == int(logged_in_id(anon_client))


def test_a_used_link_cannot_be_replayed(app, invite, accounts):
    """A second click on the same link, from a browser that never saw the first.

    The wording is the interesting half: ``consume`` nulls the hash, so the replay
    is genuinely indistinguishable from a typo and the page must not claim to know
    which it was.
    """
    first = app.test_client()
    open_link(first, invite.token)
    submit(first, username="first-arrival")

    second = app.test_client()
    response = open_link(second, invite.token)

    assert response.status_code == 404
    assert len(accounts()) == 1
    assert stashed_token(second) is None


# ── The four refusals ──────────────────────────────────────────────────────

def test_an_unknown_token_is_worded_for_typos_and_replays_at_once(anon_client):
    """``InvalidInvite`` serves two readers who cannot be told apart (design §13).

    One mistyped or truncated the link; the other already used it, because
    acceptance nulls the hash and leaves nothing to match. The copy has to give
    both of them their next step without asserting which one they are.
    """
    response = anon_client.get("/invite/not-a-real-token")
    body = response.get_data(as_text=True)

    assert response.status_code == 404
    assert "not valid" in body
    # The mistyped-link reader.
    assert "copied the whole link" in body
    # The already-registered reader.
    assert "sign in below" in body.lower()
    assert "stops working the moment it is accepted" in body


def test_an_expired_link_says_so_and_names_the_fix(anon_client, invite, expire):
    """410 Gone: it was real, it is not any more. The only fix is an admin resend,
    so the message says that rather than leaving them re-clicking."""
    expire(invite.id)

    response = anon_client.get(f"/invite/{invite.token}")
    body = response.get_data(as_text=True)

    assert response.status_code == 410
    assert "expired" in body
    assert "send you a new one" in body


def test_an_accepted_invite_points_at_the_login_page(app, anon_client, invite):
    """``AlreadyAccepted`` out of ``validate`` needs a row that is accepted *and*
    still holds a token, which the normal path never produces — ``consume`` nulls
    the hash. It is reachable from a hand-edited row, a restored backup, or a
    future path that stamps acceptance elsewhere, and the wording is what stops a
    person who already has an account from asking for another invitation.
    """
    with app.app_context():
        row = db.session.get(AllowedEmail, invite.id)
        row.accepted_at = datetime.utcnow()
        db.session.commit()

    response = anon_client.get(f"/invite/{invite.token}")
    body = response.get_data(as_text=True)

    assert response.status_code == 409
    assert "already been accepted" in body
    assert "the account exists" in body


def test_the_form_without_any_invitation_explains_there_is_no_signup_page(anon_client):
    """Someone typing /invite, or coming back after their session cookie expired.

    Distinct from the three link faults: nothing is wrong with any link, they just
    have not opened one, and the useful thing to say is that no public form exists.
    """
    response = anon_client.get("/invite")
    body = response.get_data(as_text=True)

    assert response.status_code == 403
    assert "Open the invitation link you were sent" in body
    assert "invite-only" in body


def test_posting_without_any_invitation_is_refused_the_same_way(anon_client, accounts):
    """The POST is the half that matters — a form is not a boundary, and the
    acceptance route must refuse a hand-crafted request with no session behind it."""
    response = submit(anon_client)

    assert response.status_code == 403
    assert accounts() == []


# ── A stash that has gone stale since the click ────────────────────────────

def test_a_token_rotated_since_the_click_is_refused(app, anon_client, invite, accounts):
    """An admin pressing Resend or Copy link between the GET and the POST.

    Rotation mints a new token and kills the old one (design §4), so the copy in
    this session is now worthless. The POST re-validates rather than trusting the
    GET's verdict, which is the only thing that catches this.
    """
    open_link(anon_client, invite.token)
    with app.app_context():
        invites.rotate(db.session, db.session.get(AllowedEmail, invite.id))

    response = submit(anon_client)

    assert response.status_code == 404
    assert accounts() == []
    # Dead tokens are dropped, so /invite does not keep refusing forever.
    assert stashed_token(anon_client) is None


def test_a_token_consumed_in_another_tab_is_refused(app, anon_client, invite, accounts):
    """Two tabs on one invitation, or a double-submit. The second one loses."""
    open_link(anon_client, invite.token)

    other_tab = app.test_client()
    open_link(other_tab, invite.token)
    submit(other_tab, username="first-arrival")

    response = submit(anon_client, username="second-arrival")

    assert response.status_code == 404
    assert accounts() == [("first-arrival", INVITED_EMAIL)]


def test_an_expired_stash_is_refused_at_submit_time(anon_client, invite, expire, accounts):
    """The TTL can lapse while the form sits open. Re-validation catches that too."""
    open_link(anon_client, invite.token)
    expire(invite.id)

    response = submit(anon_client)

    assert response.status_code == 410
    assert accounts() == []


# ── A live session must not absorb an invitation ───────────────────────────

def test_an_invitation_is_refused_while_someone_is_signed_in(
        client, user, invite, invite_row):
    """Design §7.2 allows logging the visitor out or refusing; this app refuses.

    Logging them out is a side effect a stranger can trigger in someone else's
    browser with a URL, and the server cannot tell whether the signed-in person is
    the invitee anyway. Refusing leaves both the session and the invitation intact,
    so whoever the link was actually sent to can still use it.
    """
    response = client.get(f"/invite/{invite.token}")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")
    # Their session survives, and the invitation is untouched and still live.
    assert logged_in_id(client) == str(user.id)
    assert stashed_token(client) is None
    assert invite_row(invite.id).state.name == "ISSUED"


def test_a_signed_in_visitor_cannot_accept_by_posting_directly(
        app, client, user, invite, invite_row, accounts):
    """The refusal has to hold on the POST as well, otherwise it is only advice."""
    anonymous = app.test_client()
    open_link(anonymous, invite.token)
    # Hand the signed-in browser the same session token the anonymous one holds.
    with client.session_transaction() as sess:
        sess["invite_token"] = invite.token

    response = submit(client, username="second-account")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")
    assert [name for name, _ in accounts()] == [user.username]
    assert invite_row(invite.id).accepted_at is None


# ── The rules /register used to enforce ────────────────────────────────────

@pytest.mark.parametrize("fields, expected", [
    ({"username": "ab"}, "at least 3 characters"),
    ({"username": "a" * 65}, "64 characters or fewer"),
    ({"username": "bad name!"}, "letters, numbers, hyphens, and underscores"),
    ({"password": "short"}, "at least 8 characters"),
    ({"password": "x" * 1025}, "Password is too long"),
    ({"confirm": "something else"}, "Passwords do not match"),
])
def test_username_and_password_rules_survive_the_move_from_register(
        anon_client, invite, accounts, fields, expected):
    """The same rules, with the same wording, as the deleted ``/register`` route.

    Quietly relaxing one during a move is how a password minimum disappears without
    anyone deciding to remove it. The 1024-character ceiling in particular is not a
    style rule: ``set_password`` runs 600k PBKDF2 rounds, so an unbounded password
    is a way to spend a worker thread on request.
    """
    open_link(anon_client, invite.token)

    response = submit(anon_client, **fields)

    assert response.status_code == 200
    assert expected in response.get_data(as_text=True)
    assert accounts() == []


def test_a_duplicate_username_is_refused_with_the_invite_still_live(
        anon_client, user, invite, invite_row):
    """Checked before ``consume`` so the reader gets "pick another name" rather than
    the generic collision backstop — and so the invitation is not spent on a
    submission that could not have worked."""
    open_link(anon_client, invite.token)

    response = submit(anon_client, username=user.username)

    assert response.status_code == 200
    assert "Username already taken." in response.get_data(as_text=True)
    assert invite_row(invite.id).accepted_at is None


def test_an_address_that_already_has_an_account_is_sent_to_login(
        app, anon_client, invite, invite_row):
    """An admin who issued an invite and then created the account by hand.

    Without this check ``consume`` fails on the unique index on ``user.email`` and
    reports a username collision, sending the reader off to invent names that can
    never work.
    """
    with app.app_context():
        existing = User(username="already-here", email=INVITED_EMAIL)
        existing.password_hash = "pbkdf2:sha256:600000$test$deadbeef"
        db.session.add(existing)
        db.session.commit()

    open_link(anon_client, invite.token)
    response = submit(anon_client)

    assert response.status_code == 200
    assert "An account already exists for this address" in response.get_data(as_text=True)
    assert invite_row(invite.id).accepted_at is None


# ── Public registration is gone ────────────────────────────────────────────

@pytest.mark.parametrize("method", ["get", "post"])
def test_register_is_404_not_a_redirect(anon_client, method):
    """Design §8. A redirect to /login would say the page moved; it did not, it was
    removed, and the only way in now is a link an admin issued."""
    response = getattr(anon_client, method)("/register")

    assert response.status_code == 404


def test_the_login_page_no_longer_offers_a_signup_link(anon_client):
    """And says why, so the missing link does not read as a broken page."""
    body = anon_client.get("/login").get_data(as_text=True)

    assert "Sign up" not in body
    assert "invite-only" in body


def test_the_access_required_page_names_someone_to_ask(app, anon_client, album):
    """An accountless share-link guest used to self-register from here (design §11
    Q6). That path is closed, so the page has to name a contact instead of ending
    at a sign-in button they cannot use."""
    body = anon_client.get(f"/album/{album.token}").get_data(as_text=True)

    assert "invite-only" in body
    # ADMIN_CONTACT falls back to MAIL_FROM, which conftest sets for the run.
    assert app.jinja_env.globals is not None
    assert "pixelvault@test.invalid" in body


# ── Rate limits (design §10) ───────────────────────────────────────────────

def test_clicking_links_is_throttled(anon_client):
    """60/hour per IP on the link endpoint.

    Guessing a 256-bit token is not the threat this bounds — the odds are
    indistinguishable from zero either way. What it bounds is an unauthenticated
    endpoint that does a database lookup per request being used as a cheap way to
    make the app work.
    """
    statuses = [anon_client.get(f"/invite/token-{n}").status_code
                for n in range(INVITE_LINK_BUDGET + 2)]

    assert statuses[0] == 404
    assert statuses.index(429) == INVITE_LINK_BUDGET


def test_submitting_the_form_is_throttled_harder(anon_client):
    """20/hour per IP, a third of the link budget.

    The POST is the expensive one — it hashes a password with 600k PBKDF2 rounds on
    the way to creating a row — and no honest invitee submits it more than a
    handful of times.
    """
    statuses = [submit(anon_client).status_code for _ in range(INVITE_SUBMIT_BUDGET + 2)]

    assert statuses[0] == 403
    assert statuses.index(429) == INVITE_SUBMIT_BUDGET
