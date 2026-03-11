import re

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
