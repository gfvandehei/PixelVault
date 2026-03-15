from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
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

limiter = Limiter(
    get_remote_address,
    default_limits=["200 per hour"],
    storage_uri="memory://",
)

