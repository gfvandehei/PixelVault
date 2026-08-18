"""Who may touch a session, and for how long that permission lasts.

An ``upload_id`` is a bearer handle: it appears in a URL, it lives in the client's
localStorage, and it is the only thing naming the ``.part`` file a chunk writes
into. So every chunked endpoint re-derives the album from the share token and
re-scopes the session lookup to ``current_user.id`` — on every request, not once
at init.

The "on every request" part is what these tests are really for. A 470 MB file is
~59 requests over several minutes, and the owner can revoke uploads at any point
in that window. The permission that admitted the first chunk says nothing about
the last one.
"""

import pytest

from tests.conftest import TEST_CHUNK_SIZE
from tests.protocol import ProtocolClient


def _started(protocol, data, chunks_to_send=2):
    """Init and send a few chunks, returning ``(upload_id, cursor)``."""
    init = protocol.init_for("clip.jpg", data).get_json()
    stop = chunks_to_send * TEST_CHUNK_SIZE
    protocol.send_chunks(init["upload_id"], data, TEST_CHUNK_SIZE, stop=stop)
    return init["upload_id"], stop


def _session_count(app):
    from pixelvault.extensions import db
    from pixelvault.models import UploadSession
    with app.app_context():
        return db.session.query(UploadSession).count()


# ── A session belongs to one user ──────────────────────────────────────────

def test_another_users_session_is_reported_missing_not_forbidden(
        protocol, other_client, album, multi_chunk_jpeg):
    """403 would confirm to a stranger that the handle they hold is real. §8."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    mallory = ProtocolClient(other_client, album.token)

    assert mallory.status(upload_id).status_code == 404
    assert mallory.chunk(upload_id, cursor,
                         multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE]).status_code == 404
    assert mallory.complete(upload_id).status_code == 404


def test_a_foreign_chunk_does_not_move_the_owners_cursor(
        protocol, other_client, album, multi_chunk_jpeg, session_row, partials_dir):
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    mallory = ProtocolClient(other_client, album.token)

    mallory.chunk(upload_id, cursor, b"\x00" * 4096)

    assert session_row(upload_id).received_bytes == cursor
    assert (partials_dir / f"{upload_id}.part").stat().st_size == cursor


def test_the_owner_can_still_finish_after_a_foreign_attempt(
        protocol, other_client, album, multi_chunk_jpeg, photos, stored_bytes):
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    ProtocolClient(other_client, album.token).chunk(upload_id, cursor, b"\x00" * 4096)

    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE, start=cursor)
    protocol.complete(upload_id)

    assert stored_bytes(photos(album.id)[0].stored_filename) == multi_chunk_jpeg


def test_a_session_is_not_reachable_through_a_different_albums_token(
        app, client, protocol, user, multi_chunk_jpeg):
    """The lookup is scoped by album as well as user, so a handle cannot be
    redirected into another album the same user happens to own."""
    from pixelvault.extensions import db
    from pixelvault.models import Album

    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    with app.app_context():
        other_album = Album(name="Other", owner_id=user.id, description="")
        db.session.add(other_album)
        db.session.commit()
        other_token = other_album.token

    elsewhere = ProtocolClient(client, other_token)

    assert elsewhere.status(upload_id).status_code == 404
    assert elsewhere.complete(upload_id).status_code == 404


def test_an_upload_id_that_was_never_issued_is_a_404_on_every_endpoint(protocol,
                                                                       small_jpeg):
    made_up = "00000000-0000-0000-0000-000000000000"

    assert protocol.status(made_up).status_code == 404
    assert protocol.chunk(made_up, 0, small_jpeg).status_code == 404
    assert protocol.complete(made_up).status_code == 404


# ── Revocation mid-transfer ────────────────────────────────────────────────

def test_disabling_uploads_blocks_the_next_chunk(protocol, multi_chunk_jpeg,
                                                 album, set_allow_upload, session_row):
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)

    set_allow_upload(album.id, False)
    response = protocol.chunk(upload_id, cursor,
                              multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE])

    assert response.status_code == 403
    assert response.get_json()["error"] == "Uploads are disabled for this album."
    assert session_row(upload_id).received_bytes == cursor


def test_disabling_uploads_blocks_complete_even_when_every_byte_has_landed(
        protocol, small_jpeg, album, set_allow_upload, photos):
    """The last request is the one that creates the Photo; revocation has to reach it."""
    init = protocol.init_for("holiday.jpg", small_jpeg).get_json()
    protocol.chunk(init["upload_id"], 0, small_jpeg)

    set_allow_upload(album.id, False)
    response = protocol.complete(init["upload_id"])

    assert response.status_code == 403
    assert photos(album.id) == []


def test_disabling_uploads_blocks_a_new_session(protocol, album, set_allow_upload,
                                                small_jpeg, app):
    set_allow_upload(album.id, False)

    response = protocol.init_for("holiday.jpg", small_jpeg)

    assert response.status_code == 403
    assert _session_count(app) == 0


def test_disabling_uploads_blocks_the_status_probe(protocol, multi_chunk_jpeg,
                                                   album, set_allow_upload):
    upload_id, _ = _started(protocol, multi_chunk_jpeg)

    set_allow_upload(album.id, False)

    assert protocol.status(upload_id).status_code == 403


def test_re_enabling_uploads_lets_the_same_session_carry_on(
        protocol, multi_chunk_jpeg, album, set_allow_upload, photos, stored_bytes):
    """Revocation pauses a transfer; it must not silently destroy it."""
    upload_id, cursor = _started(protocol, multi_chunk_jpeg)
    set_allow_upload(album.id, False)
    protocol.chunk(upload_id, cursor, multi_chunk_jpeg[cursor:cursor + TEST_CHUNK_SIZE])

    set_allow_upload(album.id, True)
    protocol.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE, start=cursor)
    protocol.complete(upload_id)

    assert stored_bytes(photos(album.id)[0].stored_filename) == multi_chunk_jpeg


def test_an_unknown_share_token_creates_nothing_for_an_anonymous_caller(anon_client,
                                                                        small_jpeg, app):
    """No session, no album, nothing created."""
    unknown = ProtocolClient(anon_client, "no-such-token")

    assert unknown.init_for("holiday.jpg", small_jpeg).status_code in (302, 401, 404)
    assert _session_count(app) == 0


def test_a_known_token_with_an_unknown_album_row_is_404_for_a_logged_in_caller(client):
    missing = ProtocolClient(client, "11111111-2222-3333-4444-555555555555")

    assert missing.init("holiday.jpg", 4096).status_code == 404


# ── Authentication ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("endpoint", ["init", "status", "chunk", "complete"])
def test_an_anonymous_caller_cannot_reach_any_chunked_endpoint(anon_client, album,
                                                               small_jpeg, app, endpoint):
    anon = ProtocolClient(anon_client, album.token)
    calls = {
        "init": lambda: anon.init_for("holiday.jpg", small_jpeg),
        "status": lambda: anon.status("00000000-0000-0000-0000-000000000000"),
        "chunk": lambda: anon.chunk("00000000-0000-0000-0000-000000000000", 0, small_jpeg),
        "complete": lambda: anon.complete("00000000-0000-0000-0000-000000000000"),
    }

    response = calls[endpoint]()

    assert response.status_code not in (200, 201)
    assert _session_count(app) == 0


def test_an_anonymous_caller_cannot_finish_a_logged_in_users_upload(
        protocol, anon_client, album, small_jpeg, photos):
    init = protocol.init_for("holiday.jpg", small_jpeg).get_json()
    protocol.chunk(init["upload_id"], 0, small_jpeg)

    anon = ProtocolClient(anon_client, album.token)
    response = anon.complete(init["upload_id"])

    assert response.status_code not in (200, 201)
    assert photos(album.id) == []


def test_an_anonymous_caller_gets_401_rather_than_a_redirect_to_the_login_page(
        anon_client, album, small_jpeg):
    """A 302 to the HTML login page is invisible to XHR — it follows it and reports
    HTTP 200 with a login page as the body, so uploader.js's "Session expired — reload
    the page" branch never runs. Mid-upload expiry is the realistic trigger."""
    anon = ProtocolClient(anon_client, album.token)

    response = anon.init_for("holiday.jpg", small_jpeg)

    assert response.status_code == 401


def test_a_browser_page_load_still_gets_the_login_redirect(anon_client):
    """The 401 is for callers that speak JSON. A person following a link to a page
    still gets sent to the login form, with ``next`` set so they land where they meant
    to — regressing that would trade one bad experience for another."""
    response = anon_client.get("/dashboard")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_a_chunk_from_a_logged_out_client_is_a_readable_401(anon_client, album,
                                                            small_jpeg):
    """The realistic trigger: the login session expires mid-transfer, so the 401 has
    to reach uploader.js on the chunk endpoint too, not just on init."""
    anon = ProtocolClient(anon_client, album.token)

    response = anon.chunk("00000000-0000-0000-0000-000000000000", 0, small_jpeg)

    assert response.status_code == 401
    assert response.mimetype == "application/json"
    assert "error" in response.get_json()
