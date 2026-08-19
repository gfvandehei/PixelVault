import sqlite3

from flask import current_app, flash, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user
from flask_login.utils import login_url
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.utils import redirect
from pixelvault.models import Base, User


db = SQLAlchemy(model_class=Base)


@event.listens_for(Engine, "connect")
def _apply_sqlite_pragmas(dbapi_connection, connection_record):
    """Put SQLite in WAL mode on every new connection, with room to wait out a busy writer.

    Chunked uploads changed this database's access pattern. Serving the app used to
    be read-dominated, but every accepted chunk now commits an UPDATE to
    ``upload_session`` — for a 470 MB file that is ~59 writes, and three files upload
    concurrently by default. At the same time the production server moved from 2 sync
    workers to 2 x 4 threads, so eight handlers can reach the database at once where
    two could before.

    In the default ``delete`` journal mode a writer locks out every reader, so those
    two changes together would surface as ``database is locked`` on thumbnail
    requests whenever someone uploads a video. WAL lets readers and one writer
    proceed together, which is exactly the shape of this workload.

    ``synchronous=NORMAL`` is the standard companion to WAL: it keeps the fsync per
    commit off the hot path while still being crash-safe, because the durability that
    actually matters for a partial upload is the fsync of the ``.part`` file in
    uploads.py, not of the row counting it.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return  # the pragmas below are SQLite-only; other backends configure themselves
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # Wait rather than failing instantly when another handler holds the write lock.
        cursor.execute("PRAGMA busy_timeout=10000")
    finally:
        cursor.close()

login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def _wants_json():
    """Return True if this request is a program's, not a browser page load's.

    Four signals, any one of which is decisive:

    * ``X-Requested-With: XMLHttpRequest`` — set on every request uploader.js makes;
    * a JSON request body, which no navigation has;
    * an ``Accept`` that prefers JSON over HTML;
    * an endpoint that only ever speaks JSON.

    The endpoint check is not redundant. A chunk body is ``application/octet-stream``
    and carries no ``Accept``, so on a non-XHR client — curl, a retry from a service
    worker — the first three all miss, and answering that with a login page would be
    the same bug in a different disguise.
    """
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    if request.is_json:
        return True
    if request.accept_mimetypes.best_match(('text/html', 'application/json'),
                                           default=None) == 'application/json':
        return True
    endpoint = request.endpoint or ''
    return endpoint.startswith(('upload_', 'api_', 'do_upload')) or request.path.startswith('/api/')


@login_manager.unauthorized_handler
def handle_unauthorized():
    """Answer an unauthenticated request in the dialect it asked in.

    Flask-Login's default is a 302 to ``login_view``, which is right for a browser
    following a link and wrong for everything else: XHR follows a redirect
    transparently, so an upload whose session expired mid-transfer saw HTTP 200 with an
    HTML login page in the body, found no ``results`` key in it, and reported the
    generic "Upload failed". The 401 branch uploader.js already has — "Session expired
    — reload the page", the one instruction that actually resolves it — was unreachable.

    The HTML path below reproduces the default exactly, flash message and ``next``
    parameter included, so ordinary logged-out browsing is unchanged. It has to be
    reproduced rather than delegated to: ``login_manager.unauthorized()`` dispatches
    back into this handler.
    """
    if _wants_json():
        return jsonify({'error': 'Session expired — reload the page.'}), 401
    if login_manager.login_message:
        flash(login_manager.login_message, login_manager.login_message_category)
    return redirect(login_url(login_manager.login_view, request.url))


def rate_limit_key():
    """Return the bucket a request is charged against: the user if known, else the client IP.

    Keying on identity rather than address is what makes a per-user quota mean
    anything. Every upload, album and admin route is already `@login_required`, and
    a user id cannot be spoofed by a header the way a forwarded address can — so for
    the routes that carry real cost this sidesteps the reverse-proxy question
    entirely (see TRUSTED_PROXY_COUNT in config.py and issue #30).

    The IP fallback is still correct where it is used: /register and /login run
    before the caller has any identity to key on.
    """
    if current_user.is_authenticated:
        return f"user:{current_user.id}"
    return get_remote_address()


limiter = Limiter(
    rate_limit_key,
    default_limits=["200 per hour"],
    storage_uri="memory://",
)


class MailerProxy:
    """App-bound handle on the mail transport, in the shape of db / login_manager / limiter.

    The concrete backend is chosen once per app by :func:`~pixelvault.mailer.build_mailer`
    and parked in ``app.extensions['mailer']``; this proxy resolves it per call
    through ``current_app``. That indirection is what lets a test write
    ``app.extensions['mailer'] = MemoryMailer()`` and have every caller pick it up
    — including callers that imported ``mailer`` at module load, which is all of
    them, and which is why swapping a module global would not work under the
    session-scoped ``app`` fixture.
    """

    def init_app(self, app):
        """Build the configured backend and register it on ``app``."""
        from pixelvault.mailer import build_mailer
        app.extensions['mailer'] = build_mailer()

    @property
    def backend(self):
        """The current app's mail backend."""
        return current_app.extensions['mailer']

    def send(self, message):
        """Deliver ``message`` through the current app's backend; raises ``MailError``."""
        return self.backend.send(message)


mailer = MailerProxy()
