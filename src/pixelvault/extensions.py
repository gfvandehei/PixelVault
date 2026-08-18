import sqlite3

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy import event
from sqlalchemy.engine import Engine
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
