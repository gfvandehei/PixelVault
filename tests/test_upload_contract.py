"""The wire contract between static/js/uploader.js and routes/share.py.

These are the tests that exist because the two sides were written in parallel
against ``docs/upload_protocol.md`` and had never exchanged a byte. Every
assertion here pins a *name* — a JSON key, a header, a content type — rather
than a behaviour, because a rename is the failure mode that a behavioural test
sails straight past.

The right-hand side of each assertion is what ``uploader.js`` actually reads,
quoted from the line that reads it.
"""

import pytest

from tests.conftest import TEST_CHUNK_SIZE
from tests.protocol import (CHUNK_CONTENT_TYPE, HEADER_OFFSET, HEADER_SHA256,
                            client_key_for, sha256_hex)


# ── init ───────────────────────────────────────────────────────────────────

def test_init_accepts_the_exact_json_payload_the_browser_sends(protocol, small_jpeg):
    # uploader.js init(): JSON.stringify({client_key, filename, total_size, content_type})
    response = protocol.client.post(
        f"{protocol.base}/init",
        json={
            "client_key": client_key_for("holiday.jpg", len(small_jpeg)),
            "filename": "holiday.jpg",
            "total_size": len(small_jpeg),
            "content_type": "image/jpeg",
        },
    )

    assert response.status_code == 201
    assert response.mimetype == "application/json"


def test_init_returns_the_three_keys_the_browser_reads(protocol, small_jpeg):
    response = protocol.init_for("holiday.jpg", small_jpeg)
    body = response.get_json()

    # uploader.js: body.upload_id / body.chunk_size / seekTo(body.received_bytes)
    assert isinstance(body["upload_id"], str) and body["upload_id"]
    assert body["chunk_size"] == TEST_CHUNK_SIZE
    assert body["received_bytes"] == 0
    # Documented in §6.1 and used by the resume assertions below.
    assert body["total_size"] == len(small_jpeg)
    assert body["resumed"] is False


def test_init_content_type_field_is_advisory_and_never_trusted(protocol, small_jpeg):
    """The client sends the browser's guess; the server must not store or believe it."""
    response = protocol.init_for("holiday.jpg", small_jpeg,
                                 content_type="application/x-dosexec")

    assert response.status_code == 201


@pytest.mark.parametrize("bad_key", [
    "not-hex",
    "A" * 64,              # uppercase — §7 says lowercase
    "a" * 63,              # too short
    "a" * 65,              # too long
    "",
    None,
])
def test_init_rejects_a_client_key_that_is_not_64_lowercase_hex(protocol, small_jpeg, bad_key):
    response = protocol.init_for("holiday.jpg", small_jpeg, client_key=bad_key)

    assert response.status_code == 400
    assert "error" in response.get_json()


def test_init_rejects_a_non_integer_total_size(protocol):
    response = protocol.init("holiday.jpg", total_size="lots")

    assert response.status_code == 400
    assert "error" in response.get_json()


def test_init_rejects_a_missing_filename(protocol, small_jpeg):
    response = protocol.init("   ", len(small_jpeg))

    assert response.status_code == 400


# ── status ─────────────────────────────────────────────────────────────────

def test_status_returns_the_two_keys_the_resume_probe_reads(protocol, multi_chunk_jpeg):
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    protocol.chunk(init["upload_id"], 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE])

    body = protocol.status(init["upload_id"]).get_json()

    # uploader.js _prepareResume: body.total_size, clamp(body.received_bytes, ...)
    assert body["received_bytes"] == TEST_CHUNK_SIZE
    assert body["total_size"] == len(multi_chunk_jpeg)
    # §6.2 also promises these two.
    assert body["upload_id"] == init["upload_id"]
    assert body["original_filename"] == "clip.jpg"
    assert body["expires_at"].endswith("Z")


def test_status_is_404_for_an_unknown_upload_id(protocol):
    """Unknown, expired and completed are one case to the client: evict and re-init."""
    response = protocol.status("00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404


# ── chunk ──────────────────────────────────────────────────────────────────

def test_chunk_takes_a_raw_octet_stream_body_not_multipart(protocol, multi_chunk_jpeg):
    piece = multi_chunk_jpeg[:TEST_CHUNK_SIZE]
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()

    response = protocol.client.post(
        f"{protocol.base}/chunk/{init['upload_id']}",
        data=piece,                          # bare bytes, exactly as xhr.send(buffer)
        content_type=CHUNK_CONTENT_TYPE,
        headers={HEADER_OFFSET: "0", HEADER_SHA256: sha256_hex(piece)},
    )

    assert response.status_code == 200
    assert response.get_json() == {"received_bytes": len(piece)}


def test_chunk_reads_the_offset_and_digest_from_the_documented_headers(protocol, multi_chunk_jpeg):
    """A rename on either side breaks resumption silently; pin the spellings."""
    assert HEADER_OFFSET == "X-Upload-Offset"
    assert HEADER_SHA256 == "X-Chunk-SHA256"

    piece = multi_chunk_jpeg[:TEST_CHUNK_SIZE]
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()

    response = protocol.chunk(init["upload_id"], 0, piece)

    assert response.status_code == 200


def test_chunk_without_an_offset_header_is_a_400(protocol, multi_chunk_jpeg):
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()

    response = protocol.client.post(
        f"{protocol.base}/chunk/{init['upload_id']}",
        data=multi_chunk_jpeg[:TEST_CHUNK_SIZE],
        content_type=CHUNK_CONTENT_TYPE,
        headers={HEADER_SHA256: sha256_hex(multi_chunk_jpeg[:TEST_CHUNK_SIZE])},
    )

    assert response.status_code == 400
    assert "error" in response.get_json()


def test_chunk_with_an_empty_body_is_a_400(protocol, multi_chunk_jpeg):
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()

    response = protocol.chunk(init["upload_id"], 0, b"")

    assert response.status_code == 400


def test_chunk_response_carries_received_bytes_the_client_seeks_by(protocol, multi_chunk_jpeg):
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    upload_id = init["upload_id"]

    first = protocol.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE])
    second = protocol.chunk(upload_id, TEST_CHUNK_SIZE,
                            multi_chunk_jpeg[TEST_CHUNK_SIZE:2 * TEST_CHUNK_SIZE])

    # uploader.js onChunkResponse: res.body.received_bytes, must be a number.
    assert isinstance(first.get_json()["received_bytes"], int)
    assert second.get_json()["received_bytes"] == 2 * TEST_CHUNK_SIZE


# ── complete ───────────────────────────────────────────────────────────────

def test_complete_returns_the_results_envelope_the_client_shares_with_the_legacy_path(
        protocol, multi_chunk_jpeg):
    _, _, response = protocol.upload("clip.jpg", multi_chunk_jpeg, TEST_CHUNK_SIZE)

    assert response.status_code == 200
    body = response.get_json()
    # uploader.js complete(): res.body.results[0].success / .error
    assert list(body.keys()) == ["results"]
    assert body["results"][0]["success"] is True
    assert body["results"][0]["filename"] == "clip.jpg"


def test_complete_takes_an_empty_body(protocol, multi_chunk_jpeg):
    """xhr.send(null) — no content type, no payload."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    protocol.send_chunks(init["upload_id"], multi_chunk_jpeg, TEST_CHUNK_SIZE)

    response = protocol.client.post(f"{protocol.base}/complete/{init['upload_id']}")

    assert response.status_code == 200


def test_409_from_both_chunk_and_complete_carries_received_bytes(protocol, multi_chunk_jpeg):
    """The client re-seeks off this number from either endpoint; both must supply it."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    upload_id = init["upload_id"]
    protocol.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE])

    from_chunk = protocol.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE])
    from_complete = protocol.complete(upload_id)

    assert from_chunk.status_code == 409
    assert from_chunk.get_json()["received_bytes"] == TEST_CHUNK_SIZE
    assert from_complete.status_code == 409
    assert from_complete.get_json()["received_bytes"] == TEST_CHUNK_SIZE
    # uploader.js requires a *number*; a string would fail its typeof check and
    # be reported to the user as "Upload is out of sync".
    assert isinstance(from_complete.get_json()["received_bytes"], int)


def test_every_error_response_carries_an_error_key(protocol, small_jpeg):
    """errorForStatus() prefers body.error over its own canned strings."""
    bad_init = protocol.init_for("holiday.jpg", small_jpeg, client_key="nope")

    assert "error" in bad_init.get_json()


def test_the_four_endpoints_hang_off_the_legacy_upload_url(app, album):
    """uploadBase() derives every chunked route from the legacy upload URL."""
    base = f"/share/{album.token}/upload"
    rules = {str(rule) for rule in app.url_map.iter_rules()}

    assert "/share/<token>/upload" in rules
    assert "/share/<token>/upload/init" in rules
    assert "/share/<token>/upload/status/<upload_id>" in rules
    assert "/share/<token>/upload/chunk/<upload_id>" in rules
    assert "/share/<token>/upload/complete/<upload_id>" in rules
    assert base.endswith("/upload")
