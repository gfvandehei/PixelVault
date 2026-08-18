"""The two ceilings on a chunked upload: the rate limiter and the per-user quotas.

They protect different things and are easy to confuse. The limiter is per worker
process, in memory, and resets on deploy — it bounds *request rate*. The quotas
live in the database and bound *bytes and sessions on disk*. Chunking is what
forced them apart: once one file is sixty requests, request count stopped
correlating with cost, so the limiter had to stop charging for the traffic the
feature exists to produce.

The chunk endpoint charges the limiter for every response that is not a 200, and
for nothing else. That rule replaced a 422-only charge which was wrong in both
directions at once: every other refusal was an unmetered 8 MiB sink, and the limit
was still *checked* on requests it never deducted, so sixty bad-network checksum
failures threw a 429 at every chunk that user sent for the next hour.

Both directions are easy to regress silently, because neither breaks anything a
happy-path test would notice — one only breaks under abuse, the other only breaks
for the clients already having a bad day. Hence the flood tests below, which pin
the budget from both sides: refusals are bounded, and the number that bounds them
is far above anything an honest client produces.

Sizes come from ``conftest``: an 8 MB request ceiling over a 4 MB in-flight quota
over 3 concurrent sessions, chosen so each limit can be tripped without tripping
the others. The limiter's counters are reset between tests by ``reset_state``.
"""

import pytest

from tests.conftest import (TEST_CHUNK_SIZE, TEST_MAX_INFLIGHT_MB,
                            TEST_MAX_SESSIONS, TEST_MAX_UPLOAD_MB)
from tests.protocol import client_key_for

MB = 1024 * 1024

#: The chunk endpoint's own limit, charged only for non-200s. Floods below must
#: comfortably exceed it; honest-client tests must stay comfortably under it.
CHUNK_FAILURE_BUDGET = 600

#: A run of failures a genuinely unlucky client could hit in an hour — a flapping
#: link corrupting chunk after chunk. It used to be the whole budget.
BAD_NETWORK_RUN = 60


# ── What the failure budget charges ───────────────────────────────────────

def test_a_flood_of_offset_mismatches_is_eventually_throttled(protocol, multi_chunk_jpeg):
    """The cheapest sink on the endpoint, and once entirely free.

    One session parked at a fixed offset answers 409 forever, and every one of those
    requests used to cost the server a full body read with the limiter's counter still
    reading zero. It is still not an *error* — see the resume tests below — but it is
    no longer unmetered.
    """
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    piece = multi_chunk_jpeg[:1024]

    statuses = [protocol.chunk(init["upload_id"], 9_999, piece).status_code
                for _ in range(CHUNK_FAILURE_BUDGET + 5)]

    assert statuses[0] == 409
    assert statuses.index(429) == CHUNK_FAILURE_BUDGET


def test_repeated_checksum_mismatches_are_eventually_throttled(protocol, multi_chunk_jpeg):
    """A 422 is the costliest refusal — the server read the body and hashed it before
    finding out. Replaying a bad digest must not stay free."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    piece = multi_chunk_jpeg[:1024]

    statuses = [protocol.chunk(init["upload_id"], 0, piece, digest="0" * 64).status_code
                for _ in range(CHUNK_FAILURE_BUDGET + 5)]

    assert statuses[0] == 422
    assert statuses.index(429) == CHUNK_FAILURE_BUDGET


def test_repeated_chunks_with_no_digest_are_eventually_throttled(protocol,
                                                                 multi_chunk_jpeg):
    """Omitting ``X-Chunk-SHA256`` was once a way to write unverified bytes; it was
    also, because only 422s were charged, a way to replay them for free."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    piece = multi_chunk_jpeg[:1024]
    url = f"{protocol.base}/chunk/{init['upload_id']}"

    statuses = [protocol.client.post(url, data=piece,
                                     content_type="application/octet-stream",
                                     headers={"X-Upload-Offset": "0"}).status_code
                for _ in range(CHUNK_FAILURE_BUDGET + 5)]

    assert statuses[0] == 400
    assert statuses.index(429) == CHUNK_FAILURE_BUDGET


def test_repeated_overruns_are_eventually_throttled(protocol, multi_chunk_jpeg):
    """413 was free too: understate ``total_size`` at init and every chunk after it is
    a refused 8 MiB read, indefinitely."""
    init = protocol.init("clip.jpg", 1024).get_json()
    piece = multi_chunk_jpeg[:4096]

    statuses = [protocol.chunk(init["upload_id"], 0, piece).status_code
                for _ in range(CHUNK_FAILURE_BUDGET + 5)]

    assert statuses[0] == 413
    assert statuses.index(429) == CHUNK_FAILURE_BUDGET


# ── What it must not charge ────────────────────────────────────────────────

def test_a_full_upload_spends_nothing_at_all(protocol, multi_chunk_jpeg, album, photos):
    """A 500 MB file is ~63 chunks and a bad hour is 600 refusals, so the two would
    share a budget if success were charged. Walk a file in 1 KiB slices — more requests
    than the whole failure budget, all of them 200, none of them charged."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    upload_id = init["upload_id"]

    responses = protocol.send_chunks(upload_id, multi_chunk_jpeg, 1024,
                                     stop=(CHUNK_FAILURE_BUDGET + 20) * 1024)

    assert len(responses) > CHUNK_FAILURE_BUDGET
    assert {r.status_code for r in responses} == {200}


def test_a_resume_costs_a_rounding_error_of_the_budget(protocol, multi_chunk_jpeg):
    """409 is how a resuming client and the loser of a two-tab race find their place.
    Charging it is only defensible while the budget is sized so that no resume can
    notice: the ones below are a hundred times what a real resume needs."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    upload_id = init["upload_id"]

    statuses = {protocol.chunk(upload_id, 9_999, multi_chunk_jpeg[:1024]).status_code
                for _ in range(BAD_NETWORK_RUN)}

    assert statuses == {409}


def test_a_flood_of_offset_mismatches_leaves_the_upload_usable(protocol, multi_chunk_jpeg,
                                                               album, photos, stored_bytes):
    """Not poisoned, and not throttled out of finishing: the session survives intact."""
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    upload_id = init["upload_id"]
    for _ in range(2 * BAD_NETWORK_RUN):
        protocol.chunk(upload_id, 9_999, multi_chunk_jpeg[:TEST_CHUNK_SIZE])

    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE)
    protocol.complete(upload_id)

    assert stored_bytes(photos(album.id)[0].stored_filename) == multi_chunk_jpeg


def test_a_bad_network_run_of_checksum_failures_does_not_lock_the_user_out(
        protocol, multi_chunk_jpeg):
    """The inverse defect, and the reason the budget is 600 rather than 60.

    The limiter guards the endpoint, not a status code — once the bucket is empty even
    a well-formed chunk waits. With the budget set to a plausible run of corruption,
    a flapping link was enough to deny that user uploads entirely for an hour.
    """
    init = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()
    upload_id = init["upload_id"]
    for _ in range(BAD_NETWORK_RUN):
        protocol.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE], digest="0" * 64)

    honest = protocol.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE])

    assert honest.status_code == 200


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
    the function reads. That one rebinding is enough for the guarded INSERT as well:
    it enforces the caps ``check_user_quota`` hands it rather than reading its own.
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


# ── Quotas under concurrency ───────────────────────────────────────────────
#
# The caps are checked and then acted on, and for a long time those were two
# statements with nothing holding a lock between them — pysqlite runs a bare SELECT
# in autocommit, so a burst of inits all read the same "still room" and all inserted.
# Measured at ~1.6x over both caps, and every session admitted over the line then sat
# there for the full 24 h TTL. The insert now carries the caps in its own WHERE.

def _init_concurrently(app, user_ref, token, requests, workers=12):
    """Fire ``requests`` inits at once, each from its own client, returning the statuses.

    A client per thread rather than one shared: the test client keeps a cookie jar and
    a request context, and sharing those would be testing the harness, not the server.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from tests.conftest import login
    from tests.protocol import ProtocolClient

    workers = min(workers, len(requests))
    # Everything expensive — building the client, planting the session cookie — happens
    # before the barrier, so what actually overlaps is the request itself. Without it
    # the pool's own warm-up staggers the threads enough to hide the race.
    at_the_line = threading.Barrier(workers)

    def _one(index):
        test_client = app.test_client()
        login(test_client, user_ref)
        filename, size = requests[index]
        protocol = ProtocolClient(test_client, token)
        at_the_line.wait()
        return protocol.init(filename, size).status_code

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_one, range(len(requests))))


def _live_sessions(app):
    from pixelvault.extensions import db
    from pixelvault.models import UploadSession
    with app.app_context():
        return db.session.query(UploadSession).all()


def test_concurrent_inits_cannot_overshoot_the_session_quota(app, user, album):
    """Distinct client keys, so nothing is deduplicated by the unique constraint — the
    only thing standing between 24 simultaneous inits and 24 sessions is the cap."""
    requests = [(f"file{i}.jpg", 4096) for i in range(12)]

    statuses = _init_concurrently(app, user, album.token, requests)

    assert len(_live_sessions(app)) <= TEST_MAX_SESSIONS
    assert statuses.count(201) == len(_live_sessions(app))
    assert set(statuses) <= {201, 429}


def test_concurrent_inits_cannot_overshoot_the_in_flight_byte_quota(app, user, album):
    """Same race, counted in bytes: one 3 MB session fits under a 4 MB cap and two
    do not, however close together they arrive."""
    three_mb = 3 * MB
    requests = [(f"big{i}.jpg", three_mb) for i in range(12)]

    _init_concurrently(app, user, album.token, requests)

    sessions = _live_sessions(app)
    assert sum(row.total_size for row in sessions) <= TEST_MAX_INFLIGHT_MB * MB
    assert len(sessions) == 1


def test_a_refused_concurrent_init_still_explains_itself(app, user, album):
    """The guard lives in the INSERT, which cannot say which cap it tripped; the
    message has to come from re-reading the table afterwards."""
    requests = [(f"file{i}.jpg", 4096) for i in range(12)]

    _init_concurrently(app, user, album.token, requests)
    from tests.protocol import ProtocolClient
    from tests.conftest import login
    test_client = app.test_client()
    login(test_client, user)
    refused = ProtocolClient(test_client, album.token).init("one-more.jpg", 4096)

    assert refused.status_code == 429
    assert "error" in refused.get_json()
