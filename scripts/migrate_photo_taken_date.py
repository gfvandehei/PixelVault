#!/usr/bin/env python3
"""
migrate_photo_taken_date.py — Backfill Photo.taken_at from stored EXIF data.

Photos uploaded before the taken_at column existed have no capture date recorded.
This script re-reads the stored file for each such photo, extracts the EXIF
DateTimeOriginal (falling back to EXIF DateTime), and sets taken_at. Photos with
no usable EXIF data (screenshots, videos, stripped metadata) fall back to
uploaded_at so every photo ends up with a taken_at value.

Usage:
    python scripts/migrate_photo_taken_date.py [--dry-run] [--env .env]
"""

import argparse
import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PIL import Image


def main():
    parser = argparse.ArgumentParser(description="Backfill Photo.taken_at from EXIF data")
    parser.add_argument('--dry-run', action='store_true',
                        help="Show what would be updated without writing to the database")
    parser.add_argument('--env', default='.env',
                        help="Path to .env file (default: .env)")
    args = parser.parse_args()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    load_dotenv(args.env)
    from pixelvault.models import Photo, Base
    from pixelvault.config import ALLOWED_PHOTO_TYPES
    from pixelvault.utils import extract_exif_taken_at
    import pixelvault.config as config

    engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
    upload_dir = Path(config.UPLOAD_FOLDER).resolve()

    if not upload_dir.exists():
        sys.exit(f"ERROR: Upload folder not found: {upload_dir}")

    with Session(engine) as session:
        Base.metadata.create_all(engine)
        photos = session.query(Photo).filter(Photo.taken_at == None).all()

        if not photos:
            print("No photos need backfilling. Nothing to do.")
            return

        print(f"Backfilling taken_at for {len(photos)} photo(s)...")
        if args.dry_run:
            print("DRY RUN — no changes will be saved.\n")

        from_exif = 0
        from_upload = 0
        for photo in photos:
            taken_at = None
            if photo.mime_type in ALLOWED_PHOTO_TYPES:
                original_path = upload_dir / photo.stored_filename
                if original_path.exists():
                    try:
                        with Image.open(str(original_path)) as img:
                            taken_at = extract_exif_taken_at(img)
                    except Exception:
                        taken_at = None

            if taken_at:
                from_exif += 1
                print(f"  {photo.stored_filename}  taken_at={taken_at} (EXIF)")
            else:
                taken_at = photo.uploaded_at
                from_upload += 1
                print(f"  {photo.stored_filename}  taken_at={taken_at} (fallback to uploaded_at)")

            if not args.dry_run:
                photo.taken_at = taken_at

        if not args.dry_run:
            session.commit()

        print(f"\n{'Would set' if args.dry_run else 'Set'} taken_at on {len(photos)} photo(s): "
              f"{from_exif} from EXIF, {from_upload} fell back to uploaded_at.")


if __name__ == '__main__':
    main()
