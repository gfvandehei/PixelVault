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
    Column, Integer, String, Boolean, DateTime, ForeignKey
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
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256:600000')

    def check_password(self, password):
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
    view_token: Mapped[str] = mapped_column(String(36), unique=True, nullable=True)
    photos: Mapped[list["Photo"]] = relationship('Photo', backref='album', lazy=True, cascade='all, delete-orphan')
    token: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    description: Mapped[str] = mapped_column(String(512), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    allow_anonymous: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_upload: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def photo_count(self):
        return len(self.photos)

    @property
    def cover_photo(self):
        photos = [p for p in self.photos if p.is_photo]
        return photos[0] if photos else None

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
        return self.mime_type in ALLOWED_PHOTO_TYPES

    @property
    def is_video(self):
        return self.mime_type in ALLOWED_VIDEO_TYPES

    @property
    def thumbnail_filename(self):
        return f"thumb_{self.stored_filename}"

    @property
    def file_size_human(self):
        size = self.file_size
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"
"""

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    albums = db.relationship('Album', backref='owner', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256:600000')

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class AllowedEmail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    note = db.Column(db.String(256), default='')
    added_at = db.Column(db.DateTime, default=datetime.utcnow)


class Album(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.String(512), default='')
    token = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    allow_anonymous = db.Column(db.Boolean, default=True)
    allow_upload = db.Column(db.Boolean, default=True)
    view_token = db.Column(db.String(36), unique=True, nullable=True)
    photos = db.relationship('Photo', backref='album', lazy=True, cascade='all, delete-orphan')

    @property
    def photo_count(self):
        return len(self.photos)

    @property
    def cover_photo(self):
        photos = [p for p in self.photos if p.is_photo]
        return photos[0] if photos else None


class Photo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    album_id = db.Column(db.Integer, db.ForeignKey('album.id'), nullable=False)
    uploader_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    uploader_name = db.Column(db.String(64), default='Anonymous')
    stored_filename = db.Column(db.String(256), nullable=False)
    original_filename = db.Column(db.String(256), nullable=False)
    mime_type = db.Column(db.String(64), nullable=False)
    file_size = db.Column(db.Integer, default=0)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    has_thumbnail = db.Column(db.Boolean, default=False)

    uploader = db.relationship('User', backref='photos', lazy=True)

    @property
    def is_photo(self):
        return self.mime_type in ALLOWED_PHOTO_TYPES

    @property
    def is_video(self):
        return self.mime_type in ALLOWED_VIDEO_TYPES

    @property
    def thumbnail_filename(self):
        return f"thumb_{self.stored_filename}"

    @property
    def file_size_human(self):
        size = self.file_size
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"
"""
