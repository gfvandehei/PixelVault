from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pixelvault.models import Base, User


db = SQLAlchemy(model_class=Base)

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
