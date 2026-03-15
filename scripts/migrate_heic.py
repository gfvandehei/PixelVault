#!/usr/bin/env python3
"""
migrate_heic.py — Convert stored HEIC/HEIF files to JPEG in-place.

Updates stored_filename and mime_type in the database so existing records
continue to work without any change needed on the frontend.

Original filenames shown to users are left untouched.

Usage:
    python migrate_heic.py [--dry-run]
"""

import argparse
import os
import sys
from pathlib import Path

# ── Setup ──────────────────────────────────────────────────────────────────

# Load .env before importing app so config is correct
from dotenv import load_dotenv
load_dotenv()

# If DATABASE_URL isn't set but DATA_DIRECTORY is, construct the path from it
if 'DATABASE_URL' not in os.environ and os.environ.get('DATA_DIRECTORY'):
    data_dir = os.environ['DATA_DIRECTORY'].rstrip('/')
    os.environ['DATABASE_URL'] = f'sqlite:///{data_dir}/pixelvault.db'

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    sys.exit("ERROR: pillow-heif is not installed. Run: pip install pillow-heif")

from PIL import Image

# Import app and db from the package
sys.path.insert(0, str(Path(__file__).parent))
from pixelvault import create_app
from pixelvault.extensions import db
from pixelvault.models import Photo
app = create_app()

HEIC_EXTS = {'.heic', '.heif'}
HEIC_MIMES = {'image/heic', 'image/heif'}


def convert_photo(photo, upload_dir: Path, dry_run: bool) -> bool:
    """Convert a single Photo record from HEIC to JPEG. Returns True on success."""
    old_stored = photo.stored_filename
    old_path = upload_dir / old_stored
    old_thumb = upload_dir / f"thumb_{old_stored}"

    if not old_path.exists():
        print(f"  [SKIP] File missing on disk: {old_stored}")
        return False

    # Build new JPEG filename (same UUID, different extension)
    stem = Path(old_stored).stem
    new_stored = f"{stem}.jpg"
    new_path = upload_dir / new_stored
    new_thumb = upload_dir / f"thumb_{new_stored}"

    if new_path.exists():
        print(f"  [SKIP] JPEG already exists: {new_stored}")
        return False

    print(f"  {old_stored}  →  {new_stored}")

    if dry_run:
        return True

    # Convert HEIC → JPEG
    try:
        with Image.open(str(old_path)) as img:
            img = img.convert('RGB')
            img.save(str(new_path), 'JPEG', quality=92)
    except Exception as e:
        print(f"  [ERROR] Conversion failed: {e}")
        return False

    # Regenerate thumbnail (the old thumb may also be HEIC or may not exist)
    has_thumbnail = False
    try:
        src = new_path  # use the freshly converted JPEG as source
        with Image.open(str(src)) as img:
            img = img.convert('RGB')
            img.thumbnail((400, 400), Image.LANCZOS)
            img.save(str(new_thumb), 'JPEG', quality=85)
        has_thumbnail = True
    except Exception as e:
        print(f"  [WARN] Thumbnail generation failed: {e}")

    # Update DB record
    photo.stored_filename = new_stored
    photo.mime_type = 'image/jpeg'
    photo.file_size = new_path.stat().st_size
    photo.has_thumbnail = has_thumbnail

    # Remove old files
    old_path.unlink(missing_ok=True)
    old_thumb.unlink(missing_ok=True)

    return True


def main():
    parser = argparse.ArgumentParser(description="Convert stored HEIC files to JPEG")
    parser.add_argument('--dry-run', action='store_true',
                        help="Show what would be done without making any changes")
    args = parser.parse_args()

    upload_dir = Path(app.config['UPLOAD_FOLDER']).resolve()
    if not upload_dir.exists():
        sys.exit(f"ERROR: Upload folder not found: {upload_dir}")

    with app.app_context():
        # Find all Photo records with HEIC stored filenames
        heic_photos = [
            p for p in Photo.query.all()
            if Path(p.stored_filename).suffix.lower() in HEIC_EXTS
            or p.mime_type in HEIC_MIMES
        ]

        if not heic_photos:
            print("No HEIC photos found in database. Nothing to do.")
            return

        print(f"Found {len(heic_photos)} HEIC photo(s) to convert.")
        if args.dry_run:
            print("DRY RUN — no files will be changed.\n")

        converted = 0
        for photo in heic_photos:
            ok = convert_photo(photo, upload_dir, args.dry_run)
            if ok:
                converted += 1

        if not args.dry_run and converted:
            db.session.commit()
            print(f"\nDone. Converted {converted}/{len(heic_photos)} file(s).")
        elif args.dry_run:
            print(f"\nDry run complete. Would convert {converted}/{len(heic_photos)} file(s).")
        else:
            print(f"\nDone. {converted}/{len(heic_photos)} file(s) converted.")


if __name__ == '__main__':
    main()
