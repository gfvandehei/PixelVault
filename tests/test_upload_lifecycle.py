"""What a chunked upload actually does, from init to a committed Photo.

The contract suite pins the *names* on the wire. This module pins the *effects*:
that the bytes that come out the far end are the bytes that went in, that a
transfer interrupted halfway picks up where it stopped instead of starting over,
and that the single-request path this feature was bolted alongside still works.

Resumption is the headline of #29, so it gets the most tests here — including the
two cases a naive implementation gets wrong: resuming a session that has received
nothing at all, and resuming the same session twice.
"""

import io

from tests.conftest import TEST_CHUNK_SIZE
from tests.protocol import client_key_for


# ── The happy path ─────────────────────────────────────────────────────────

def test_a_chunked_upload_commits_the_exact_bytes_that_were_sent(
        protocol, multi_chunk_jpeg, album, photos, stored_bytes):
    init, chunks, complete = protocol.upload("clip.jpg", multi_chunk_jpeg, TEST_CHUNK_SIZE)

    assert init.status_code == 201
    assert [c.status_code for c in chunks] == [200] * len(chunks)
    assert complete.get_json()["results"][0]["success"] is True

    stored = photos(album.id)
    assert len(stored) == 1
    assert stored_bytes(stored[0].stored_filename) == multi_chunk_jpeg


def test_the_committed_photo_row_describes_the_file_that_landed(
        protocol, multi_chunk_jpeg, album, user, photos):
    protocol.upload("clip.jpg", multi_chunk_jpeg, TEST_CHUNK_SIZE)

    photo = photos(album.id)[0]

    assert photo.original_filename == "clip.jpg"
    assert photo.file_size == len(multi_chunk_jpeg)
    assert photo.mime_type == "image/jpeg"
    assert photo.uploader_id == user.id
    assert photo.uploader_name == user.username


def test_completing_generates_a_thumbnail_beside_the_stored_file(
        protocol, multi_chunk_jpeg, album, photos, upload_dir):
    protocol.upload("clip.jpg", multi_chunk_jpeg, TEST_CHUNK_SIZE)

    photo = photos(album.id)[0]

    assert photo.has_thumbnail is True
    assert (upload_dir / f"thumb_{photo.stored_filename}").exists()


def test_completing_drops_the_session_row_and_leaves_no_partial_behind(
        protocol, multi_chunk_jpeg, session_row, partials_dir):
    init, _, _ = protocol.upload("clip.jpg", multi_chunk_jpeg, TEST_CHUNK_SIZE)
    upload_id = init.get_json()["upload_id"]

    assert session_row(upload_id) is None
    assert list(partials_dir.glob("*")) == []


def test_a_file_that_fits_in_one_chunk_still_goes_through_the_whole_dance(
        protocol, small_jpeg, album, photos, stored_bytes):
    """The chunked path must not require more than one chunk to be correct."""
    _, chunks, complete = protocol.upload("holiday.jpg", small_jpeg, TEST_CHUNK_SIZE)

    assert len(chunks) == 1
    assert complete.status_code == 200
    assert stored_bytes(photos(album.id)[0].stored_filename) == small_jpeg


def test_a_short_final_chunk_is_accepted(protocol, multi_chunk_jpeg, session_row):
    """The last slice is almost never a full chunk; nothing may pad or reject it."""
    assert len(multi_chunk_jpeg) % TEST_CHUNK_SIZE != 0, "fixture must have a ragged tail"

    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    responses = protocol.send_chunks(init["upload_id"], multi_chunk_jpeg, TEST_CHUNK_SIZE)

    assert responses[-1].status_code == 200
    assert responses[-1].get_json()["received_bytes"] == len(multi_chunk_jpeg)


# ── Resumption ─────────────────────────────────────────────────────────────

def _half_uploaded(protocol, data, chunks_to_send=3):
    """Init a session, send the first few chunks, and return ``(upload_id, offset)``."""
    init = protocol.init_for("clip.jpg", data).get_json()
    stop = chunks_to_send * TEST_CHUNK_SIZE
    protocol.send_chunks(init["upload_id"], data, TEST_CHUNK_SIZE, stop=stop)
    return init["upload_id"], stop


def test_reinit_with_the_same_client_key_returns_the_session_already_in_flight(
        protocol, multi_chunk_jpeg):
    upload_id, offset = _half_uploaded(protocol, multi_chunk_jpeg)

    resumed = protocol.init_for("clip.jpg", multi_chunk_jpeg)
    body = resumed.get_json()

    # 200, not 201: nothing was created. §6.1.
    assert resumed.status_code == 200
    assert body["resumed"] is True
    assert body["upload_id"] == upload_id
    assert body["received_bytes"] == offset


def test_a_resumed_upload_finishes_byte_identical_to_the_original(
        protocol, multi_chunk_jpeg, album, photos, stored_bytes):
    """The whole point of #29: an interrupted transfer must not corrupt the file."""
    upload_id, _ = _half_uploaded(protocol, multi_chunk_jpeg)

    body = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE,
                         start=body["received_bytes"])
    complete = protocol.complete(upload_id)

    assert complete.get_json()["results"][0]["success"] is True
    assert stored_bytes(photos(album.id)[0].stored_filename) == multi_chunk_jpeg


def test_resuming_twice_still_yields_one_session_and_one_file(
        protocol, multi_chunk_jpeg, album, photos, stored_bytes, partials_dir):
    """Two interruptions are not a special case; they are the same case twice."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    upload_id = init["upload_id"]

    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE,
                         stop=2 * TEST_CHUNK_SIZE)
    first_resume = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE,
                         start=first_resume["received_bytes"], stop=5 * TEST_CHUNK_SIZE)
    second_resume = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    assert len(list(partials_dir.glob("*.part"))) == 1

    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE,
                         start=second_resume["received_bytes"])
    protocol.complete(upload_id)

    assert first_resume["upload_id"] == upload_id
    assert second_resume["upload_id"] == upload_id
    assert second_resume["received_bytes"] == 5 * TEST_CHUNK_SIZE
    assert len(photos(album.id)) == 1
    assert stored_bytes(photos(album.id)[0].stored_filename) == multi_chunk_jpeg


def test_reinit_before_any_chunk_lands_resumes_at_zero_rather_than_opening_a_second_session(
        protocol, multi_chunk_jpeg, partials_dir):
    """A reload between init and the first chunk must not orphan a partial."""
    first = protocol.init_for("clip.jpg", multi_chunk_jpeg)

    second = protocol.init_for("clip.jpg", multi_chunk_jpeg)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.get_json()["resumed"] is True
    assert second.get_json()["upload_id"] == first.get_json()["upload_id"]
    assert second.get_json()["received_bytes"] == 0
    assert len(list(partials_dir.glob("*.part"))) == 1


def test_a_zero_chunk_session_resumed_from_scratch_still_commits_the_whole_file(
        protocol, multi_chunk_jpeg, album, photos, stored_bytes):
    protocol.init_for("clip.jpg", multi_chunk_jpeg)
    body = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()

    protocol.send_chunks(body["upload_id"], multi_chunk_jpeg, TEST_CHUNK_SIZE,
                         start=body["received_bytes"])
    protocol.complete(body["upload_id"])

    assert stored_bytes(photos(album.id)[0].stored_filename) == multi_chunk_jpeg


def test_the_status_probe_reports_the_same_cursor_init_would_hand_back(
        protocol, multi_chunk_jpeg):
    """uploader.js probes status before re-initing; the two must never disagree."""
    upload_id, offset = _half_uploaded(protocol, multi_chunk_jpeg)

    from_status = protocol.status(upload_id).get_json()
    from_init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()

    assert from_status["received_bytes"] == offset
    assert from_init["received_bytes"] == offset


def test_a_client_key_reused_for_a_different_size_starts_a_fresh_session(
        protocol, multi_chunk_jpeg, partials_dir):
    """A stale row is replaced, not resumed into — its offset means nothing now."""
    key = client_key_for("clip.jpg", len(multi_chunk_jpeg))
    upload_id, _ = _half_uploaded(protocol, multi_chunk_jpeg)

    replacement = protocol.init("clip.jpg", len(multi_chunk_jpeg) + 1, client_key=key)

    assert replacement.status_code == 201
    assert replacement.get_json()["resumed"] is False
    assert replacement.get_json()["upload_id"] != upload_id
    assert replacement.get_json()["received_bytes"] == 0
    # The superseded partial went with the row it belonged to.
    assert [p.name for p in partials_dir.glob("*.part")] == [
        f"{replacement.get_json()['upload_id']}.part"
    ]


def test_two_different_files_get_two_independent_sessions(protocol, multi_chunk_jpeg,
                                                          small_jpeg, partials_dir):
    big = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    small = protocol.init_for("holiday.jpg", small_jpeg).get_json()

    assert big["upload_id"] != small["upload_id"]
    assert len(list(partials_dir.glob("*.part"))) == 2


def test_the_same_file_uploaded_twice_in_a_row_produces_two_photos(
        protocol, multi_chunk_jpeg, album, photos):
    """The client key is an in-flight idempotency token, not a deduplication key."""
    protocol.upload("clip.jpg", multi_chunk_jpeg, TEST_CHUNK_SIZE)
    protocol.upload("clip.jpg", multi_chunk_jpeg, TEST_CHUNK_SIZE)

    assert len(photos(album.id)) == 2


# ── The legacy single-request path ─────────────────────────────────────────

def _legacy_post(client, token, filename, data):
    """POST the multipart form the non-chunked uploader sends."""
    return client.post(
        f"/share/{token}/upload",
        data={"files": (io.BytesIO(data), filename)},
        content_type="multipart/form-data",
    )


def test_the_single_request_path_still_stores_a_photo(client, album, small_jpeg,
                                                      photos, stored_bytes):
    response = _legacy_post(client, album.token, "holiday.jpg", small_jpeg)

    assert response.status_code == 200
    assert response.get_json()["results"][0]["success"] is True
    assert stored_bytes(photos(album.id)[0].stored_filename) == small_jpeg


def test_a_disallowed_type_rides_inside_results_at_http_200(client, album, not_an_image,
                                                            photos):
    """uploader.js shares one response handler with the chunked path; per-file
    failures are data, not a status code."""
    response = _legacy_post(client, album.token, "holiday.jpg", not_an_image)

    assert response.status_code == 200
    result = response.get_json()["results"][0]
    assert "success" not in result
    assert "not allowed" in result["error"]
    assert photos(album.id) == []


def test_the_chunked_path_refuses_the_same_types_the_legacy_path_does(
        protocol, not_an_image, album, photos):
    """One allow-list, two transports — a type refused by one cannot slip past the other."""
    init = protocol.init_for("holiday.jpg", not_an_image).get_json()

    chunk = protocol.chunk(init["upload_id"], 0, not_an_image)

    assert chunk.status_code == 400
    assert "not allowed" in chunk.get_json()["error"]
    assert photos(album.id) == []


def test_the_legacy_path_still_refuses_an_album_with_uploads_disabled(
        client, album, small_jpeg, set_allow_upload, photos):
    set_allow_upload(album.id, False)

    response = _legacy_post(client, album.token, "holiday.jpg", small_jpeg)

    assert response.status_code == 403
    assert photos(album.id) == []


def test_both_paths_store_identical_bytes_for_the_same_file(
        client, protocol, album, small_jpeg, photos, stored_bytes):
    _legacy_post(client, album.token, "holiday.jpg", small_jpeg)
    protocol.upload("holiday.jpg", small_jpeg, TEST_CHUNK_SIZE)

    stored = photos(album.id)
    assert len(stored) == 2
    assert {stored_bytes(p.stored_filename) for p in stored} == {small_jpeg}
