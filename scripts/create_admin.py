from pixelvault.config import SECRET_KEY, ADMIN_PASSWORD, ADMIN_EMAIL, ADMIN_USERNAME
from pixelvault.extensions import db
from pixelvault.models import User
import click

@click.command()
@click.option("--email", prompt="Admin email", default=ADMIN_EMAIL, help="Email for the admin user")
@click.option("--username", prompt="Admin username", default=ADMIN_USERNAME, help="Username for the admin user")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True, default=ADMIN_PASSWORD, help="Password for the admin user")
def create_admin(email, username, password):
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
