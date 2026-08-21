"""The account page: what it shows, and what changing a password actually does.

Most of this file is ordinary form-validation cover. Three tests are not, and they
are the reason the feature needed a design doc:

* ``test_other_sessions_are_signed_out`` — the property the issue asked for. A
  second browser holding a valid cookie for the same user must stop being signed in
  the moment the password changes, which is only possible because the cookie carries
  a rotatable token rather than a bare primary key.
* ``test_the_changing_browser_stays_signed_in`` — the other half. Rotation kills
  *every* cookie including the caller's, so the route re-issues it; without that,
  changing your password logs you out of the browser you did it in.
* ``test_a_failed_attempt_evicts_nobody`` — the failure direction. A wrong current
  password must not rotate anything, or a form anyone with a hijacked session can
  POST becomes a way to log the real owner out.

Passwords here are hashed with 1,000 PBKDF2 rounds instead of the app's 600,000
(``_password_user``). The route still spends the full 600k hashing whatever it is
given, so the *stored* cost is the only part a test can reasonably drop.
"""

import pytest
from werkzeug.security import generate_password_hash

from pixelvault.extensions import db
from pixelvault.mailer import MailError
from pixelvault.models import User

from tests.conftest import Ref, login

CURRENT = "correct horse battery staple"
NEW = "a-brand-new-password"


# ── Fixtures ───────────────────────────────────────────────────────────────

def _password_user(username, email, password=CURRENT):
    """A user whose password can actually be checked, hashed cheaply."""
    user = User(username=username, email=email)
    user.password_hash = generate_password_hash(password, method='pbkdf2:sha256:1000')
    db.session.add(user)
    db.session.commit()
    return Ref(id=user.id, username=user.username, email=user.email,
               session_token=user.session_token)


@pytest.fixture
def account_user(app):
    with app.app_context():
        return _password_user("alice", "alice@example.com")


@pytest.fixture
def account_client(app, account_user):
    client = app.test_client()
    login(client, account_user)
    return client


def row(app, user_ref):
    """Re-read the user row, so a test asserts on the database and not a cache."""
    with app.app_context():
        return db.session.get(User, user_ref.id)


def change(client, current=CURRENT, new=NEW, confirm=None):
    return client.post("/account/password", data={
        "current_password": current,
        "new_password": new,
        "confirm_password": new if confirm is None else confirm,
    })


# ── The page ───────────────────────────────────────────────────────────────

def test_the_page_shows_the_username_and_email(account_client, account_user):
    """What the issue actually asked for."""
    response = account_client.get("/account")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert account_user.username in body
    assert account_user.email in body


def test_the_page_needs_a_session(anon_client):
    response = anon_client.get("/account")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_an_anonymous_post_changes_nothing(anon_client):
    response = anon_client.post("/account/password", data={
        "current_password": CURRENT, "new_password": NEW, "confirm_password": NEW,
    })

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


# ── Changing the password ──────────────────────────────────────────────────

def test_the_password_changes(app, account_client, account_user, mailer):
    response = change(account_client)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/account")
    assert row(app, account_user).check_password(NEW)


def test_the_old_password_stops_working(app, account_client, account_user, mailer):
    change(account_client)

    assert not row(app, account_user).check_password(CURRENT)


def test_other_sessions_are_signed_out(app, account_client, account_user, mailer):
    """The property this feature exists for.

    ``phone`` holds a cookie that was valid a moment ago and is untouched by the
    request that follows — the only thing that changes underneath it is the token
    its identity is compared against.
    """
    phone = app.test_client()
    login(phone, account_user)
    assert phone.get("/dashboard").status_code == 200

    change(account_client)

    bounced = phone.get("/dashboard")
    assert bounced.status_code == 302
    assert "/login" in bounced.headers["Location"]


def test_the_changing_browser_stays_signed_in(app, account_client, mailer):
    """Rotation kills the caller's cookie too; the route re-issues it."""
    change(account_client)

    assert account_client.get("/dashboard").status_code == 200
    assert account_client.get("/account").status_code == 200


def test_the_session_token_is_rotated(app, account_client, account_user, mailer):
    """Stated directly, because every eviction above depends on this one write."""
    change(account_client)

    assert row(app, account_user).session_token != account_user.session_token


def test_other_users_are_untouched(app, account_client, account_user, mailer):
    with app.app_context():
        bystander = _password_user("bob", "bob@example.com")

    change(account_client)

    after = row(app, bystander)
    assert after.session_token == bystander.session_token
    assert after.check_password(CURRENT)


# ── Refusals ───────────────────────────────────────────────────────────────

def test_a_wrong_current_password_is_refused(app, account_client, account_user, mailer):
    response = change(account_client, current="not my password")

    assert response.status_code == 403
    assert row(app, account_user).check_password(CURRENT)
    assert mailer.outbox == []


def test_a_failed_attempt_evicts_nobody(app, account_client, account_user, mailer):
    """A refusal must not rotate the token.

    Otherwise the form is a logout button for anyone who can reach it — no
    knowledge of the current password required, which is the opposite of the
    guarantee the page makes.
    """
    phone = app.test_client()
    login(phone, account_user)

    change(account_client, current="not my password")

    assert row(app, account_user).session_token == account_user.session_token
    assert phone.get("/dashboard").status_code == 200


@pytest.mark.parametrize("new,confirm,expected", [
    ("short", None, "at least 8 characters"),
    ("x" * 1025, None, "too long"),
    ("a-brand-new-password", "a-different-password", "do not match"),
    (CURRENT, None, "different from your current one"),
])
def test_the_new_password_rules(app, account_client, account_user, mailer,
                                new, confirm, expected):
    """The four rules, each with the wording registration uses."""
    response = change(account_client, new=new, confirm=confirm)

    assert response.status_code == 400
    assert expected in response.get_data(as_text=True)
    assert row(app, account_user).check_password(CURRENT)
    assert row(app, account_user).session_token == account_user.session_token


# ── The notification ───────────────────────────────────────────────────────

def test_a_notice_is_sent_to_the_account_address(app, account_client, account_user, mailer):
    change(account_client)

    assert len(mailer.outbox) == 1
    message = mailer.outbox[0]
    assert message["To"] == account_user.email
    assert "password" in message["Subject"].lower()


def test_the_notice_carries_no_link_to_click(app, account_client, mailer):
    """There is no reset flow yet (#33), so the message must not teach anyone to
    click a link in a mail about their password."""
    change(account_client)

    text = mailer.outbox[0].get_body(preferencelist=('plain',)).get_content()
    assert "http" not in text


def test_a_relay_failure_does_not_undo_the_change(app, account_client, account_user):
    """The invariant that makes an inline send safe: the password is committed
    before anything is put on the wire, so a dead relay costs a courtesy email and
    nothing else."""
    class Refusing:
        def send(self, message):
            raise MailError("relay refused: 550 5.7.1 message rejected")

    previous = app.extensions.get("mailer")
    app.extensions["mailer"] = Refusing()
    try:
        response = change(account_client)
    finally:
        app.extensions["mailer"] = previous

    assert response.status_code == 302
    assert row(app, account_user).check_password(NEW)


# ── Rate limit ─────────────────────────────────────────────────────────────

def test_the_form_is_rate_limited(app, account_client, mailer):
    """Ten per hour, keyed on the user — a bound on both guessing and on PBKDF2 CPU.

    Spent on refusals, so the account is left with its original password and the
    limit is what the last response is about.
    """
    for _ in range(10):
        change(account_client, current="wrong")

    assert change(account_client, current="wrong").status_code == 429
