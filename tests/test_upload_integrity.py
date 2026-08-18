"""The rules that keep a partial file honest: offset, checksum, and truncate-back.

Everything here is about the one invariant §9 rests on — ``received_bytes`` counts
bytes that are known-good and durable, and never counts more than the ``.part``
file actually holds. A chunk that fails any check must leave the cursor and the
file exactly where it found them, because the client's next move is computed from
that cursor.

The crash-safety tests at the bottom reach past the wire and write to the ``.part``
file directly. That is deliberate: the states they simulate — a chunk half-landed
before the socket died — cannot be produced through the protocol, and they are
precisely the states the design claims to heal.
"""

import pytest

from tests.conftest import TEST_CHUNK_SIZE
from tests.protocol import HEADER_SHA256, sha256_hex


def _partial(partials_dir, upload_id):
    """The on-disk ``.part`` file backing a session."""
    return partials_dir / f"{upload_id}.part"


def _started(protocol, data, chunks_to_send=2):
    """Init and send a few chunks, returning ``(upload_id, cursor)``."""
    init = protocol.init_for("clip.jpg", data).get_json()
    stop = chunks_to_send * TEST_CHUNK_SIZE
    protocol.send_chunks(init["upload_id"], data, TEST_CHUNK_SIZE, stop=stop)
    return init["upload_id"], stop


# ── Offset mismatch (409) ──────────────────────────────────────────────────

def test_a_chunk_behind_the_cursor_is_409_with_the_true_cursor(protocol, multi_chunk_jpeg):
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)

    response = protocol.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE])

    assert response.status_code == 409
    assert response.get_json()["received_bytes"] == cursor


def test_a_chunk_ahead_of_the_cursor_is_409_rather_than_leaving_a_hole(
        protocol, multi_chunk_jpeg):
    """Accepting a forward jump would leave undefined bytes in the middle of the file."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    ahead = cursor + TEST_CHUNK_SIZE

    response = protocol.chunk(upload_id, ahead,
                              multi_chunk_jpeg[ahead:ahead + TEST_CHUNK_SIZE])

    assert response.status_code == 409
    assert response.get_json()["received_bytes"] == cursor


def test_a_replayed_chunk_does_not_double_append(protocol, multi_chunk_jpeg,
                                                 session_row, partials_dir):
    """The retry a flaky network produces must be a no-op, not an insertion."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    before = _partial(partials_dir, upload_id).stat().st_size

    replay = protocol.chunk(upload_id, cursor - TEST_CHUNK_SIZE,
                            multi_chunk_jpeg[cursor - TEST_CHUNK_SIZE:cursor])

    assert replay.status_code == 409
    assert session_row(upload_id).received_bytes == cursor
    assert _partial(partials_dir, upload_id).stat().st_size == before == cursor


def test_the_upload_completes_correctly_after_an_offset_mismatch(
        protocol, multi_chunk_jpeg, album, photos, stored_bytes):
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    protocol.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE])  # the 409

    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE, start=cursor)
    complete = protocol.complete(upload_id)

    assert complete.get_json()["results"][0]["success"] is True
    assert stored_bytes(photos(album.id)[0].stored_filename) == multi_chunk_jpeg


def test_completing_a_short_upload_is_the_same_409_a_chunk_would_give(
        protocol, multi_chunk_jpeg, session_row):
    """One answer for "you are not where you think you are", from either endpoint."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)

    response = protocol.complete(upload_id)

    assert response.status_code == 409
    assert response.get_json()["received_bytes"] == cursor
    # A refused complete must not consume the session; the client resumes into it.
    assert session_row(upload_id) is not None


# ── Checksum mismatch (422) ────────────────────────────────────────────────

def test_a_chunk_whose_digest_does_not_match_is_422_and_writes_nothing(
        protocol, multi_chunk_jpeg, session_row, partials_dir):
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    piece = multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE]

    response = protocol.chunk(upload_id, cursor, piece, digest="0" * 64)

    assert response.status_code == 422
    assert session_row(upload_id).received_bytes == cursor
    assert _partial(partials_dir, upload_id).stat().st_size == cursor


def test_corrupt_bytes_under_an_honest_digest_are_also_422(protocol, multi_chunk_jpeg,
                                                           partials_dir):
    """The digest attests to the bytes; flipping either side must fail the same way."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    piece = multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE]
    corrupted = bytes([piece[0] ^ 0xFF]) + piece[1:]

    response = protocol.chunk(upload_id, cursor, corrupted, digest=sha256_hex(piece))

    assert response.status_code == 422
    assert _partial(partials_dir, upload_id).stat().st_size == cursor


def test_the_same_chunk_resent_correctly_after_a_422_is_accepted(protocol, multi_chunk_jpeg):
    """§8 tells the client to retry the same chunk; the cursor must still admit it."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    piece = multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE]
    protocol.chunk(upload_id, cursor, piece, digest="0" * 64)

    retry = protocol.chunk(upload_id, cursor, piece)

    assert retry.status_code == 200
    assert retry.get_json()["received_bytes"] == cursor + len(piece)


def test_the_upload_completes_byte_identically_after_a_checksum_mismatch(
        protocol, multi_chunk_jpeg, album, photos, stored_bytes):
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    protocol.chunk(upload_id, cursor, multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE],
                   digest="f" * 64)

    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE, start=cursor)
    protocol.complete(upload_id)

    assert stored_bytes(photos(album.id)[0].stored_filename) == multi_chunk_jpeg


def test_an_uppercase_digest_is_accepted(protocol, multi_chunk_jpeg):
    """§7 asks for lowercase, but a case difference is not a corrupt payload."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    piece = multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE]

    response = protocol.chunk(upload_id, cursor, piece, digest=sha256_hex(piece).upper())

    assert response.status_code == 200


@pytest.mark.xfail(
    reason="BUG: uploads.append_chunk skips verification entirely when "
           "X-Chunk-SHA256 is absent (`if chunk_sha256 is not None`), so a client "
           "that simply omits the header writes unverified bytes AND dodges the "
           "422 rate-limit charge. §6.3 lists the header as part of every chunk "
           "request and §9's flowchart puts the digest check on the only path to "
           "the append.",
    strict=True,
)
def test_a_chunk_with_no_digest_header_is_refused(protocol, multi_chunk_jpeg,
                                                  session_row):
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    piece = multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE]

    response = protocol.client.post(
        f"{protocol.base}/chunk/{upload_id}",
        data=piece,
        content_type="application/octet-stream",
        headers={"X-Upload-Offset": str(cursor)},   # no X-Chunk-SHA256
    )

    assert response.status_code == 400
    assert session_row(upload_id).received_bytes == cursor


def test_a_malformed_digest_header_does_not_pass_as_no_digest_at_all(
        protocol, multi_chunk_jpeg, partials_dir):
    """A truncated or garbled header must fail closed, not fall through to "unchecked"."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    piece = multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE]

    response = protocol.chunk(upload_id, cursor, piece, headers={HEADER_SHA256: "abc"})

    assert response.status_code == 422
    assert _partial(partials_dir, upload_id).stat().st_size == cursor


# ── Crash safety (§9) ──────────────────────────────────────────────────────

def test_bytes_past_the_cursor_are_truncated_away_by_the_next_chunk(
        protocol, multi_chunk_jpeg, partials_dir, album, photos, stored_bytes):
    """The self-healing direction: a chunk that half-landed before the socket died.

    The bytes are on disk but the commit that would have counted them never ran, so
    the cursor sits behind the file. The next append truncates back to the cursor
    and overwrites them — no repair pass, no duplicated slice.
    """
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    path = _partial(partials_dir, upload_id)
    with open(path, "ab") as fh:
        fh.write(b"\xde\xad\xbe\xef" * 512)          # a chunk that half-landed
    assert path.stat().st_size > cursor

    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE, start=cursor)
    complete = protocol.complete(upload_id)

    assert complete.get_json()["results"][0]["success"] is True
    assert stored_bytes(photos(album.id)[0].stored_filename) == multi_chunk_jpeg


def test_a_partial_longer_than_the_whole_file_is_still_healed(
        protocol, multi_chunk_jpeg, partials_dir, album, photos, stored_bytes):
    """The excess can exceed what is left to send; the truncate is absolute, not relative."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    path = _partial(partials_dir, upload_id)
    with open(path, "ab") as fh:
        fh.write(b"\x00" * (2 * len(multi_chunk_jpeg)))

    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE, start=cursor)
    protocol.complete(upload_id)

    assert stored_bytes(photos(album.id)[0].stored_filename) == multi_chunk_jpeg


def test_healing_leaves_the_partial_exactly_as_long_as_the_cursor_claims(
        protocol, multi_chunk_jpeg, partials_dir, session_row):
    """Length and counter converge on the first append, not eventually."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    path = _partial(partials_dir, upload_id)
    with open(path, "ab") as fh:
        fh.write(b"junk" * 100)

    protocol.chunk(upload_id, cursor, multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE])

    assert path.stat().st_size == session_row(upload_id).received_bytes
    assert path.read_bytes() == multi_chunk_jpeg[:cursor + TEST_CHUNK_SIZE]


def test_a_counter_ahead_of_the_file_is_not_self_healing(
        protocol, multi_chunk_jpeg, partials_dir):
    """The other direction, which §9 calls irrecoverable — pinned so it stays a
    known property rather than becoming a surprise.

    If the counter ever ran ahead of the durable bytes, ``truncate`` would *extend*
    the file with NULs instead of trimming it, and the gap would be committed
    silently: the length still matches ``total_size``, so ``complete`` sees nothing
    wrong. This is exactly why the fsync in ``append_chunk`` precedes the DB commit.
    """
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    path = _partial(partials_dir, upload_id)
    with open(path, "r+b") as fh:
        fh.truncate(cursor - 4096)                   # bytes lost; the counter did not move

    protocol.chunk(upload_id, cursor, multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE])

    written = path.read_bytes()
    assert len(written) == cursor + TEST_CHUNK_SIZE   # length still looks right
    assert written != multi_chunk_jpeg[:cursor + TEST_CHUNK_SIZE]
    assert written[cursor - 4096:cursor] == b"\x00" * 4096   # the hole, silently


def test_a_missing_partial_file_is_a_404_not_a_silent_restart(
        protocol, multi_chunk_jpeg, partials_dir):
    """Losing the file must evict the session, never resume from a fresh zero."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    _partial(partials_dir, upload_id).unlink()

    response = protocol.chunk(upload_id, cursor,
                              multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE])

    assert response.status_code == 404
    assert "error" in response.get_json()


def test_reinit_after_the_partial_vanished_opens_a_new_session_at_zero(
        protocol, multi_chunk_jpeg, partials_dir):
    upload_id, _ = _started(protocol, multi_chunk_jpeg)
    _partial(partials_dir, upload_id).unlink()

    response = protocol.init_for("clip.jpg", multi_chunk_jpeg)

    assert response.status_code == 201
    assert response.get_json()["resumed"] is False
    assert response.get_json()["received_bytes"] == 0
    assert response.get_json()["upload_id"] != upload_id


def test_completing_with_the_partial_missing_is_a_404(protocol, small_jpeg, partials_dir):
    init = protocol.init_for("holiday.jpg", small_jpeg).get_json()
    protocol.chunk(init["upload_id"], 0, small_jpeg)
    _partial(partials_dir, init["upload_id"]).unlink()

    response = protocol.complete(init["upload_id"])

    assert response.status_code == 404
