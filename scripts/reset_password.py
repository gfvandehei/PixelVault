#!/usr/bin/env python3
"""
Standalone script to reset a user's password.

Usage:
    python scripts/reset_password.py --env .env --email user@example.com --password newsecret
    python scripts/reset_password.py --env .env --user alice          # prompts for the password

The .env file must contain DATABASE_URL (falls back to sqlite:///pixelvault.db if absent).
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

# Allow imports from src/
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


def load_env(env_path: str):
    """Parse a .env file and set variables into os.environ (existing vars take priority)."""
    path = Path(env_path)
    if not path.exists():
        print(f"Error: .env file not found: {env_path}", file=sys.stderr)
        sys.exit(1)

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def prompt_password():
    """Ask for the new password twice and return it once both entries match."""
    while True:
        password = getpass.getpass("New password: ")
        if not password:
            print("Password cannot be empty.", file=sys.stderr)
            continue
        if password != getpass.getpass("Confirm password: "):
            print("Passwords do not match, try again.", file=sys.stderr)
            continue
        return password


def main():
    parser = argparse.ArgumentParser(description="Reset a PixelVault user's password")
    parser.add_argument("--env", default=".env", help="Path to .env file (default: .env)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--user", help="Username of the account to reset")
    group.add_argument("--email", help="Email of the account to reset")
    parser.add_argument("--password", help="New password (prompted for if omitted)")
    args = parser.parse_args()

    load_env(args.env)

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from pixelvault.models import User

    db_url = os.environ.get("DATABASE_URL", "sqlite:///pixelvault.db")
    print(db_url)
    engine = create_engine(db_url)

    with Session(engine) as session:
        if args.user:
            stmt = select(User).where(User.username == args.user)
            label = f"username '{args.user}'"
        else:
            stmt = select(User).where(User.email == args.email)
            label = f"email '{args.email}'"

        user = session.scalars(stmt).first()
        if user is None:
            print(f"Error: no user found with {label}", file=sys.stderr)
            sys.exit(1)

        password = args.password or prompt_password()
        user.set_password(password)
        session.commit()
        print(f"Password reset for {user.username} <{user.email}>")


if __name__ == "__main__":
    main()
