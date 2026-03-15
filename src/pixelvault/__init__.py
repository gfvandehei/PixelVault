import os
import uuid
from pathlib import Path

from flask import Flask, render_template

from .extensions import db, login_manager, limiter
from .config import *


def create_app():
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parents[2]/ 'templates'),
    )
    print(SQLALCHEMY_DATABASE_URI)
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
        return render_template('error.html', code=403, message="Access denied."), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template('error.html', code=404, message="Page not found."), 404

    @app.errorhandler(413)
    def too_large(e):
        return render_template('error.html', code=413, message="File too large."), 413

    @app.errorhandler(429)
    def rate_limited(e):
        return render_template('error.html', code=429, message="Too many requests. Please slow down."), 429

    # ── CLI commands ───────────────────────────────────────────────────────
    @app.cli.command('create-admin')
    def create_admin_command():
        """Create the admin user from ADMIN_USERNAME / ADMIN_EMAIL / ADMIN_PASSWORD env vars."""
        import click
        from .models import User

        username = ADMIN_USERNAME
        email = ADMIN_EMAIL
        password = ADMIN_PASSWORD

        if not username or not email or not password:
            click.echo('Set ADMIN_USERNAME, ADMIN_EMAIL, and ADMIN_PASSWORD environment variables.')
            return

        if User.query.filter_by(is_admin=True).first():
            click.echo('An admin user already exists.')
            return
        if User.query.filter_by(email=email).first():
            click.echo(f'A user with email {email} already exists.')
            return

        admin = User(username=username, email=email, is_admin=True)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        click.echo(f'Admin user "{username}" created successfully.')

    # ── Database init & migrations ─────────────────────────────────────────
    with app.app_context():
        db.create_all()
        _run_migrations()

    return app


def _run_migrations():
    """Add new columns to existing databases without breaking old deployments."""
    from .models import Album

    with db.engine.connect() as conn:
        for stmt in [
            "ALTER TABLE album ADD COLUMN allow_upload BOOLEAN NOT NULL DEFAULT 1",
            "ALTER TABLE album ADD COLUMN view_token VARCHAR(36)",
        ]:
            try:
                conn.execute(db.text(stmt))
                conn.commit()
            except Exception:
                pass  # Column already exists

    albums = Album.query.filter(Album.view_token == None).all()
    for album in albums:
        album.view_token = str(uuid.uuid4())
    if albums:
        db.session.commit()
