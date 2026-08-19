import uuid
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

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
from .config import ALLOWED_PHOTO_TYPES, ALLOWED_VIDEO_TYPES, UPLOAD_PARTIALS_SUBDIR

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

class InviteState(str, Enum):
    """The state of one invite, as shown in the admin panel.

    A ``str`` enum so a template can compare against the plain value and a
    member renders as itself without a ``.value`` everywhere.
    """
    ACCEPTED = 'accepted'      # account created; terminal
    # Not a cosmetic state. Every allowed_email row that predates this feature has
    # no token, and once registration is link-only those addresses are silently
    # unusable — the person was told they could sign up and now cannot, with
    # nothing in the UI to say why. LEGACY is what lets the admin panel list them
    # as "No invite sent" beside a Send invite button, and it is deliberately
    # resolved by an admin's click rather than by a migration that emails a
    # year-old address on deploy day (design §11 Q9).
    LEGACY = 'legacy'          # whitelist row from before invites existed; no token
    EXPIRED = 'expired'        # token minted but the TTL ran out unclicked
    SEND_FAILED = 'send_failed'  # token is live; the relay refused the message
    ISSUED = 'issued'          # token minted, never emailed (copy-link path)
    SENT = 'sent'              # emailed, awaiting the click


class AllowedEmail(Base):
    """An authorized email address and the invite issued against it.

    One row is both halves of the fact: the whitelist entry that permits
    registration, and the credential that carries it to a person. They are kept
    together because in this app neither exists without the other — see
    docs/invite_registration_design.md §4.

    The token itself is never stored. ``token_hash`` holds the SHA-256 of it, so
    a leaked backup or a screenshot of the admin page cannot be used to create an
    account; the plaintext exists only in the sent email and, for the copy-link
    fallback, in a single flash message. A resend therefore cannot re-show the
    old link and mints a new one instead, which also revokes the old.

    Lifecycle position is read from :attr:`state`, derived on every access rather
    than stored — see that property for why.
    """
    __tablename__ = 'allowed_email'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    note: Mapped[str] = mapped_column(String(256), default='')
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Indexed because acceptance looks a row up by this and nothing else; an
    # unindexed scan would also leak, through timing, how far down the table a
    # guessed token sits.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=True, index=True)
    token_issued_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    # Denormalised from token_issued_at + the TTL so the admin table can sort and
    # display honest expiry without every render re-deriving it against a config
    # value that may have changed since the token was minted.
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    prefill_username: Mapped[str] = mapped_column(String(64), default='')
    last_sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    send_count: Mapped[int] = mapped_column(Integer, default=0)
    # Truncated to fit, and never carries the token: an SMTP failure string is
    # rendered back to the admin and may reach a log. Empty means the last send
    # succeeded.
    last_send_error: Mapped[str] = mapped_column(String(256), default='')
    accepted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    accepted_user_id: Mapped[int] = mapped_column(Integer, ForeignKey('user.id'), nullable=True)
    invited_by_id: Mapped[int] = mapped_column(Integer, ForeignKey('user.id'), nullable=True)

    @property
    def state(self) -> InviteState:
        """Return where this invite stands, derived fresh on every read.

        Deliberately not a stored column: the EXPIRED branch depends on the
        wall clock, so a persisted value would be wrong from the moment a TTL
        elapses with nobody looking, and would need a sweep job to stay honest.
        Deriving it costs one comparison.

        The branch order is the specification (design §4), not an accident —
        each test precedes the next because it describes a *stronger* fact
        about the row:

        * ACCEPTED first because it is terminal. The row keeps its old
          ``expires_at``, so a TTL that lapsed after the account was created
          would otherwise re-report a live account's invite as EXPIRED.
        * LEGACY next because a row with no token has nothing the remaining
          branches can meaningfully test — no expiry, no send history. It is
          also the only state an admin resolves by *issuing* rather than
          resending.
        * EXPIRED above SEND_FAILED because a dead token is the binding
          problem: re-attempting delivery of a link that no longer works helps
          nobody, and both faults are fixed by the same rotate-and-resend.
        * SEND_FAILED above the two success states because the token is fine
          and only delivery failed — it is a *pending* invite (see
          :attr:`is_pending`) that happens to need another attempt, which is
          exactly why it does not outrank EXPIRED.
        * ISSUED before SENT distinguishes "minted but never emailed" — the
          copy-link path, and an invite that is issued with mail disabled —
          from one the relay has accepted.
        """
        if self.accepted_at is not None:
            return InviteState.ACCEPTED
        if self.token_hash is None:
            return InviteState.LEGACY
        if self.expires_at is not None and datetime.utcnow() >= self.expires_at:
            return InviteState.EXPIRED
        if self.last_send_error:
            return InviteState.SEND_FAILED
        if self.last_sent_at is None:
            return InviteState.ISSUED
        return InviteState.SENT

    @property
    def is_pending(self) -> bool:
        """Return True while a usable token is outstanding and nobody has accepted it.

        The three states that share one property: the link works today, so the
        admin's action is to wait or resend, not to issue anew.
        """
        return self.state in (InviteState.ISSUED, InviteState.SENT, InviteState.SEND_FAILED)

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
    taken_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
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


class UploadSession(Base):
    """A large file mid-flight, uploaded in chunks and resumable across drops and reloads.

    One row exists per file being streamed in slices. The row is the authority on
    where the transfer stands: ``received_bytes`` counts the bytes of the ``.part``
    file on disk that are known-good, and every chunk is appended at exactly that
    offset. It is both the resume cursor handed back to a returning client and the
    integrity anchor a chunk append truncates back to.

    Rows are short-lived. ``complete`` deletes one after turning the partial into a
    ``Photo``; the sweep in ``pixelvault.uploads`` deletes any that fall silent for
    longer than ``UPLOAD_SESSION_TTL_HOURS``. See docs/upload_protocol.md for the
    wire contract these fields back.
    """
    __tablename__ = 'upload_session'
    # Makes `init` idempotent: re-picking the same file returns the session already
    # in flight and its true offset instead of orphaning the .part and starting over.
    __table_args__ = (UniqueConstraint('user_id', 'album_id', 'client_key'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    upload_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    album_id: Mapped[int] = mapped_column(Integer, ForeignKey('album.id'), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('user.id'), nullable=False)
    client_key: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(256), nullable=False)
    total_size: Mapped[int] = mapped_column(Integer, nullable=False)
    received_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def is_complete(self):
        """Return True once every declared byte has landed and the partial is ready to commit."""
        return self.received_bytes >= self.total_size

    @property
    def remaining_bytes(self):
        """Return how many bytes are still outstanding, never negative."""
        return max(0, self.total_size - self.received_bytes)

    @property
    def progress_percent(self):
        """Return transfer progress as a percentage, for logging and status responses."""
        if self.total_size <= 0:
            return 0.0
        return round(100.0 * self.received_bytes / self.total_size, 1)

    def expires_at(self, ttl_hours):
        """Return the moment this session stops being resumable, measured from its last chunk."""
        return (self.updated_at or self.created_at) + timedelta(hours=ttl_hours)

    def is_expired(self, ttl_hours, now=None):
        """Return True if the session has been silent for longer than the TTL and may be swept.

        Measured from ``updated_at`` so an upload that is still making progress never
        expires under a slow client, only one that has genuinely been abandoned.
        """
        return (now or datetime.utcnow()) >= self.expires_at(ttl_hours)

    def partial_path(self, upload_dir):
        """Return the on-disk path of this session's ``.part`` file.

        Named from ``upload_id`` rather than anything the client supplied, so no
        request can steer a write outside the partials directory.
        """
        return Path(upload_dir) / UPLOAD_PARTIALS_SUBDIR / f"{self.upload_id}.part"

    def accepts_chunk_at(self, offset):
        """Return True if a chunk declaring this start offset lines up with the resume cursor.

        A mismatch is normal control flow, not an error: it is how a resuming or
        racing client discovers where to seek (409, see docs/upload_protocol.md §8).
        """
        return offset == self.received_bytes

    def would_overrun(self, chunk_length):
        """Return True if accepting a chunk of this size would push past the declared total.

        Without this check a client can stream unbounded bytes to disk by understating
        ``total_size`` at init, since MAX_CONTENT_LENGTH only bounds a single chunk.
        """
        return self.received_bytes + chunk_length > self.total_size
