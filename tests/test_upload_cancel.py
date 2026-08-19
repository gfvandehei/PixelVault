"""Cancelling a chunked upload — the quota's release valve.

Every open session reserves its **declared** ``total_size`` against the per-user
in-flight cap, in every album, for the full TTL. Before cancel existed the only
ways out were finishing the transfer or waiting a day, so a user who queued three
large files, changed their mind, and re-picked them met "upload would exceed your
in-flight limit" with nothing actually uploading. That is the failure these tests
exist for: the assertions are about the quota being *free again*, not merely about
a row disappearing.

The endpoint's three deviations from the rest of the protocol — always 200, not
gated on ``allow_upload``, silent about foreign handles — are each pinned below,
because each one looks like a bug to anyone reading the route in isolation.
See docs/upload_protocol.md §6.5.
"""

from tests.conftest import TEST_CHUNK_SIZE, TEST_MAX_INFLIGHT_MB, TEST_MAX_SESSIONS
from tests.protocol import ProtocolClient, client_key_for

MB = 1024 * 1024


# ── What cancel removes ────────────────────────────────────────────────────

def test_cancel_deletes_the_session_row(protocol, multi_chunk_jpeg, session_row):
    upload_id = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()["upload_id"]

    response = protocol.cancel(upload_id)

    assert response.status_code == 200
    assert response.get_json() == {"cancelled": True}
    assert session_row(upload_id) is None


def test_cancel_unlinks_the_partial_file(protocol, multi_chunk_jpeg, partials_dir):
    """The row is the quota's bookkeeping; the ``.part`` is the disk it was protecting."""
    upload_id = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()["upload_id"]
    protocol.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE])
    partial = partials_dir / f"{upload_id}.part"
    assert partial.exists()

    protocol.cancel(upload_id)

    assert not partial.exists()


# ── What cancel gives back ─────────────────────────────────────────────────

def test_cancel_frees_the_reserved_bytes_immediately(protocol):
    """The whole point. Two-thirds of the byte quota is reserved, then released, and
    a file that did not fit before now fits — without waiting out the TTL."""
    two_thirds = (TEST_MAX_INFLIGHT_MB * MB * 2) // 3
    upload_id = protocol.init("first.jpg", two_thirds).get_json()["upload_id"]
    blocked = protocol.init("second.jpg", two_thirds)
    assert blocked.status_code == 429

    protocol.cancel(upload_id)

    assert protocol.init("second.jpg", two_thirds).status_code == 201


def test_cancel_frees_a_session_slot_immediately(protocol):
    """The count cap releases too, not just the byte cap — they are separate guards
    in ``check_user_quota`` and the guarded INSERT, and only one is exercised above."""
    upload_ids = [protocol.init(f"f{i}.jpg", 1024).get_json()["upload_id"]
                  for i in range(TEST_MAX_SESSIONS)]
    assert protocol.init("one-too-many.jpg", 1024).status_code == 429

    protocol.cancel(upload_ids[0])

    assert protocol.init("one-too-many.jpg", 1024).status_code == 201


def test_a_cancelled_upload_re_inits_from_zero(protocol, multi_chunk_jpeg):
    """Cancel is not a pause. ``init`` is idempotent on ``client_key``, so a cancelled
    session must actually be gone or the re-init would resume into bytes the user
    asked to discard."""
    key = client_key_for("clip.jpg", len(multi_chunk_jpeg))
    upload_id = protocol.init_for("clip.jpg", multi_chunk_jpeg,
                                  client_key=key).get_json()["upload_id"]
    protocol.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE])
    protocol.cancel(upload_id)

    again = protocol.init_for("clip.jpg", multi_chunk_jpeg, client_key=key)

    assert again.status_code == 201
    assert again.get_json()["received_bytes"] == 0
    assert again.get_json()["resumed"] is False
    assert again.get_json()["upload_id"] != upload_id


def test_a_chunk_sent_after_cancel_is_404(protocol, multi_chunk_jpeg):
    """The client fires cancel without waiting, so an in-flight chunk can arrive after
    it. The session lookup must refuse rather than recreate anything on disk."""
    upload_id = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()["upload_id"]
    protocol.cancel(upload_id)

    response = protocol.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE])

    assert response.status_code == 404


# ── Idempotence: cancel never reports failure ──────────────────────────────

def test_cancelling_twice_is_not_an_error(protocol, multi_chunk_jpeg):
    """A double click, a retry, or a handle the sweep already took all mean the same
    thing to the caller — the session is not there, which is what they wanted."""
    upload_id = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()["upload_id"]
    protocol.cancel(upload_id)

    second = protocol.cancel(upload_id)

    assert second.status_code == 200
    assert second.get_json() == {"cancelled": False}


def test_cancelling_an_unknown_handle_is_200(protocol):
    response = protocol.cancel("no-such-upload-id")

    assert response.status_code == 200
    assert response.get_json() == {"cancelled": False}


def test_cancelling_an_expired_session_is_200(protocol, multi_chunk_jpeg, age_session):
    upload_id = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()["upload_id"]
    age_session(upload_id)

    response = protocol.cancel(upload_id)

    assert response.status_code == 200


# ── Permissions ────────────────────────────────────────────────────────────

def test_cancel_works_after_uploads_are_disabled_on_the_album(
        protocol, multi_chunk_jpeg, album, set_allow_upload, session_row):
    """Deliberately *not* behind ``_album_for_upload``. Closing an album would
    otherwise strand every guest's reservation for the full TTL, and handing quota
    back is the one upload operation that stays safe once uploads are off."""
    upload_id = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()["upload_id"]
    set_allow_upload(album.id, False)

    response = protocol.cancel(upload_id)

    assert response.status_code == 200
    assert response.get_json() == {"cancelled": True}
    assert session_row(upload_id) is None


def test_another_user_cannot_cancel_your_session(protocol, other_client, album,
                                                 multi_chunk_jpeg, session_row):
    """``upload_id`` is a bearer handle, so the lookup is scoped to the caller. The
    stranger is told nothing-to-cancel rather than 403 — a refusal would confirm the
    handle is real."""
    upload_id = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()["upload_id"]

    response = ProtocolClient(other_client, album.token).cancel(upload_id)

    assert response.status_code == 200
    assert response.get_json() == {"cancelled": False}
    assert session_row(upload_id) is not None


def test_an_anonymous_caller_cannot_cancel(anon_client, album, protocol,
                                           multi_chunk_jpeg, session_row):
    upload_id = protocol.init_for("clip.jpg", multi_chunk_jpeg).get_json()["upload_id"]

    response = ProtocolClient(anon_client, album.token).cancel(upload_id)

    assert response.status_code in (301, 302, 401)
    assert session_row(upload_id) is not None


def test_cancel_on_an_unknown_album_token_is_404(client):
    response = ProtocolClient(client, "not-a-real-token").cancel("whatever")

    assert response.status_code == 404
