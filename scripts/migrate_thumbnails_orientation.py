#!/usr/bin/env python3
"""
migrate_thumbnails_orientation.py — Regenerate thumbnails for photos with EXIF rotation.

Phones often store landscape photos with an EXIF orientation tag instead of rotating
the pixels. Thumbnails generated before this fix was applied will display sideways.
This script finds affected photos and regenerates their thumbnails correctly.

The original stored files are never modified.

Usage:
    python scripts/migrate_thumbnails_orientation.py [--dry-run] [--env .env]
"""

import argparse
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PIL import Image, ImageOps

# EXIF orientation tag
_ORIENTATION_TAG = 0x0112

def needs_rotation(img: Image.Image) -> bool:
    """Return True if the image has a non-trivial EXIF orientation tag."""
    try:
        return img.getexif().get(_ORIENTATION_TAG, 1) != 1
    except Exception:
        return False


def fix_thumbnail(photo, upload_dir: Path, dry_run: bool) -> bool:
    """Regenerate the thumbnail for a photo with EXIF rotation. Returns True if acted on."""
    original_path = upload_dir / photo.stored_filename
    thumb_path = upload_dir / f"thumb_{photo.stored_filename}"

    if not original_path.exists():
        print(f"  [SKIP] Original missing: {photo.stored_filename}")
        return False

    try:
        with Image.open(str(original_path)) as img:
            if not needs_rotation(img):
                return False

            orientation = img.getexif().get(_ORIENTATION_TAG, 1)
            print(f"  {photo.stored_filename}  (orientation={orientation})")

            if dry_run:
                return True

            img = ImageOps.exif_transpose(img)
            img = img.convert('RGB')
            img.thumbnail((400, 400), Image.LANCZOS)
            img.save(str(thumb_path), 'JPEG', quality=85)

    except Exception as e:
        print(f"  [ERROR] {photo.stored_filename}: {e}")
        return False

    return True


def main():
    parser = argparse.ArgumentParser(description="Fix sideways thumbnails caused by EXIF orientation")
    parser.add_argument('--dry-run', action='store_true',
                        help="Show what would be fixed without writing any files")
    parser.add_argument('--env', default='.env',
                        help="Path to .env file (default: .env)")
    args = parser.parse_args()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    #from pixelvault import create_app
    #from pixelvault.extensions import db
    load_dotenv(args.env)
    from pixelvault.models import Photo, Base
    from pixelvault.config import ALLOWED_PHOTO_TYPES
    import pixelvault.config as config
    print(config.SQLALCHEMY_DATABASE_URI)
    engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
    upload_dir = Path(config.UPLOAD_FOLDER).resolve()

    if not upload_dir.exists():
        sys.exit(f"ERROR: Upload folder not found: {upload_dir}")

    with Session(engine) as session:
        print(Base.metadata.tables.keys())
        
        Base.metadata.create_all(engine)
        photos = session.query(Photo).all()
        print(photos)
        photos = session.query(Photo).filter(
            Photo.has_thumbnail == True,
            Photo.mime_type.in_(ALLOWED_PHOTO_TYPES),
        ).all()

        if not photos:
            print("No photos with thumbnails found. Nothing to do.")
            return

        print(f"Checking {len(photos)} photo(s) for EXIF rotation...")
        if args.dry_run:
            print("DRY RUN — no files will be changed.\n")

        fixed = 0
        for photo in photos:
            if fix_thumbnail(photo, upload_dir, args.dry_run):
                fixed += 1

        if fixed == 0:
            print("All thumbnails are already correctly oriented. Nothing to do.")
        elif args.dry_run:
            print(f"\nDry run complete. Would regenerate {fixed}/{len(photos)} thumbnail(s).")
        else:
            print(f"\nDone. Regenerated {fixed}/{len(photos)} thumbnail(s).")


if __name__ == '__main__':
    main()
