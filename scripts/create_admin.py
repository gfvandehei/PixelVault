#!/usr/bin/env python3
"""
Standalone script to create an admin user.

Usage:
    python scripts/create_admin.py --env .env --user admin --email admin@example.com --password secret

The .env file must contain DATABASE_URL (falls back to sqlite:///pixelvault.db if absent).
"""
import argparse
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


def main():
    parser = argparse.ArgumentParser(description="Create a PixelVault admin user")
    parser.add_argument("--env", default=".env", help="Path to .env file (default: .env)")
    parser.add_argument("--user", required=True, help="Admin username")
    parser.add_argument("--email", required=True, help="Admin email")
    parser.add_argument("--password", required=True, help="Admin password")
    args = parser.parse_args()

    load_env(args.env)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from pixelvault.models import Base
    from pixelvault.utils import create_admin

    db_url = os.environ.get("DATABASE_URL", "sqlite:///pixelvault.db")
    print(db_url)
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        create_admin(args.user, args.email, args.password, session, print)
        session.commit()


if __name__ == "__main__":
    main()
