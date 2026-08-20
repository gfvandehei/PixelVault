"""What the browser is handed, and what the app refuses to boot without.

Three faults, one theme: the credentials that stand in for a password after login
had weaker protection than the password itself.

* **The remember cookie** (#36). Flask-Login's ``remember_token`` restores a full
  session on its own, with no session cookie present — so it is a password
  equivalent with a 30-day life. It shipped with neither ``Secure`` nor
  ``SameSite``, which put it on the wire in cleartext over any ``http://``
  request and let it ride a cross-site POST. A client holding only that cookie
  deleted an album. The tests below read the *actual* ``Set-Cookie`` headers
  rather than ``app.config``, because a flag set in config and not reaching the
  browser is the failure being guarded against.
* **The login timing gap** (#43). An unknown username returned before hashing;
  a known one spent 600k PBKDF2 rounds. One request per candidate answered "does
  this account exist" — a question the identical flash message is careful not to.
  Asserted by observing that the hash actually happens, not by trusting the
  clock, with a loose timing check alongside as corroboration.
* **The session secret** (#44). Unset, empty and placeholder keys each produced a
  silent failure: random logouts, blanket 500s, or a fully forgeable admin
  session. Now they stop the boot.

The SECRET_KEY section calls ``create_app()`` a second time, which the
session-scoped ``app`` fixture in ``conftest.py`` otherwise forbids — building
the app twice stacks duplicate rate limits on the shared ``limiter`` singleton.
It is safe here *only* because every value those tests pass is fatal, and
``_validate_secret_key()`` runs before the ``Flask`` object is even constructed.
Never add a case there with a valid key.
"""

import time
from datetime import timedelta

import pytest

from pixelvault import create_app
from pixelvault import config as config_module
from pixelvault.config import SECRET_KEY_PLACEHOLDER, check_secret_key
from pixelvault.extensions import db
from pixelvault.models import User

from tests.conftest import Ref

#: Real credentials, because these tests go through the login form rather than
#: forging a session — the remember cookie only exists as a product of
#: ``login_user()``, and the timing property is a property of the route.
LOGIN_USERNAME = "cookiemonster"
LOGIN_PASSWORD = "correct horse battery staple"


# ── Fixtures and helpers ───────────────────────────────────────────────────

@pytest.fixture
def real_user(app):
    """A user whose password hash is genuine, unlike ``conftest._make_user``'s.

    Costs one 600k-round hash. Unavoidable: a login that must be *verified*
    cannot run against the placeholder hash the rest of the suite uses.
    """
    with app.app_context():
        user = User(username=LOGIN_USERNAME, email="cookie@example.com", is_admin=False)
        user.set_password(LOGIN_PASSWORD)
        db.session.add(user)
        db.session.commit()
        return Ref(id=user.id, username=user.username, email=user.email)


def do_login(client, username=LOGIN_USERNAME, password=LOGIN_PASSWORD, remember=True):
    """POST the login form exactly as the browser does, checkbox and all."""
    data = {"username": username, "password": password}
    if remember:
        # An HTML checkbox submits "on"; the route only tests truthiness.
        data["remember"] = "on"
    return client.post("/login", data=data)


def cookie_attributes(response, name):
    """Return the attribute names set on cookie ``name``, or None if unset.

    Parses the raw ``Set-Cookie`` headers rather than consulting the client's
    cookie jar: the jar keeps values, and the whole question here is about the
    *attributes* — which the jar normalises away, and which are the only thing
    standing between a bearer token and a cross-site POST.
    """
    for header in response.headers.getlist("Set-Cookie"):
        first, _, rest = header.partition(";")
        if first.split("=", 1)[0].strip() != name:
            continue
        return [part.strip() for part in rest.split(";") if part.strip()]
    return None


def flag_names(attributes):
    """Normalise ``['HttpOnly', 'Path=/', 'SameSite=Lax']`` to a comparable set."""
    return {attr.split("=", 1)[0].strip() for attr in attributes}


# ── The remember cookie's flags (#36) ──────────────────────────────────────

def test_remember_cookie_is_httponly_and_samesite_lax(client_for_cookies, real_user):
    """The regression test for the reported bug, stated as observed headers.

    Before the fix this cookie carried exactly ``Expires``, ``HttpOnly`` and
    ``Path`` — no ``SameSite`` at all. Browsers that do not default cookies to
    Lax (Firefox, Safari) therefore attached it to cross-site POSTs, and because
    it authenticates on its own the session cookie's ``SameSite=Lax`` protected
    nothing.
    """
    response = do_login(client_for_cookies)
    assert response.status_code == 302

    attributes = cookie_attributes(response, "remember_token")
    assert attributes is not None, "logging in with 'remember' set no remember_token"

    assert "HttpOnly" in flag_names(attributes)
    assert "SameSite=Lax" in attributes


def test_remember_cookie_gains_secure_exactly_when_the_session_cookie_does(
        app, client_for_cookies, real_user, monkeypatch):
    """``HTTPS=true`` must reach both cookies, not just the session one.

    Flipped through ``app.config`` because the suite runs with ``HTTPS=false``
    and the value is read out of config when the response is built, so this is
    the same code path a TLS deployment takes. Both cookies are asserted in one
    test on purpose: the property is that they agree, and checking them apart
    would pass while one of them lagged.
    """
    monkeypatch.setitem(app.config, "SESSION_COOKIE_SECURE", True)
    monkeypatch.setitem(app.config, "REMEMBER_COOKIE_SECURE", True)

    response = do_login(client_for_cookies)

    assert "Secure" in flag_names(cookie_attributes(response, "remember_token"))
    assert "Secure" in flag_names(cookie_attributes(response, "session"))


def test_remember_cookie_secure_tracks_the_https_switch(app):
    """One env var governs both cookies.

    Hard-coding ``REMEMBER_COOKIE_SECURE = True`` would look safer and be worse:
    on a plain-HTTP deployment the browser would drop the cookie, and "remember
    me" would silently stop working with nothing to explain it. Tying it to
    ``SESSION_COOKIE_SECURE`` means the failure modes of the two cookies are the
    same failure mode, which is the one an operator already knows about.
    """
    assert app.config["REMEMBER_COOKIE_SECURE"] == app.config["SESSION_COOKIE_SECURE"]


def test_remember_cookie_lives_thirty_days_not_a_year(app, client_for_cookies, real_user):
    """Flask-Login's default is 365 days; a bearer token should not outlive a laptop.

    Checked both as configuration and as the ``Expires`` the browser is actually
    told, with a day of slack for the clock — the point is the order of
    magnitude, not the second.
    """
    assert app.config["REMEMBER_COOKIE_DURATION"] == timedelta(days=30)

    attributes = cookie_attributes(do_login(client_for_cookies), "remember_token")
    expires = next(a for a in attributes if a.startswith("Expires="))

    from email.utils import parsedate_to_datetime
    lifetime = parsedate_to_datetime(expires.split("=", 1)[1]) - _now_utc()
    assert timedelta(days=29) < lifetime < timedelta(days=31), expires


def test_no_remember_cookie_when_the_box_is_not_ticked(client_for_cookies, real_user):
    """The long-lived credential is opt-in, so the flags matter only when asked for."""
    response = do_login(client_for_cookies, remember=False)

    assert response.status_code == 302
    assert cookie_attributes(response, "remember_token") is None


def test_accepting_an_invitation_does_not_mint_a_remember_token(anon_client, app):
    """A form with no checkbox must not decide for the person filling it in.

    Invite acceptance used to pass ``remember=True`` unconditionally, so every
    account ever created started life holding a standalone authenticator nobody
    asked for. They are still signed in — the session cookie is set — but the
    30-day token is now a choice made on the login page.
    """
    from pixelvault import invites

    with app.app_context():
        _, token = invites.issue(db.session, "fresh@example.com")

    anon_client.get(f"/invite/{token}")
    response = anon_client.post("/invite", data={
        "username": "freshface",
        "password": LOGIN_PASSWORD,
        "confirm_password": LOGIN_PASSWORD,
    })

    assert response.status_code == 302
    assert cookie_attributes(response, "remember_token") is None
    with anon_client.session_transaction() as sess:
        assert sess.get("_user_id") is not None, "the new account was not signed in"


def test_the_session_cookie_kept_its_flags(client_for_cookies, real_user):
    """A guard on the cookie that was already configured correctly.

    Nothing in the remember-cookie fix touches these, which is exactly why it is
    worth pinning them: the two blocks sit next to each other in ``create_app``
    and a later edit to one is an easy edit to both.
    """
    attributes = cookie_attributes(do_login(client_for_cookies), "session")

    assert "HttpOnly" in flag_names(attributes)
    assert "SameSite=Lax" in attributes


# ── Login timing (#43) ─────────────────────────────────────────────────────

def test_an_unknown_username_still_pays_for_a_hash(client_for_cookies, real_user, monkeypatch):
    """The equaliser is observed doing its work, not inferred from a stopwatch.

    ``check_password_hash`` is looked up in the route's module globals at call
    time, so replacing it here is enough to count the calls. Asserting on the
    call — and on *which* hash it was handed — states the property directly:
    the unknown-username branch verifies the dummy hash rather than returning
    early.
    """
    from pixelvault.routes import auth as auth_module

    calls = []
    real = auth_module.check_password_hash

    def counting(stored_hash, password):
        calls.append(stored_hash)
        return real(stored_hash, password)

    monkeypatch.setattr(auth_module, "check_password_hash", counting)

    response = do_login(client_for_cookies, username="no-such-person", remember=False)

    assert response.status_code == 200  # the form, re-rendered with the flash
    assert calls == [auth_module._DUMMY_PASSWORD_HASH]


def test_the_dummy_hash_uses_the_model_s_own_parameters():
    """It has to cost what a real verification costs, or it equalises nothing.

    Derived through ``User.set_password`` rather than by naming the algorithm in
    ``auth.py``, so raising the model's round count raises this one too. The
    assertion is that the two agree, not that either is a particular number.
    """
    from pixelvault.routes.auth import _DUMMY_PASSWORD_HASH

    reference = User()
    reference.set_password("anything")

    method = _DUMMY_PASSWORD_HASH.rsplit("$", 2)[0]
    assert method == reference.password_hash.rsplit("$", 2)[0]
    assert method.startswith("pbkdf2:sha256:")


def test_unknown_and_wrong_password_take_comparable_time(client_for_cookies, real_user):
    """The measurement from the issue, re-run: the gap was 36x (2.8 ms vs 102.5 ms).

    The bound is deliberately loose. Both branches now run one 600k-round PBKDF2
    that dominates everything else in the request, so a real regression collapses
    one side to a couple of milliseconds and lands nowhere near the threshold —
    while ordinary scheduling noise on a busy CI box stays well inside it. A tight
    bound here would buy nothing and flake often.
    """
    def elapsed(username):
        started = time.perf_counter()
        do_login(client_for_cookies, username=username, password="wrong password",
                 remember=False)
        return time.perf_counter() - started

    known = min(elapsed(LOGIN_USERNAME) for _ in range(3))
    unknown = min(elapsed("no-such-person") for _ in range(3))

    assert unknown > known * 0.4, (
        f"unknown username answered in {unknown * 1000:.1f} ms against "
        f"{known * 1000:.1f} ms for a known one — the existence of an account is "
        f"readable from the clock again"
    )


# ── The session secret (#44) ───────────────────────────────────────────────

@pytest.mark.parametrize("configured", ["", "   "])
def test_an_unset_or_empty_secret_key_is_an_error_outside_debug(configured):
    """Neither has a working degraded mode.

    Unset means the per-process fallback, and two Gunicorn workers signing with
    two different keys log users out on roughly half their requests. Empty is
    what ``docker compose`` substitutes for a ``${SECRET_KEY}`` missing from
    ``.env.prod`` — and because the variable *is* then set, the fallback never
    fires and Flask raises on every request that writes a session.
    """
    assert [s for s, _ in check_secret_key(configured, debug=False)] == ["error"]


@pytest.mark.parametrize("configured", ["", "   "])
def test_debug_downgrades_a_missing_key_to_a_warning(configured):
    """A dev checkout must still boot.

    One process means one key, and the only cost is that restarting the server
    logs you out of your own laptop. The warning still says so, because the same
    checkout becomes a deployment the day someone points Gunicorn at it.
    """
    severities = [s for s, _ in check_secret_key(configured, debug=True)]
    assert severities == ["warning"]


def test_the_env_example_placeholder_is_fatal_even_in_debug():
    """The one case where a known key is worse than no key.

    An absent key is random per process: confusing, but unforgeable. The
    placeholder is a real working key that is also published in this repository,
    so anyone who has read it can mint a cookie with ``_user_id`` set to the
    admin's and skip authentication entirely. There is nothing to degrade to, and
    debug does not excuse it — the fix is one command, printed in the message.
    """
    for debug in (True, False):
        problems = check_secret_key(SECRET_KEY_PLACEHOLDER, debug=debug)
        assert [s for s, _ in problems] == ["error"]
        assert SECRET_KEY_PLACEHOLDER in problems[0][1]


def test_a_short_key_is_reported_but_not_fatal():
    """Weak, not broken — and a running deployment should be told, not stopped.

    This is the boundary between this check and ``_validate_mail_config``'s
    absolutism: a key that is merely short still signs and verifies cookies
    consistently across workers, so refusing to boot would take down a working
    site to make a point.
    """
    problems = check_secret_key("short", debug=False)

    assert [s for s, _ in problems] == ["warning"]


def test_a_generated_key_passes_cleanly():
    """The shape the docs tell an operator to produce must be silent."""
    import secrets

    assert check_secret_key(secrets.token_hex(32), debug=False) == []


@pytest.mark.parametrize("configured", ["", SECRET_KEY_PLACEHOLDER])
def test_create_app_refuses_to_boot_on_a_fatal_secret_key(configured, monkeypatch):
    """The check is wired into the boot path, not merely importable.

    ``_validate_secret_key()`` runs before ``Flask()`` is constructed, so this
    raises without registering a route, touching the database or stacking a
    second set of limits on the shared limiter — which is the only reason a test
    is allowed to call ``create_app()`` twice in one process.
    """
    monkeypatch.setattr(config_module, "_SECRET_KEY_ENV", configured)
    monkeypatch.setattr(config_module, "FLASK_DEBUG", False)

    with pytest.raises(RuntimeError) as excinfo:
        create_app()

    assert "SECRET_KEY" in str(excinfo.value)
    # The message has to be actionable from `docker logs` alone.
    assert "secrets.token_hex(32)" in str(excinfo.value)


# ── Local helpers ──────────────────────────────────────────────────────────

def _now_utc():
    """``datetime.now`` in UTC, tz-aware — ``Expires`` parses to an aware value."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


@pytest.fixture
def client_for_cookies(app):
    """A test client with no session, used to drive the real login form.

    Deliberately not ``conftest``'s ``client``: that one forges a session
    directly, which is right for every other module and useless here, because a
    remember cookie only exists as a product of ``login_user()``.
    """
    return app.test_client()
