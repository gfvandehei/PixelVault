"""Album ZIP download: what the archive contains, what it costs, and what it is capped at.

Three properties are under test here, and they are not the same property:

* **Correctness** — every stored file appears under the name its uploader gave it,
  and two uploads that shared a name are told apart rather than one shadowing the
  other in the archive.
* **Cost** — assembling the archive does not allocate the archive. This is the
  regression that issue #41 was: ``build_album_zip`` buffered the whole album in an
  ``io.BytesIO`` and ``send_file`` held it for the life of the response, so eight
  concurrent downloads on a ``--workers 2 --threads 4`` container was an OOM kill.
  The assertions below measure peak Python allocation across two albums an order of
  magnitude apart in size and require that it *not* track the album.
* **Containment** — the staging file leaves nothing behind, and the endpoints have a
  per-user budget so the CPU and disk a download does spend cannot be spent in a loop.

The media here is ``os.urandom`` under an image extension rather than a real JPEG:
``build_album_zip`` reads bytes off disk by ``stored_filename`` and never decodes
them, and incompressible bytes are the honest case anyway — videos are stored
uncompressed and DEFLATE does not shrink them, which is exactly why the old buffer
was the size of the album.
"""

import io
import os
import tracemalloc
import zipfile
from pathlib import Path

import pytest

from pixelvault.extensions import db
from pixelvault.models import Album, Photo
from pixelvault.utils import ALBUM_ZIP_RATE_LIMIT, ZIP_STAGING_SUBDIR, build_album_zip

from .conftest import Ref, login

#: How many archives one account may build per endpoint per hour. Read off the
#: constant rather than repeated as a literal, so retuning the budget retunes the
#: test with it instead of breaking it.
ZIP_BUDGET = int(ALBUM_ZIP_RATE_LIMIT.split()[0])


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def stock_album(app, user, upload_dir):
    """Return a callable that fills an album with files of given names and sizes.

    Takes ``(original_filename, size)`` pairs and writes each one to the media root
    under a distinct stored name, so a repeated original name is a genuine collision
    in the archive and not two rows pointing at one file.
    """
    def _fill(entries, name="Trip"):
        with app.app_context():
            row = Album(name=name, owner_id=user.id, description="")
            db.session.add(row)
            db.session.commit()
            album_id, token, view_token = row.id, row.token, row.view_token

            upload_dir.mkdir(parents=True, exist_ok=True)
            sources = {}
            for index, (original, size) in enumerate(entries):
                stored = f"{album_id}-{index}.jpg"
                payload = os.urandom(size)
                (upload_dir / stored).write_bytes(payload)
                sources[stored] = payload
                db.session.add(Photo(
                    album_id=album_id,
                    uploader_id=user.id,
                    stored_filename=stored,
                    original_filename=original,
                    mime_type="image/jpeg",
                    file_size=size,
                ))
            db.session.commit()
            return Ref(id=album_id, token=token, view_token=view_token,
                       name=name, sources=sources)
    return _fill


@pytest.fixture
def staging_dir(upload_dir):
    """The directory album archives are staged in while they are being built."""
    return upload_dir / ZIP_STAGING_SUBDIR


def read_zip(payload):
    """Return the archive's ``{name: bytes}`` mapping, proving it is a readable ZIP."""
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.testzip() is None, "archive failed its own CRC check"
        return {name: zf.read(name) for name in zf.namelist()}


# ── What the archive contains ──────────────────────────────────────────────

def test_archive_holds_every_file_under_its_original_name(client, stock_album):
    """The download carries the album's bytes back, named as the uploader named them."""
    album = stock_album([("beach.jpg", 900), ("sunset.jpg", 1500)])

    response = client.get(f"/album/{album.token}/download")

    assert response.status_code == 200
    members = read_zip(response.data)
    assert set(members) == {"beach.jpg", "sunset.jpg"}
    assert sorted(members.values(), key=len) == sorted(album.sources.values(), key=len)


def test_repeated_original_names_are_suffixed_not_collapsed(client, stock_album):
    """Three uploads called IMG_0001.jpg produce three distinct members, not one.

    Cameras hand out the same name to everyone, so this is the ordinary case rather
    than an adversarial one — and a ZIP with duplicate entry names is a silently
    lossy archive on extraction.
    """
    album = stock_album([("IMG_0001.jpg", 400), ("IMG_0001.jpg", 500), ("IMG_0001.jpg", 600)])

    members = read_zip(client.get(f"/album/{album.token}/download").data)

    assert set(members) == {"IMG_0001.jpg", "IMG_0001_1.jpg", "IMG_0001_2.jpg"}
    # Suffixing must rename, not re-copy: the three members carry three different files.
    assert len({bytes(v) for v in members.values()}) == 3
    assert {len(v) for v in members.values()} == {400, 500, 600}


def test_row_without_a_file_on_disk_is_skipped(app, client, stock_album, upload_dir):
    """A Photo row whose bytes are gone drops out of the archive instead of 500ing."""
    album = stock_album([("kept.jpg", 300), ("vanished.jpg", 300)])
    with app.app_context():
        gone = db.session.query(Photo).filter_by(
            album_id=album.id, original_filename="vanished.jpg").one()
        (upload_dir / gone.stored_filename).unlink()

    response = client.get(f"/album/{album.token}/download")

    assert response.status_code == 200
    assert set(read_zip(response.data)) == {"kept.jpg"}


def test_empty_album_still_downloads_as_a_valid_archive(client, album):
    """No photos is an empty ZIP, not an error and not a zero-byte body."""
    response = client.get(f"/album/{album.token}/download")

    assert response.status_code == 200
    assert read_zip(response.data) == {}


def test_response_is_a_named_attachment_with_a_real_content_length(client, stock_album):
    """The length is set explicitly because send_file cannot derive one from an unnamed file.

    Without it the response falls back to chunked encoding and a browser downloading
    a multi-gigabyte album gets no progress bar — see ``send_album_zip``.
    """
    album = stock_album([("beach.jpg", 2048)], name="Summer Trip")

    response = client.get(f"/album/{album.token}/download")

    assert response.headers["Content-Disposition"] == "attachment; filename=Summer_Trip.zip"
    assert response.headers["Content-Type"] == "application/zip"
    assert int(response.headers["Content-Length"]) == len(response.data)
    assert response.headers.get("Transfer-Encoding") is None


# ── What the archive costs ─────────────────────────────────────────────────

def _peak_bytes_building(app, album_id):
    """Return ``(peak Python allocation, archive size)`` for zipping one album."""
    with app.app_context():
        album = db.session.get(Album, album_id)
        # Touch the relationship first: loading the rows is the caller's cost, not
        # the archive's, and leaving it inside the measurement would put a fixed
        # ORM overhead into the number being compared.
        len(album.photos)
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            handle = build_album_zip(album)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        # Sized by seeking rather than fstat, so the measurement works against any
        # implementation — including the BytesIO one this test exists to rule out.
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.close()
        return peak, size


def test_archive_is_not_assembled_in_memory(app, stock_album):
    """Peak allocation stays small next to an album many times its size.

    The pre-fix implementation's peak was, by construction, at least the size of the
    finished archive; this asserts it is now a small constant.
    """
    album = stock_album([(f"clip{i}.jpg", 2 * 1024 * 1024) for i in range(4)])

    peak, size = _peak_bytes_building(app, album.id)

    assert size > 7 * 1024 * 1024, "fixture must be incompressible enough to matter"
    assert peak < size // 8, (
        f"zipping a {size} byte album allocated {peak} bytes; the archive is being "
        f"buffered rather than streamed"
    )


def test_peak_memory_does_not_scale_with_album_size(app, stock_album):
    """A 32x larger album does not cost 32x the memory to zip.

    The absolute ceiling above could in principle be met by an implementation that
    still buffers, just efficiently. This is the shape assertion: hold the file count
    fixed, multiply the bytes, and require the peak to stay flat.
    """
    small = stock_album([(f"clip{i}.jpg", 64 * 1024) for i in range(4)], name="Small")
    large = stock_album([(f"clip{i}.jpg", 2 * 1024 * 1024) for i in range(4)], name="Large")

    small_peak, small_size = _peak_bytes_building(app, small.id)
    large_peak, large_size = _peak_bytes_building(app, large.id)

    assert large_size > 20 * small_size
    assert large_peak < small_peak + 256 * 1024, (
        f"peak grew from {small_peak} to {large_peak} while the album grew from "
        f"{small_size} to {large_size} bytes"
    )


# ── What the staging file leaves behind ────────────────────────────────────

def test_staging_file_has_no_name_while_it_is_open(app, stock_album, staging_dir):
    """The archive is unlinked at creation, so nothing can leak it — not even a crash.

    Cleanup is therefore the kernel reclaiming an unreferenced inode rather than a
    teardown hook that a killed worker would never run.
    """
    album = stock_album([("beach.jpg", 4096)])

    with app.app_context():
        handle = build_album_zip(db.session.get(Album, album.id))
        try:
            # A real descriptor on the media volume, not an in-memory buffer...
            assert not isinstance(handle, io.BytesIO)
            assert os.fstat(handle.fileno()).st_size > 0
            # ...with no directory entry anywhere for it to be left behind as...
            assert list(staging_dir.iterdir()) == []
            # ...and it is nonetheless a complete, readable archive.
            assert set(read_zip(handle.read())) == {"beach.jpg"}
        finally:
            handle.close()


def test_downloads_leak_neither_files_nor_descriptors(client, stock_album, staging_dir):
    """Repeated downloads leave the staging directory empty and the fd table flat.

    The descriptor half matters as much as the file half: an unlinked file that is
    never closed still holds its blocks, so a response that failed to close the
    handle would fill the volume just as surely as a named temp file left behind.
    """
    album = stock_album([("beach.jpg", 8192), ("sunset.jpg", 8192)])
    fd_dir = Path("/proc/self/fd")

    # One warm-up request first, so lazily-opened descriptors (the SQLite
    # connection, the log handler) are already counted in the baseline.
    client.get(f"/album/{album.token}/download").close()
    before = len(list(fd_dir.iterdir()))

    for _ in range(5):
        response = client.get(f"/album/{album.token}/download")
        assert response.status_code == 200
        response.close()

    assert list(staging_dir.iterdir()) == []
    assert len(list(fd_dir.iterdir())) == before


# ── What the endpoints are capped at ───────────────────────────────────────

def test_download_is_charged_against_a_per_user_budget(client, stock_album):
    """The budget'th archive is served; the next one is refused.

    Before #41 these routes carried no limit of their own, so the reproduction in the
    issue — 25 downloads in a row, all 200 — is what this asserts is now impossible.
    """
    album = stock_album([("beach.jpg", 512)])

    for _ in range(ZIP_BUDGET):
        assert client.get(f"/album/{album.token}/download").status_code == 200

    assert client.get(f"/album/{album.token}/download").status_code == 429


def test_share_and_view_downloads_are_limited_too(client, stock_album):
    """Both public-link download routes carry the same budget as the owner's.

    They are the ones an attacker actually reaches: anyone holding a share link can
    call them, whereas /album/<token>/download needs to be the album's owner.
    """
    album = stock_album([("beach.jpg", 512)])

    for path in (f"/share/{album.token}/download", f"/view/{album.view_token}/download"):
        for _ in range(ZIP_BUDGET):
            assert client.get(path).status_code == 200
        assert client.get(path).status_code == 429


def test_each_download_route_keeps_its_own_bucket(client, stock_album):
    """Exhausting one route's budget does not deny the other two.

    Flask-Limiter scopes a limit per endpoint, so the worst honest hour from one
    account is three budgets, not one — stated here so the number in
    ALBUM_ZIP_RATE_LIMIT is read with that in mind.
    """
    album = stock_album([("beach.jpg", 512)])
    for _ in range(ZIP_BUDGET):
        client.get(f"/album/{album.token}/download")
    assert client.get(f"/album/{album.token}/download").status_code == 429

    assert client.get(f"/share/{album.token}/download").status_code == 200
    assert client.get(f"/view/{album.view_token}/download").status_code == 200


def test_the_budget_is_keyed_by_user_not_by_address(app, client, other_user, stock_album,
                                                    grant_access):
    """One account exhausting its budget does not lock out another on the same address.

    ``rate_limit_key`` resolves an authenticated caller to ``user:<id>``, which is
    what makes this a per-account quota rather than a bucket shared by everyone
    behind one NAT — and what stops a rate limit on an expensive route from becoming
    a way to deny the feature to a whole household.
    """
    album = stock_album([("beach.jpg", 512)])
    for _ in range(ZIP_BUDGET):
        client.get(f"/share/{album.token}/download")
    assert client.get(f"/share/{album.token}/download").status_code == 429

    second = app.test_client()

    login(second, other_user)

    # The grant the new authorisation model requires of any non-owner (#38/#40).

    # Without it this asserts 403 and stops describing rate limiting at all.

    grant_access(album.id, other_user.id, "view")
    assert second.get(f"/share/{album.token}/download").status_code == 200
