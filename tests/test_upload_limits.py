"""The two ceilings on a chunked upload: the rate limiter and the per-user quotas.

They protect different things and are easy to confuse. The limiter is per worker
process, in memory, and resets on deploy — it bounds *request rate*. The quotas
live in the database and bound *bytes and sessions on disk*. Chunking is what
forced them apart: once one file is sixty requests, request count stopped
correlating with cost, so the limiter had to stop charging for the traffic the
feature exists to produce.

The asymmetry in ``upload_chunk`` — ``deduct_when=lambda r: r.status_code == 422``
— is the piece most likely to regress silently, because charging for 409s breaks
nothing that a happy-path test would notice. It only breaks resumption, and only
for the clients that need it most. Hence the flood tests below.

Sizes come from ``conftest``: an 8 MB request ceiling over a 4 MB in-flight quota
over 3 concurrent sessions, chosen so each limit can be tripped without tripping
the others. The limiter's counters are reset between tests by ``reset_state``.
"""

import pytest

from tests.conftest import (TEST_CHUNK_SIZE, TEST_MAX_INFLIGHT_MB,
                            TEST_MAX_SESSIONS, TEST_MAX_UPLOAD_MB)
from tests.protocol import client_key_for

MB = 1024 * 1024

#: The chunk endpoint's own limit. Floods below must comfortably exceed it.
CHUNK_LIMIT_PER_HOUR = 60


# ── Limiter asymmetry ──────────────────────────────────────────────────────

def test_a_flood_of_offset_mismatches_is_never_throttled(protocol, multi_chunk_jpeg):
    """409 is how a resuming client finds its place; charging for it would throttle
    resumption itself. Four times the endpoint's hourly limit, all refused the same way."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    piece = multi_chunk_jpeg[:TEST_CHUNK_SIZE]

    statuses = {
        protocol.chunk(init["upload_id"], 9_999, piece).status_code
        for _ in range(4 * CHUNK_LIMIT_PER_HOUR)
    }

    assert statuses == {409}


def test_a_flood_of_offset_mismatches_leaves_the_upload_usable(protocol, multi_chunk_jpeg,
                                                               album, photos, stored_bytes):
    """Not throttled *and* not poisoned: the session survives the flood intact."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    upload_id = init["upload_id"]
    for _ in range(2 * CHUNK_LIMIT_PER_HOUR):
        protocol.chunk(upload_id, 9_999, multi_chunk_jpeg[:TEST_CHUNK_SIZE])

    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE)
    protocol.complete(upload_id)

    assert stored_bytes(photos(album.id)[0].stored_filename) == multi_chunk_jpeg


def test_a_full_upload_is_far_more_requests_than_the_chunk_limit_allows(
        protocol, multi_chunk_jpeg, album, photos):
    """The limit is 60/hour and a real file is more chunks than that, so an
    uncharged success path is not a nicety — it is what makes the endpoint work."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    upload_id = init["upload_id"]

    # Walk the file in 1 KiB slices: same bytes, many more requests than the limit.
    responses = protocol.send_chunks(upload_id, multi_chunk_jpeg, 1024,
                                     stop=(CHUNK_LIMIT_PER_HOUR + 20) * 1024)

    assert len(responses) > CHUNK_LIMIT_PER_HOUR
    assert {r.status_code for r in responses} == {200}


def test_repeated_checksum_mismatches_are_eventually_throttled(protocol, multi_chunk_jpeg):
    """A 422 is the one chunk failure that costs the server real work for nothing —
    it read and hashed the body. Replaying a bad digest must not stay free."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    piece = multi_chunk_jpeg[:TEST_CHUNK_SIZE]

    statuses = [protocol.chunk(init["upload_id"], 0, piece, digest="0" * 64).status_code
                for _ in range(CHUNK_LIMIT_PER_HOUR + 5)]

    assert statuses[0] == 422
    assert 429 in statuses
    assert statuses.index(429) == CHUNK_LIMIT_PER_HOUR
    assert set(statuses[CHUNK_LIMIT_PER_HOUR:]) == {429}


def test_throttling_earned_by_422s_also_stops_the_good_chunks(protocol, multi_chunk_jpeg):
    """The limiter guards the endpoint, not a status code — once the bucket is empty
    a well-formed chunk waits too. Pinned so the cost of a bad client is understood."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    upload_id = init["upload_id"]
    for _ in range(CHUNK_LIMIT_PER_HOUR):
        protocol.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE], digest="0" * 64)

    honest = protocol.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE])

    assert honest.status_code == 429


def test_offset_mismatches_do_not_count_towards_the_checksum_budget(protocol,
                                                                    multi_chunk_jpeg):
    """The two floods must not share a counter, or a resuming client would arrive at
    the endpoint with its budget already spent."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    upload_id = init["upload_id"]
    for _ in range(2 * CHUNK_LIMIT_PER_HOUR):
        protocol.chunk(upload_id, 9_999, multi_chunk_jpeg[:TEST_CHUNK_SIZE])

    after = protocol.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE],
                           digest="0" * 64)

    assert after.status_code == 422   # still budget left; the 409s cost nothing


# ── Size ceilings ──────────────────────────────────────────────────────────

def test_init_refuses_a_declared_size_above_the_request_ceiling(protocol):
    """MAX_CONTENT_LENGTH stopped bounding an upload the moment uploads became many
    requests, so ``init`` has to re-impose it on the declared total."""
    response = protocol.init("huge.jpg", TEST_MAX_UPLOAD_MB * MB + 1)

    assert response.status_code == 413
    assert str(TEST_MAX_UPLOAD_MB) in response.get_json()["error"]


def test_init_accepts_a_declared_size_exactly_at_the_ceiling(protocol, monkeypatch):
    """Off-by-one at the boundary would reject the largest legal file.

    The in-flight byte quota deliberately sits *below* the request ceiling (see
    conftest), so it would fire first and mask the boundary. Lifting it means
    rebinding ``check_user_quota``'s default arguments — the quota constants were
    captured there when the ``def`` executed, so the module attribute is not what
    the function reads.
    """
    import pixelvault.uploads as uploads_module
    monkeypatch.setattr(uploads_module.check_user_quota, "__defaults__",
                        (TEST_MAX_SESSIONS, TEST_MAX_UPLOAD_MB * MB))

    response = protocol.init("exact.jpg", TEST_MAX_UPLOAD_MB * MB)

    assert response.status_code == 201


def test_a_chunk_that_would_overrun_the_declared_total_is_413(protocol, multi_chunk_jpeg,
                                                              session_row, partials_dir):
    """Understating ``total_size`` at init must not become a way to stream unbounded
    bytes to disk one legal-sized chunk at a time."""
    init = protocol.init("clip.jpg", 1024).get_json()
    upload_id = init["upload_id"]

    response = protocol.chunk(upload_id, 0, multi_chunk_jpeg[:4096])

    assert response.status_code == 413
    assert session_row(upload_id).received_bytes == 0
    assert (partials_dir / f"{upload_id}.part").stat().st_size == 0


def test_an_overrunning_chunk_is_refused_even_partway_through(protocol, multi_chunk_jpeg,
                                                              session_row):
    """The check is against what is left, not against the chunk in isolation."""
    declared = 3 * TEST_CHUNK_SIZE + 100
    init = protocol.init("clip.jpg", declared).get_json()
    upload_id = init["upload_id"]
    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE,
                         stop=3 * TEST_CHUNK_SIZE)

    response = protocol.chunk(upload_id, 3 * TEST_CHUNK_SIZE,
                              multi_chunk_jpeg[3 * TEST_CHUNK_SIZE:4 * TEST_CHUNK_SIZE])

    assert response.status_code == 413
    assert session_row(upload_id).received_bytes == 3 * TEST_CHUNK_SIZE


def test_a_chunk_body_far_larger_than_a_chunk_is_refused(protocol, multi_chunk_jpeg):
    """The endpoint re-caps the body to roughly one chunk; the global 500 MB cap would
    let a single lying Content-Length undo the point of slicing the file up."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()

    response = protocol.chunk(init["upload_id"], 0, multi_chunk_jpeg)

    assert response.status_code == 413


# ── Per-user quotas ────────────────────────────────────────────────────────

def _open_sessions(protocol, count, size=4096):
    """Open ``count`` distinct sessions, returning the responses."""
    return [protocol.init(f"file{i}.jpg", size + i) for i in range(count)]


def test_a_user_may_not_hold_more_open_sessions_than_the_quota_allows(protocol):
    opened = _open_sessions(protocol, TEST_MAX_SESSIONS)
    assert [r.status_code for r in opened] == [201] * TEST_MAX_SESSIONS

    refused = protocol.init("one-too-many.jpg", 4096)

    assert refused.status_code == 429
    assert "Too many uploads in progress" in refused.get_json()["error"]


def test_the_session_quota_429_is_json_the_client_can_read(protocol):
    """errorForStatus() prefers body.error, and "too many in progress" is a different
    instruction to the user than "you are going too fast"."""
    _open_sessions(protocol, TEST_MAX_SESSIONS)

    refused = protocol.init("one-too-many.jpg", 4096)

    assert refused.mimetype == "application/json"
    assert "error" in refused.get_json()


def test_resuming_an_existing_session_does_not_consume_another_slot(protocol):
    """Otherwise a client at its quota could never resume anything."""
    _open_sessions(protocol, TEST_MAX_SESSIONS)

    resumed = protocol.init("file0.jpg", 4096)

    assert resumed.status_code == 200
    assert resumed.get_json()["resumed"] is True


def test_completing_a_session_frees_its_slot(protocol, small_jpeg):
    protocol.init_for("holiday.jpg", small_jpeg)
    _open_sessions(protocol, TEST_MAX_SESSIONS - 1)
    assert protocol.init("blocked.jpg", 4096).status_code == 429

    protocol.upload("holiday.jpg", small_jpeg, TEST_CHUNK_SIZE)

    assert protocol.init("now-allowed.jpg", 4096).status_code == 201


def test_discarding_a_stale_session_frees_its_slot(protocol, multi_chunk_jpeg):
    """Replacing a session must not leave the old one counted against the quota."""
    key = client_key_for("file0.jpg", 4096)
    _open_sessions(protocol, TEST_MAX_SESSIONS)

    # Same client key, different declared size: the old row is discarded, not resumed.
    replaced = protocol.init("file0.jpg", 8192, client_key=key)

    assert replaced.status_code == 201
    assert replaced.get_json()["resumed"] is False


def test_a_user_may_not_declare_more_bytes_in_flight_than_the_quota_allows(protocol):
    """Declared totals, not landed bytes: a session that has received one chunk still
    intends to reach its declared size, and the disk has to hold all of it."""
    three_mb = 3 * MB
    assert three_mb < TEST_MAX_UPLOAD_MB * MB, "must clear the per-request ceiling"

    first = protocol.init("big-a.jpg", three_mb)
    second = protocol.init("big-b.jpg", three_mb)

    assert first.status_code == 201
    assert second.status_code == 429
    assert f"{TEST_MAX_INFLIGHT_MB} MB" in second.get_json()["error"]


def test_the_byte_quota_and_the_session_quota_are_told_apart_by_their_message(protocol):
    """Two different 429s with two different remedies; the client shows the text."""
    _open_sessions(protocol, TEST_MAX_SESSIONS)
    by_count = protocol.init("extra.jpg", 4096)

    assert "Too many uploads in progress" in by_count.get_json()["error"]
    assert "in-flight limit" not in by_count.get_json()["error"]


def test_the_byte_quota_counts_only_the_calling_user(protocol, other_client, album):
    """Quotas are per user; one uploader must not be able to lock everyone else out."""
    from tests.protocol import ProtocolClient
    mallory = ProtocolClient(other_client, album.token)
    protocol.init("big-a.jpg", 3 * MB)

    assert protocol.init("big-b.jpg", 3 * MB).status_code == 429
    assert mallory.init("big-c.jpg", 3 * MB).status_code == 201


def test_the_session_quota_counts_only_the_calling_user(protocol, other_client, album):
    from tests.protocol import ProtocolClient
    mallory = ProtocolClient(other_client, album.token)
    _open_sessions(protocol, TEST_MAX_SESSIONS)

    assert protocol.init("extra.jpg", 4096).status_code == 429
    assert mallory.init("mine.jpg", 4096).status_code == 201


@pytest.mark.parametrize("declared", [0, -1])
def test_init_refuses_a_non_positive_total_size(protocol, declared):
    """Zero would open a session that ``complete`` considers finished with no bytes."""
    response = protocol.init("empty.jpg", declared)

    assert response.status_code == 400
