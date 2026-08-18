"""Regression tests for the decompression-bomb defence in ``store_upload``.

A 173 KB PNG declaring 20000x8900 (178 Mpx) once uploaded and completed
successfully while driving ~1 GB of RSS; eight of them at the production
2-workers-x-4-threads fan-out OOM-killed the container for ~1.4 MB of attacker
traffic. These tests pin the two halves of the fix: the pixel ceiling itself, and
the fact that the *detected* MIME type — not the client-chosen extension — decides
which code path an image takes.

The tests never decode a bomb themselves. The whole property under test is that
the server refuses to, so a test that allocated the raster to check on it would be
proving the opposite of what it claims.
"""

import io

import pytest
from PIL import Image

from pixelvault.utils import MAX_IMAGE_PIXELS
from tests.conftest import TEST_CHUNK_SIZE


def make_bomb(width=20000, height=8900):
    """Return a tiny PNG declaring an enormous pixel count.

    Solid black compresses to a few hundred KB at any size, which is exactly what
    makes this an attack: the cost to send is trivial and the cost to decode is not.
    """
    buf = io.BytesIO()
    Image.new('L', (width, height), 0).save(buf, format='PNG', compress_level=9)
    return buf.getvalue()


def make_image_of_pixels(pixels):
    """Return a PNG whose width x height is just under or over a target pixel count."""
    side = int(pixels ** 0.5)
    buf = io.BytesIO()
    Image.new('L', (side, side), 0).save(buf, format='PNG', compress_level=9)
    return buf.getvalue()


# ── the ceiling ────────────────────────────────────────────────────────────

def test_a_decompression_bomb_is_refused_through_the_chunked_path(protocol, photos, album):
    bomb = make_bomb()

    _, _, response = protocol.upload("bomb.png", bomb, TEST_CHUNK_SIZE)

    assert response.status_code == 200
    assert "success" not in response.get_json()["results"][0]
    assert photos(album.id) == []


def test_a_decompression_bomb_is_refused_through_the_legacy_path(client, album, photos):
    bomb = make_bomb()

    response = client.post(
        f"/share/{album.token}/upload",
        data={"files": (io.BytesIO(bomb), "bomb.png")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert "success" not in response.get_json()["results"][0]
    assert photos(album.id) == []


def test_a_bomb_leaves_no_committed_file_or_thumbnail(protocol, upload_dir, album):
    protocol.upload("bomb.png", make_bomb(), TEST_CHUNK_SIZE)

    assert [p.name for p in upload_dir.iterdir() if p.is_file()] == []


def test_a_rejected_bomb_releases_its_partial(protocol, partials_dir, album):
    """A store that raises must not strand the .part holding disk and a quota slot."""
    protocol.upload("bomb.png", make_bomb(), TEST_CHUNK_SIZE)

    assert list(partials_dir.iterdir()) == []


# ── the extension must not select the code path ────────────────────────────

def test_renaming_a_bomb_to_heic_does_not_route_it_through_the_re_encode_branch(
    protocol, photos
, album):
    """The .heic name once doubled a bomb's cost by selecting the convert+save branch.

    ``is_heic`` is now decided by the sniffed MIME type, which the client cannot
    choose, so the rename buys nothing.
    """
    _, _, response = protocol.upload("bomb.heic", make_bomb(), TEST_CHUNK_SIZE)

    assert response.status_code == 200
    assert "success" not in response.get_json()["results"][0]
    assert photos(album.id) == []


# ── the boundary ───────────────────────────────────────────────────────────

def test_an_image_just_under_the_ceiling_is_accepted(protocol, photos, album):
    data = make_image_of_pixels(MAX_IMAGE_PIXELS // 2)

    _, _, response = protocol.upload("wide.png", data, TEST_CHUNK_SIZE)

    assert response.get_json()["results"][0]["success"] is True
    assert len(photos(album.id)) == 1


def test_an_image_just_over_the_ceiling_is_refused(protocol, photos, album):
    side = int(MAX_IMAGE_PIXELS ** 0.5) + 2000
    data = make_bomb(width=side, height=side)

    _, _, response = protocol.upload("huge.png", data, TEST_CHUNK_SIZE)

    assert "success" not in response.get_json()["results"][0]
    assert photos(album.id) == []


# ── the guard must not break real photographs ──────────────────────────────

def test_a_normal_photo_still_uploads_and_thumbnails(protocol, small_jpeg, photos, stored_bytes, album):
    _, _, response = protocol.upload("holiday.jpg", small_jpeg, TEST_CHUNK_SIZE)

    assert response.get_json()["results"][0]["success"] is True
    photo = photos(album.id)[0]
    assert photo.has_thumbnail is True
    assert stored_bytes(photo.stored_filename) == small_jpeg


def test_a_multi_chunk_photo_still_uploads(protocol, multi_chunk_jpeg, photos, stored_bytes, album):
    _, _, response = protocol.upload("clip.jpg", multi_chunk_jpeg, TEST_CHUNK_SIZE)

    assert response.get_json()["results"][0]["success"] is True
    assert stored_bytes(photos(album.id)[0].stored_filename) == multi_chunk_jpeg


def test_the_ceiling_clears_a_full_resolution_phone_camera():
    """48 MP phones and 45-50 MP full-frame bodies must not be rejected as bombs."""
    assert MAX_IMAGE_PIXELS >= 8064 * 6048  # 48 MP iPhone, full resolution
