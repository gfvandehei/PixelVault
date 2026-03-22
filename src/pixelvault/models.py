import uuid
from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.orm import (
    DeclarativeBase, 
    MappedAsDataclass,
    Mapped,
    mapped_column,
    relationship
)
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, UniqueConstraint
)
from .config import ALLOWED_PHOTO_TYPES, ALLOWED_VIDEO_TYPES

class Base(DeclarativeBase):
    pass

class User(UserMixin, Base):
    __tablename__ = 'user'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    albums: Mapped[list["Album"]] = relationship('Album', backref='owner', lazy=True, cascade='all, delete-orphan')
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def set_password(self, password):
        """Hash and store the user's password using PBKDF2-SHA256 with 600,000 rounds."""
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256:600000')

    def check_password(self, password):
        """Return True if the given plaintext password matches the stored hash."""
        return check_password_hash(self.password_hash, password)

class AllowedEmail(Base):
    __tablename__ = 'allowed_email'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    note: Mapped[str] = mapped_column(String(256), default='')
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Album(Base):
    __tablename__ = "album"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey('user.id'), nullable=False)
    view_token: Mapped[str] = mapped_column(String(36), unique=True, nullable=True, default=lambda: str(uuid.uuid4()))
    photos: Mapped[list["Photo"]] = relationship('Photo', backref='album', lazy=True, cascade='all, delete-orphan')
    token: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    description: Mapped[str] = mapped_column(String(512), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    allow_anonymous: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_upload: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def photo_count(self):
        """Return the total number of files (photos and videos) in the album."""
        return len(self.photos)

    @property
    def cover_photo(self):
        """Return the first photo in the album for use as a cover thumbnail, or None if there are no photos."""
        photos = [p for p in self.photos if p.is_photo]
        return photos[0] if photos else None

class AlbumAccess(Base):
    """Tracks which users have been granted access to an album via a share link."""
    __tablename__ = 'album_access'
    __table_args__ = (UniqueConstraint('user_id', 'album_id'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('user.id'), nullable=False)
    album_id: Mapped[int] = mapped_column(Integer, ForeignKey('album.id'), nullable=False)
    accessed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    access_type: Mapped[str] = mapped_column(String(10), default='upload')
    user: Mapped["User"] = relationship('User', lazy=True)

class Photo(Base):
    __tablename__ = 'photo'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    album_id: Mapped[int] = mapped_column(Integer, ForeignKey('album.id'), nullable=False)
    uploader_id: Mapped[int] = mapped_column(Integer, ForeignKey('user.id'), nullable=True)
    stored_filename: Mapped[str] = mapped_column(String(256), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(256), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    uploader: Mapped[User] = relationship('User', backref='photos', lazy=True)
    uploader_name: Mapped[str] = mapped_column(String(64), default='Anonymous')
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    has_thumbnail: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def is_photo(self):
        """Return True if this file is an image (not a video)."""
        return self.mime_type in ALLOWED_PHOTO_TYPES

    @property
    def is_video(self):
        """Return True if this file is a video."""
        return self.mime_type in ALLOWED_VIDEO_TYPES

    @property
    def thumbnail_filename(self):
        """Return the expected filename for this photo's thumbnail on disk."""
        return f"thumb_{self.stored_filename}"

    @property
    def file_size_human(self):
        """Return the file size as a human-readable string (e.g. '3.2 MB')."""
        size = self.file_size
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"