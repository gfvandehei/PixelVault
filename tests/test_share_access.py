"""The share-token capability model: what a token can do, and what only a grant can.

One story in four parts, which is why they are in one file. A share token *names* an
album and mints an ``AlbumAccess`` grant when a logged-in visitor opens the album
page; the grant is then the only thing that authorises anything. Each section below
pins one way that model used to leak:

* **#35** — ``/view/<view_token>`` wrote the album's *upload* token into the
  visitor's session. Flask signs the session cookie but does not encrypt it, so the
  view-only recipient could read the upload capability straight out of their own
  cookie and use it.
* **#38** — ``access_type`` was read by the template and by nothing else, so the
  owner's "downgrade to view-only" hid a button and revoked nothing.
* **#39** — a trailing ``and not current_user.is_authenticated`` made the photo
  API's session check unreachable for every logged-in caller, so any account
  holding the token got the full index.
* **#40** — ``serve_share_media`` needed no login and wrote the token into the
  session, so one leaked media URL bootstrapped anonymous read access to the album.

The tests are written against behaviour a client can observe — status codes, stored
photos, and the cookie itself — rather than against the helpers, because the point
is that no *door* is open, not that one function returns False.
"""

import io

import pytest

from tests.conftest import TEST_CHUNK_SIZE
from tests.protocol import ProtocolClient


# ── Helpers ────────────────────────────────────────────────────────────────

def _upload(client, token, filename, data):
    """POST the multipart form the non-chunked uploader sends."""
    return client.post(
        f"/share/{token}/upload",
        data={"files": (io.BytesIO(data), filename)},
        content_type="multipart/form-data",
    )


def _stored_photo(client, album, jpeg, photos):
    """Put one real photo in the album as its owner and return its stored filename."""
    assert _upload(client, album.token, "holiday.jpg", jpeg).status_code == 200
    return photos(album.id)[0].stored_filename


def _session_values(test_client):
    """Every value in the client's signed session cookie, as the holder can read them."""
    with test_client.session_transaction() as sess:
        return dict(sess)


ANONYMOUS_REFUSALS = (301, 302, 401)


# ── #35 · No capability travels in the session cookie ──────────────────────

def test_the_view_only_page_puts_no_token_in_the_session(other_client, album):
    """The escalation was one assignment: ``session['album_access_token'] = album.token``.

    A signed cookie is not a secret from the person holding it, so the album's upload
    token must not appear in any session value — and neither must anything else that
    would authorise on its own.
    """
    assert other_client.get(f"/view/{album.view_token}").status_code == 200

    values = _session_values(other_client)
    assert "album_access_token" not in values
    assert album.token not in values.values()


def test_a_view_only_visit_mints_a_view_grant_and_nothing_more(other_client, other_user,
                                                               album, access_type):
    assert access_type(album.id, other_user.id) is None

    other_client.get(f"/view/{album.view_token}")

    assert access_type(album.id, other_user.id) == "view"


def test_a_view_only_guest_cannot_upload_with_the_albums_upload_token(
        other_client, album, small_jpeg, photos):
    """The whole of #35, end to end.

    The token is handed to the guest directly here — the cookie no longer carries it,
    but a re-shared link or an old screenshot might, and the refusal must not depend
    on the token staying secret.
    """
    other_client.get(f"/view/{album.view_token}")

    response = _upload(other_client, album.token, "evil.png", small_jpeg)

    assert response.status_code == 403
    assert photos(album.id) == []


def test_a_view_only_guest_cannot_open_a_chunked_session_either(other_client, album,
                                                                small_jpeg):
    """Two transports, one rule — the chunked path is not a second door into the album."""
    other_client.get(f"/view/{album.view_token}")

    response = ProtocolClient(other_client, album.token).init_for("evil.jpg", small_jpeg)

    assert response.status_code == 403


def test_the_album_page_puts_no_token_in_the_session_either(other_client, album):
    """The same assignment stood in ``album_view``'s non-owner branch.

    It leaked nothing there — the visitor arrived holding that very token — but two
    copies of a rule drift, and the surviving breadcrumb is deliberately only the
    token the guest typed themselves.
    """
    other_client.get(f"/share/{album.token}")
    assert other_client.get(f"/album/{album.token}").status_code == 200

    values = _session_values(other_client)
    assert "album_access_token" not in values
    assert values.get("album_upload_token") == album.token


def test_arriving_through_the_upload_link_mints_an_upload_grant(other_client, other_user,
                                                                album, access_type):
    """The breadcrumb's only job: which of the two links was followed."""
    other_client.get(f"/share/{album.token}")
    other_client.get(f"/album/{album.token}")

    assert access_type(album.id, other_user.id) == "upload"


def test_visiting_the_album_page_without_the_upload_link_mints_only_view(
        other_client, other_user, album, access_type):
    other_client.get(f"/album/{album.token}")

    assert access_type(album.id, other_user.id) == "view"


def test_the_view_link_never_downgrades_an_existing_upload_grant(
        other_client, other_user, album, grant_access, access_type, small_jpeg, photos):
    """Revocation is the owner's to do, not a side effect of which link a guest clicks."""
    grant_access(album.id, other_user.id, "upload")

    other_client.get(f"/view/{album.view_token}")

    assert access_type(album.id, other_user.id) == "upload"
    assert _upload(other_client, album.token, "holiday.jpg", small_jpeg).status_code == 200
    assert len(photos(album.id)) == 1


def test_the_upload_link_never_upgrades_an_existing_view_grant(
        other_client, other_user, album, grant_access, access_type, small_jpeg, photos):
    """Otherwise a downgraded guest re-grants themselves by re-opening the link they kept."""
    grant_access(album.id, other_user.id, "view")

    other_client.get(f"/share/{album.token}")
    other_client.get(f"/album/{album.token}")

    assert access_type(album.id, other_user.id) == "view"
    assert _upload(other_client, album.token, "evil.png", small_jpeg).status_code == 403
    assert photos(album.id) == []


# ── #38 · The downgrade reaches the endpoints ──────────────────────────────

def _downgrade(owner_client, album, user_id, access_type="view"):
    return owner_client.post(f"/album/{album.token}/access/{user_id}",
                             data={"access_type": access_type})


def test_a_downgraded_guest_can_no_longer_upload(client, other_client, other_user,
                                                 album, grant_access, small_jpeg, photos):
    """The control the owner is shown now changes the thing it names."""
    grant_access(album.id, other_user.id, "upload")
    assert _upload(other_client, album.token, "before.jpg", small_jpeg).status_code == 200

    assert _downgrade(client, album, other_user.id).status_code == 302

    after = _upload(other_client, album.token, "after.jpg", small_jpeg)
    assert after.status_code == 403
    assert after.get_json()["error"] == "You do not have upload access to this album."
    assert len(photos(album.id)) == 1


@pytest.mark.parametrize("endpoint", ["init", "status", "chunk", "complete"])
def test_a_downgrade_reaches_every_chunked_endpoint(client, other_client, other_user,
                                                    album, grant_access, multi_chunk_jpeg,
                                                    endpoint):
    """A 470 MB upload is ~59 requests over minutes; the owner can revoke inside that
    window, so the grant is re-read on every request, exactly as ``allow_upload`` is."""
    grant_access(album.id, other_user.id, "upload")
    mallory = ProtocolClient(other_client, album.token)
    upload_id = mallory.init_for("clip.jpg", multi_chunk_jpeg).get_json()["upload_id"]
    mallory.send_chunks(upload_id, multi_chunk_jpeg, TEST_CHUNK_SIZE, stop=TEST_CHUNK_SIZE)

    _downgrade(client, album, other_user.id)

    calls = {
        "init": lambda: mallory.init_for("another.jpg", multi_chunk_jpeg),
        "status": lambda: mallory.status(upload_id),
        "chunk": lambda: mallory.chunk(upload_id, TEST_CHUNK_SIZE,
                                       multi_chunk_jpeg[TEST_CHUNK_SIZE:2 * TEST_CHUNK_SIZE]),
        "complete": lambda: mallory.complete(upload_id),
    }
    response = calls[endpoint]()

    assert response.status_code == 403
    assert response.get_json()["error"] == "You do not have upload access to this album."


def test_a_downgraded_guest_can_still_cancel_and_reclaim_their_quota(
        client, other_client, other_user, album, grant_access, multi_chunk_jpeg,
        session_row):
    """Cancel stays open on purpose: a revoked guest must not be left holding a
    reservation against their in-flight byte quota until the TTL sweep collects it."""
    grant_access(album.id, other_user.id, "upload")
    mallory = ProtocolClient(other_client, album.token)
    upload_id = mallory.init_for("clip.jpg", multi_chunk_jpeg).get_json()["upload_id"]

    _downgrade(client, album, other_user.id)
    response = mallory.cancel(upload_id)

    assert response.status_code == 200
    assert response.get_json() == {"cancelled": True}
    assert session_row(upload_id) is None


def test_a_downgraded_guest_keeps_every_read_they_had(client, other_client, other_user,
                                                      album, grant_access, small_jpeg,
                                                      photos):
    """Read and write are two questions. Losing the second must not cost the first."""
    stored = _stored_photo(client, album, small_jpeg, photos)
    grant_access(album.id, other_user.id, "upload")

    _downgrade(client, album, other_user.id)

    assert other_client.get(f"/media/{stored}").status_code == 200
    assert other_client.get(f"/album/{album.token}").status_code == 200
    assert other_client.get(f"/share/{album.token}/download").status_code == 200


def test_an_outsider_holding_only_the_token_cannot_upload(other_client, album,
                                                          small_jpeg, photos):
    """A grant is minted by opening the album, and a missing one is a refusal rather
    than a default — otherwise a downgrade could be undone by deleting a cookie."""
    response = _upload(other_client, album.token, "evil.png", small_jpeg)

    assert response.status_code == 403
    assert photos(album.id) == []


def test_the_owner_needs_no_grant_row_of_their_own(client, album, small_jpeg, photos):
    assert _upload(client, album.token, "holiday.jpg", small_jpeg).status_code == 200
    assert len(photos(album.id)) == 1


def test_a_closed_album_is_reported_as_closed_even_to_a_grant_holder(
        other_client, other_user, album, grant_access, set_allow_upload, small_jpeg):
    """The two refusals stay distinguishable: one is about the album, one about you."""
    grant_access(album.id, other_user.id, "upload")
    set_allow_upload(album.id, False)

    response = _upload(other_client, album.token, "holiday.jpg", small_jpeg)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Uploads are disabled for this album."


# ── #39 · The photo API asks the question it documents ─────────────────────

def test_a_logged_in_outsider_cannot_list_an_albums_photos(client, other_client, album,
                                                           small_jpeg, photos):
    """The bug: the session check was guarded by ``and not current_user.is_authenticated``,
    so being logged in *skipped* it and the token alone bought the whole index."""
    _stored_photo(client, album, small_jpeg, photos)

    response = other_client.get(f"/api/album/{album.token}/photos")

    assert response.status_code == 403


def test_a_grant_holder_can_list_an_albums_photos(client, other_client, other_user,
                                                  album, grant_access, small_jpeg, photos):
    _stored_photo(client, album, small_jpeg, photos)
    grant_access(album.id, other_user.id, "view")

    response = other_client.get(f"/api/album/{album.token}/photos")

    assert response.status_code == 200
    assert len(response.get_json()) == 1


def test_the_owner_can_list_their_own_albums_photos(client, album, small_jpeg, photos):
    _stored_photo(client, album, small_jpeg, photos)

    response = client.get(f"/api/album/{album.token}/photos")

    assert response.status_code == 200
    assert response.get_json()[0]["full_url"].startswith("/media/")


def test_an_anonymous_caller_cannot_list_an_albums_photos(anon_client, album):
    assert anon_client.get(f"/api/album/{album.token}/photos").status_code in ANONYMOUS_REFUSALS


def test_the_view_listing_asks_the_same_question(client, other_client, other_user,
                                                 album, grant_access, small_jpeg, photos):
    """``/api/view/`` was ``@login_required`` and nothing else — an account plus a URL."""
    _stored_photo(client, album, small_jpeg, photos)

    assert other_client.get(f"/api/view/{album.view_token}/photos").status_code == 403

    grant_access(album.id, other_user.id, "view")
    assert other_client.get(f"/api/view/{album.view_token}/photos").status_code == 200


def test_an_unknown_token_is_404_before_any_access_question(client, album):
    assert client.get("/api/album/no-such-token/photos").status_code == 404


# ── #40 · A media URL is worth one file, to someone who already has access ─

def test_an_anonymous_caller_cannot_fetch_share_media(anon_client, client, album,
                                                      small_jpeg, photos):
    stored = _stored_photo(client, album, small_jpeg, photos)

    response = anon_client.get(f"/share/{album.token}/media/{stored}")

    assert response.status_code in ANONYMOUS_REFUSALS


def test_a_leaked_media_url_no_longer_bootstraps_the_album(anon_client, client, album,
                                                           small_jpeg, photos):
    """The whole of #40: the fetch used to answer 200 *and* write the token into the
    caller's session, promoting them into the state ``serve_media`` and the photo API
    both read as authorisation. One URL, then the entire album."""
    stored = _stored_photo(client, album, small_jpeg, photos)

    anon_client.get(f"/share/{album.token}/media/{stored}")

    assert "album_access_token" not in _session_values(anon_client)
    assert anon_client.get(f"/media/{stored}").status_code in ANONYMOUS_REFUSALS
    assert anon_client.get(f"/api/album/{album.token}/photos").status_code in ANONYMOUS_REFUSALS


def test_a_logged_in_outsider_with_a_media_url_is_refused(client, other_client, album,
                                                          small_jpeg, photos):
    """A media URL travels in history, in ``Referer``, in screenshots and in every
    listing already handed out. It names a file; it does not authorise one."""
    stored = _stored_photo(client, album, small_jpeg, photos)

    assert other_client.get(f"/media/{stored}").status_code == 403
    assert other_client.get(f"/share/{album.token}/media/{stored}").status_code == 403
    assert other_client.get(f"/view/{album.view_token}/media/{stored}").status_code == 403


def test_a_thumbnail_is_the_same_decision_as_its_original(client, other_client, other_user,
                                                          album, grant_access, small_jpeg,
                                                          photos):
    """``thumb_`` is a naming convention on disk, not a second, softer object."""
    stored = _stored_photo(client, album, small_jpeg, photos)
    assert photos(album.id)[0].has_thumbnail

    assert other_client.get(f"/media/thumb_{stored}").status_code == 403

    grant_access(album.id, other_user.id, "view")
    assert other_client.get(f"/media/thumb_{stored}").status_code == 200


def test_a_grant_holder_can_fetch_media_through_every_door(client, other_client,
                                                           other_user, album, grant_access,
                                                           small_jpeg, photos):
    """The rendered album page points guests at ``/media/``; the photo API hands them
    token-scoped URLs. Both have to work for the same person, or the gallery breaks."""
    stored = _stored_photo(client, album, small_jpeg, photos)
    grant_access(album.id, other_user.id, "view")

    assert other_client.get(f"/media/{stored}").status_code == 200
    assert other_client.get(f"/share/{album.token}/media/{stored}").status_code == 200
    assert other_client.get(f"/view/{album.view_token}/media/{stored}").status_code == 200


def test_a_contributor_can_still_see_their_own_upload(client, other_client, other_user,
                                                      album, grant_access, small_jpeg,
                                                      photos, app):
    """Their own file stays readable even with the grant withdrawn entirely — the
    dashboard lists contributed albums on exactly that fact."""
    grant_access(album.id, other_user.id, "upload")
    _upload(other_client, album.token, "mine.jpg", small_jpeg)
    stored = photos(album.id)[0].stored_filename

    from pixelvault.extensions import db
    from pixelvault.models import AlbumAccess
    with app.app_context():
        db.session.query(AlbumAccess).filter_by(
            user_id=other_user.id, album_id=album.id).delete()
        db.session.commit()

    assert other_client.get(f"/media/{stored}").status_code == 200


def test_the_zip_download_is_not_a_way_round_the_media_rule(client, other_client,
                                                            other_user, album,
                                                            grant_access, small_jpeg,
                                                            photos):
    """One request, every photo — the widest read in the app, so it asks the same
    question the narrowest one does."""
    _stored_photo(client, album, small_jpeg, photos)

    assert other_client.get(f"/share/{album.token}/download").status_code == 403
    assert other_client.get(f"/view/{album.view_token}/download").status_code == 403

    grant_access(album.id, other_user.id, "view")
    assert other_client.get(f"/share/{album.token}/download").status_code == 200
    assert other_client.get(f"/view/{album.view_token}/download").status_code == 200


def test_a_media_filename_for_another_album_is_not_reachable_through_this_token(
        client, other_client, other_user, album, grant_access, small_jpeg, photos, app,
        user):
    """The token scopes the lookup, so a grant on one album cannot be spent on another."""
    from pixelvault.extensions import db
    from pixelvault.models import Album

    with app.app_context():
        private = Album(name="Private", owner_id=user.id, description="")
        db.session.add(private)
        db.session.commit()
        private_id, private_token = private.id, private.token

    assert _upload(client, private_token, "secret.jpg", small_jpeg).status_code == 200
    secret = photos(private_id)[0].stored_filename

    grant_access(album.id, other_user.id, "view")

    assert other_client.get(f"/share/{album.token}/media/{secret}").status_code == 404
    assert other_client.get(f"/media/{secret}").status_code == 403
