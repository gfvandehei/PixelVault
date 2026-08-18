"""Shared fixtures for the PixelVault test suite.

READ THIS BEFORE ADDING A TEST — how configuration is bootstrapped
-----------------------------------------------------------------
``src/pixelvault/config.py`` reads ``os.environ`` at **module import time** and
binds the results to module-level constants. Three things then capture those
constants in ways a test cannot reach afterwards:

* ``create_app()`` copies them into ``app.config`` once, at startup;
* ``routes/share.py`` imports ``UPLOAD_CHUNK_SIZE`` into its own namespace;
* ``uploads.check_user_quota`` uses the quota constants as **default argument
  values**, which Python binds when the ``def`` statement executes.

So ``monkeypatch.setenv`` inside a test is always too late, and reassigning
``pixelvault.config.X`` is too late for the last two. The only reliable moment
is *before the package is first imported*.

That is what the block at the top of this file does: it builds a throwaway
directory with ``mkdtemp`` and stamps the environment, and only then imports
``pixelvault``. pytest imports ``conftest.py`` before it imports any test
module, so by the time a test runs the package is already configured.

Practical consequences:

* Anything read at import time (chunk size, TTL, per-user quotas) is **fixed
  for the whole run**. Choose the value in ``_TEST_ENV`` below, not in a test.
  The values are deliberately tiny so a "many chunks" test is still fast.
* Anything read through ``current_app.config`` at request time
  (``MAX_CONTENT_LENGTH``, ``UPLOAD_FOLDER``) *can* still be monkeypatched per
  test, because the route re-reads it on every request.
* Never import ``pixelvault`` at the top of a test module before this file has
  run. In a normal pytest invocation that is guaranteed; just don't invent your
  own ``sys.path`` bootstrapping in a test module.
"""

import io
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# ── Environment bootstrap — MUST precede any `pixelvault` import ────────────

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    # Belt and braces: the package is normally installed editable, but the suite
    # should not depend on that having been done.
    sys.path.insert(0, str(_SRC))

TEST_TMP = Path(tempfile.mkdtemp(prefix="pixelvault-tests-"))

#: 64 KiB rather than the production 8 MiB, so a ~500 KB JPEG genuinely spans a
#: dozen chunks and the multi-chunk tests still run in milliseconds.
TEST_CHUNK_SIZE = 64 * 1024
#: MAX_CONTENT_LENGTH for the run. Small enough that the `init` size ceiling can
#: be tripped with a declared size rather than real bytes.
TEST_MAX_UPLOAD_MB = 8
#: Per-user in-flight byte quota. Deliberately below TEST_MAX_UPLOAD_MB so the
#: two limits can be told apart: a size between them trips the quota (429), a
#: size above both trips the ceiling (413).
TEST_MAX_INFLIGHT_MB = 4
#: Per-user open-session quota. Three, so the test needs four inits, not eleven.
TEST_MAX_SESSIONS = 3
TEST_TTL_HOURS = 24

_TEST_ENV = {
    "SECRET_KEY": "test-secret-key-not-used-in-production",
    "DATABASE_URL": f"sqlite:///{TEST_TMP / 'test.db'}",
    "UPLOAD_FOLDER": str(TEST_TMP / "uploads"),
    "MAX_UPLOAD_MB": str(TEST_MAX_UPLOAD_MB),
    "UPLOAD_CHUNK_SIZE": str(TEST_CHUNK_SIZE),
    "UPLOAD_SESSION_TTL_HOURS": str(TEST_TTL_HOURS),
    "MAX_CONCURRENT_UPLOADS_PER_USER": str(TEST_MAX_SESSIONS),
    "MAX_INFLIGHT_UPLOAD_MB_PER_USER": str(TEST_MAX_INFLIGHT_MB),
    "HTTPS": "false",
    "FLASK_DEBUG": "false",
    # Blank, or create_app() seeds an admin on startup and the row counts in
    # every authorisation test shift under us.
    "ADMIN_USERNAME": "",
    "ADMIN_EMAIL": "",
    "ADMIN_PASSWORD": "",
}
os.environ.update(_TEST_ENV)

# Only now is it safe to pull the package in.
from pixelvault import create_app  # noqa: E402
from pixelvault import uploads as uploads_module  # noqa: E402
from pixelvault.extensions import db, limiter  # noqa: E402
from pixelvault.models import Album, Base, Photo, UploadSession, User  # noqa: E402

from .protocol import ProtocolClient  # noqa: E402


# ── Application ────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def app():
    """One Flask app for the whole run.

    Session-scoped on purpose: ``create_app()`` re-registers every route on the
    shared module-level ``limiter`` singleton, so building it per test would
    stack duplicate limits on the same endpoints. Isolation comes from
    ``reset_state`` below instead.
    """
    application = create_app()
    application.config.update(TESTING=True)
    yield application
    shutil.rmtree(TEST_TMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_state(app):
    """Return the app to a blank slate between tests, in any order.

    Three kinds of state survive a test otherwise: database rows, files under
    UPLOAD_FOLDER, and the rate limiter's in-memory counters. The last one is
    the easy one to forget — without the reset, ``test_limits`` would pass or
    fail depending on how many chunks the tests before it happened to send.
    """
    with app.app_context():
        for table in reversed(Base.metadata.sorted_tables):
            db.session.execute(table.delete())
        db.session.commit()

        upload_dir = Path(app.config["UPLOAD_FOLDER"])
        shutil.rmtree(upload_dir, ignore_errors=True)
        upload_dir.mkdir(parents=True, exist_ok=True)

        limiter.reset()
        # The opportunistic sweep keeps its clock in a module global. Pin it to
        # "just swept" so no unrelated `init` reclaims a session a test is
        # deliberately holding; the cleanup tests set it back to 0 themselves.
        uploads_module._last_sweep_at = float("inf")
    yield


@pytest.fixture
def upload_dir(app):
    """The directory committed media lands in, as a Path."""
    return Path(app.config["UPLOAD_FOLDER"])


@pytest.fixture
def partials_dir(upload_dir):
    """The directory in-flight ``.part`` files live in."""
    from pixelvault.config import UPLOAD_PARTIALS_SUBDIR
    return upload_dir / UPLOAD_PARTIALS_SUBDIR


# ── Users and albums ───────────────────────────────────────────────────────

class Ref:
    """A detached snapshot of a DB row.

    Flask-SQLAlchemy scopes its session to the app context, so an ORM instance
    created in a fixture is detached by the time a test touches it. Fixtures
    therefore hand back plain scalars.
    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __repr__(self):
        return f"Ref({self.__dict__})"


def _make_user(username, email, password="correct horse battery staple"):
    user = User(username=username, email=email, is_admin=False)
    # set_password runs 600k PBKDF2 rounds; assigning the hash directly keeps
    # the suite fast. No test authenticates by password — see `login`.
    user.password_hash = "pbkdf2:sha256:600000$test$deadbeef"
    db.session.add(user)
    db.session.commit()
    return Ref(id=user.id, username=user.username, email=user.email)


@pytest.fixture
def user(app):
    """The uploading user."""
    with app.app_context():
        return _make_user("alice", "alice@example.com")


@pytest.fixture
def other_user(app):
    """A second, unrelated user — used to prove sessions are not cross-reachable."""
    with app.app_context():
        return _make_user("mallory", "mallory@example.com")


@pytest.fixture
def album(app, user):
    """An album owned by ``user``, with uploads enabled."""
    with app.app_context():
        row = Album(name="Trip", owner_id=user.id, description="")
        db.session.add(row)
        db.session.commit()
        return Ref(id=row.id, token=row.token, view_token=row.view_token, name=row.name)


# ── Clients ────────────────────────────────────────────────────────────────

def login(client, user_ref):
    """Put a Flask-Login session cookie on the client without hitting /login.

    Going through the real login form would spend 600k PBKDF2 rounds per test
    and burn the login endpoint's rate limit; the session keys below are exactly
    what ``login_user()`` writes.
    """
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_ref.id)
        sess["_fresh"] = True


@pytest.fixture
def anon_client(app):
    """A test client with no session — nothing is logged in."""
    return app.test_client()


@pytest.fixture
def client(app, user):
    """A test client logged in as ``user``."""
    test_client = app.test_client()
    login(test_client, user)
    return test_client


@pytest.fixture
def other_client(app, other_user):
    """A test client logged in as ``other_user``."""
    test_client = app.test_client()
    login(test_client, other_user)
    return test_client


@pytest.fixture
def protocol(client, album):
    """Drives the chunked upload protocol as ``static/js/uploader.js`` does."""
    return ProtocolClient(client, album.token)


# ── Media fixtures ─────────────────────────────────────────────────────────

def make_jpeg(width=48, height=48, seed=1, quality=95):
    """Return the bytes of a real JPEG full of deterministic noise.

    A real image, not random bytes: the commit path runs libmagic over it,
    re-opens it in Pillow, applies EXIF transposition and writes a 400x400
    thumbnail. Noise rather than flat colour because JPEG compresses flat colour
    almost to nothing, and several tests need a file that spans many chunks.
    """
    from PIL import Image

    rnd = random.Random(seed)
    raw = rnd.randbytes(width * height * 3)
    image = Image.frombytes("RGB", (width, height), raw)
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


@pytest.fixture(scope="session")
def small_jpeg():
    """A few-KB JPEG — one chunk, or the legacy single-request path."""
    return make_jpeg(48, 48, seed=1)


@pytest.fixture(scope="session")
def multi_chunk_jpeg():
    """A JPEG large enough to span a dozen 64 KiB chunks."""
    data = make_jpeg(1024, 1024, seed=7)
    assert len(data) > 8 * TEST_CHUNK_SIZE, (
        f"fixture must span many chunks; got {len(data)} bytes "
        f"over a {TEST_CHUNK_SIZE}-byte chunk size"
    )
    return data


@pytest.fixture(scope="session")
def not_an_image():
    """Bytes libmagic identifies as a disallowed type, under an allowed extension."""
    return b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 4088


# ── Assertion helpers shared across modules ────────────────────────────────

@pytest.fixture
def session_row(app):
    """Return a callable fetching the live UploadSession row for an upload_id."""
    def _get(upload_id):
        with app.app_context():
            row = db.session.query(UploadSession).filter_by(upload_id=upload_id).one_or_none()
            if row is None:
                return None
            return Ref(
                id=row.id,
                upload_id=row.upload_id,
                received_bytes=row.received_bytes,
                total_size=row.total_size,
                original_filename=row.original_filename,
                user_id=row.user_id,
                album_id=row.album_id,
                client_key=row.client_key,
            )
    return _get


@pytest.fixture
def photos(app):
    """Return a callable listing the Photo rows in an album."""
    def _list(album_id):
        with app.app_context():
            rows = db.session.query(Photo).filter_by(album_id=album_id).all()
            return [
                Ref(
                    id=p.id,
                    stored_filename=p.stored_filename,
                    original_filename=p.original_filename,
                    mime_type=p.mime_type,
                    file_size=p.file_size,
                    has_thumbnail=p.has_thumbnail,
                    uploader_id=p.uploader_id,
                    uploader_name=p.uploader_name,
                )
                for p in rows
            ]
    return _list
