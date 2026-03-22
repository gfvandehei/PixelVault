from flask import render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user

from ..extensions import db, limiter
from ..models import User, AllowedEmail
from ..config import RE_USERNAME, RE_EMAIL, MAX_PASSWORD_LEN
from ..utils import is_safe_redirect


def register(app):

    @app.route('/')
    def index():
        """Redirect authenticated users to the dashboard, everyone else to the login page."""
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))
        return redirect(url_for('login'))

    @app.route('/register', methods=['GET', 'POST'])
    @limiter.limit("10 per hour")
    def register():
        """
        Handle new user registration.

        GET  — render the registration form.
        POST — validate the submitted username, email, and password, then create the account.
               Registration is invite-only: the email must already exist in AllowedEmail.
        """
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '')
            confirm = request.form.get('confirm_password', '')

            errors = []
            if not username or len(username) < 3:
                errors.append("Username must be at least 3 characters.")
            elif len(username) > 64:
                errors.append("Username must be 64 characters or fewer.")
            elif not RE_USERNAME.match(username):
                errors.append("Username may only contain letters, numbers, hyphens, and underscores.")
            if not email or not RE_EMAIL.match(email):
                errors.append("Please enter a valid email address.")
            elif len(email) > 120:
                errors.append("Email address is too long.")
            if len(password) < 8:
                errors.append("Password must be at least 8 characters.")
            elif len(password) > MAX_PASSWORD_LEN:
                errors.append("Password is too long.")
            if password != confirm:
                errors.append("Passwords do not match.")
            if not errors:
                if db.session.query(User).filter_by(username=username).first():
                    errors.append("Username already taken.")
                if db.session.query(User).filter_by(email=email).first():
                    errors.append("Email already registered.")
                if not db.session.query(AllowedEmail).filter_by(email=email).first():
                    errors.append("This email address has not been authorized to register.")

            if errors:
                for e in errors:
                    flash(e, 'error')
                return render_template('register.html', username=username, email=email)

            user = User(username=username, email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user, remember=True)
            flash('Welcome to PixelVault!', 'success')
            return redirect(url_for('dashboard'))

        return render_template('register.html')

    @app.route('/login', methods=['GET', 'POST'])
    @limiter.limit("20 per hour")
    def login():
        """
        Handle user login.

        GET  — render the login form.
        POST — verify credentials and start a session. Redirects to the 'next' query-param URL
               if present and safe, otherwise to the dashboard.
        """
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            username = request.form.get('username', '').strip()[:64]
            password = request.form.get('password', '')
            remember = bool(request.form.get('remember'))

            if len(password) > MAX_PASSWORD_LEN:
                flash('Invalid username or password.', 'error')
                return render_template('login.html', username=username)

            user = db.session.query(User).filter_by(username=username).first()
            if not user or not user.check_password(password):
                flash('Invalid username or password.', 'error')
                return render_template('login.html', username=username)

            login_user(user, remember=remember)
            next_page = request.args.get('next')
            return redirect(next_page if is_safe_redirect(next_page) else url_for('dashboard'))

        return render_template('login.html')

    @app.route('/logout')
    @login_required
    def logout():
        """Clear the current user's session and redirect to the login page."""
        logout_user()
        flash('You have been logged out.', 'info')
        return redirect(url_for('login'))
