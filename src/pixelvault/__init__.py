import os
import uuid
from pathlib import Path

from flask import Flask, render_template
from werkzeug.middleware.proxy_fix import ProxyFix
import sys

from .extensions import db, login_manager, limiter
from .config import *
import pixelvault.utils as utils
import logging


def create_app():
    """
    Create and configure the Flask application.

    Initialises extensions (SQLAlchemy, Flask-Login, Flask-Limiter), registers all
    route modules, attaches security headers to every response, sets up error handlers,
    and runs any pending database migrations before returning the ready app instance.
    """
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES_FOLDER.absolute()),
        static_folder=str(STATIC_FOLDER.absolute()),
    )
    print(TEMPLATES_FOLDER, file=sys.stderr)
    print(SQLALCHEMY_DATABASE_URI, file=sys.stderr)

    # ── Reverse proxy ──────────────────────────────────────────────────────
    # Requests arrive as Cloudflare -> VPS nginx -> published :5000, so the socket
    # peer is always the docker gateway and, without this, every visitor in the world
    # shares one rate-limit bucket (#30). ProxyFix rewrites REMOTE_ADDR from the
    # trailing entries of X-Forwarded-For.
    #
    # The hop count is configurable, not hard-coded, because the true value depends on
    # the VPS nginx config still being written in #28 and differs for anyone
    # self-hosting behind another topology. It must be set no higher than the number of
    # proxies that actually append to the header: too high and the client's own
    # X-Forwarded-For survives into the trusted region, letting it name any address it
    # likes and so pick which rate-limit identity it is charged under. See
    # TRUSTED_PROXY_COUNT in config.py.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXY_COUNT)

    # ── Configuration ──────────────────────────────────────────────────────
    app.config['SECRET_KEY'] = SECRET_KEY
    app.config['SQLALCHEMY_DATABASE_URI'] = SQLALCHEMY_DATABASE_URI
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
    app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE'] = SESSION_COOKIE_SECURE
    app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 30

    # The upload caps come from independent env vars that have to agree, and a
    # disagreement is otherwise invisible: the operator sees uploads refused at init
    # with nothing tying that back to the configuration. Logged, not raised — see
    # validate_upload_limits() for why a contradictory pairing must not stop boot.
    for severity, message in validate_upload_limits():
        if severity == 'error':
            app.logger.error('Upload limit misconfiguration: %s', message)
        else:
            app.logger.warning('Upload limit advisory: %s', message)

    # ── Extensions ─────────────────────────────────────────────────────────
    db.init_app(app)
    login_manager.init_app(app)
    limiter.init_app(app)
    # ── Routes ─────────────────────────────────────────────────────────────
    # Import after extensions are bound so decorators resolve correctly
    from .routes import register_all
    register_all(app)

    # ── Security headers ───────────────────────────────────────────────────
    @app.after_request
    def set_security_headers(response):
        """Attach security-related HTTP headers to every outgoing response."""
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
        if SESSION_COOKIE_SECURE:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        return response

    # ── Error handlers ─────────────────────────────────────────────────────
    @app.errorhandler(403)
    def forbidden(e):
        """Render a friendly 403 page when access is denied."""
        return render_template('error.html', code=403, message="Access denied."), 403

    @app.errorhandler(404)
    def not_found(e):
        """Render a friendly 404 page when a route or resource is not found."""
        return render_template('error.html', code=404, message="Page not found."), 404

    @app.errorhandler(413)
    def too_large(e):
        """Render a friendly 413 page when an uploaded file exceeds MAX_CONTENT_LENGTH."""
        return render_template('error.html', code=413, message="File too large."), 413

    @app.errorhandler(429)
    def rate_limited(e):
        """Render a friendly 429 page when a client exceeds a rate limit."""
        return render_template('error.html', code=429, message="Too many requests. Please slow down."), 429

    # ── CLI commands ───────────────────────────────────────────────────────
    @app.cli.command('create-admin')
    def create_admin_command():
        """Create the admin user from ADMIN_USERNAME / ADMIN_EMAIL / ADMIN_PASSWORD env vars."""
        import click
        from .models import User
        utils.create_admin(ADMIN_USERNAME, ADMIN_EMAIL, ADMIN_PASSWORD, db.session, click.echo)

    @app.cli.command('cleanup-uploads')
    def cleanup_uploads_command():
        """Delete upload sessions idle past UPLOAD_SESSION_TTL_HOURS and their .part files."""
        import click
        from .uploads import sweep_expired_sessions
        removed = sweep_expired_sessions(db.session, UPLOAD_FOLDER, UPLOAD_SESSION_TTL_HOURS)
        click.echo(f"Reclaimed {removed} abandoned upload(s).")

    # ── Database init & migrations ─────────────────────────────────────────
    with app.app_context():
        db.create_all()
        if ADMIN_EMAIL:
            print("ADMIN email was set creating admin")
            utils.create_admin(ADMIN_USERNAME, ADMIN_EMAIL, ADMIN_PASSWORD, db.session, app.logger.info)
            print("FINISHED CREATING ADMIN")
        _run_migrations()

    return app


def _run_migrations():
    """Add new columns to existing databases without breaking old deployments."""
    from .models import Album

    with db.engine.connect() as conn:
        for stmt in [
            "ALTER TABLE album ADD COLUMN allow_upload BOOLEAN NOT NULL DEFAULT 1",
            "ALTER TABLE album ADD COLUMN view_token VARCHAR(36)",
            """CREATE TABLE IF NOT EXISTS album_access (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES user(id),
                album_id INTEGER NOT NULL REFERENCES album(id),
                accessed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, album_id)
            )""",
            "ALTER TABLE album_access ADD COLUMN access_type VARCHAR(10) NOT NULL DEFAULT 'upload'",
            "ALTER TABLE photo ADD COLUMN taken_at DATETIME",
            """CREATE TABLE IF NOT EXISTS upload_session (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id VARCHAR(36) NOT NULL UNIQUE,
                album_id INTEGER NOT NULL REFERENCES album(id),
                user_id INTEGER NOT NULL REFERENCES user(id),
                client_key VARCHAR(64) NOT NULL,
                original_filename VARCHAR(256) NOT NULL,
                total_size INTEGER NOT NULL,
                received_bytes INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, album_id, client_key)
            )""",
        ]:
            try:
                conn.execute(db.text(stmt))
                conn.commit()
            except Exception:
                pass  # Column/table already exists
    albums = db.session.query(Album).filter(Album.view_token == None).all()
    for album in albums:
        album.view_token = str(uuid.uuid4())
    if albums:
        db.session.commit()
