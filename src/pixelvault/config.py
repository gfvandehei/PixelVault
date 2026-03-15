import re
import os
import pathlib

DEFAULT_ROOT = pathlib.Path(__file__).parents[2].absolute()

ALLOWED_PHOTO_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/heic'}
ALLOWED_VIDEO_TYPES = {'video/mp4', 'video/quicktime', 'video/x-msvideo', 'video/webm', 'video/mpeg'}
ALLOWED_MIME_TYPES = ALLOWED_PHOTO_TYPES | ALLOWED_VIDEO_TYPES

ALLOWED_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic',
    '.mp4', '.mov', '.avi', '.webm', '.mpg', '.mpeg'
}

RE_USERNAME = re.compile(r'^[a-zA-Z0-9_\-]+$')
RE_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
MAX_PASSWORD_LEN = 1024  # prevent slow-hash DoS via huge passwords

SECRET_KEY = os.environ.get('SECRET_KEY', os.urandom(32).hex())
SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///pixelvault.db')
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'uploads')
MAX_CONTENT_LENGTH = int(os.environ.get('MAX_UPLOAD_MB', 500)) * 1024 * 1024
SESSION_COOKIE_SECURE = os.environ.get('HTTPS', 'false').lower() == 'true'
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', '').strip()
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '').strip().lower()
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '').strip()
TEMPLATES_FOLDER = pathlib.Path(os.environ.get("FLASK_TEMPLATES_FOLDER", DEFAULT_ROOT/"templates"))