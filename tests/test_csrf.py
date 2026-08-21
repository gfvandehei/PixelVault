"""Cross-site request forgery: the token, and what happens without it (#37).

Every other module in this suite runs with ``WTF_CSRF_ENABLED = False`` — see the
``app`` fixture in conftest.py for why. This is the module that turns it back on,
and it is therefore the only place the protection is actually exercised. If it is
deleted or skipped, the app is untested against the whole class of attack.

The shape of every test here is the same pair, because a CSRF fix has two ways to
be wrong and only asserting both catches either:

* a request **without** a token must be refused, and must leave nothing changed;
* a request **with** one must go through, unchanged in every other respect.

Only the second half catches the failure mode that actually ships: protection
switched on, some caller never taught to send the token, and a feature quietly
broken for real users. Four caller classes each carry the token differently, and
each gets its own section below:

* browser form POSTs — hidden ``csrf_token`` field, rendered by the template;
* in-page ``fetch`` — ``X-CSRFToken`` header, read from the meta tag in base.html;
* chunk uploads — same header, because the body is raw ``application/octet-stream``
  and there is no form to put a field in;
* the fire-and-forget ``DELETE`` of ``cancel`` — same header again, and the one
  whose failure would be silent, since nothing reads its response.

Tokens are obtained the way a browser gets one: by fetching a page and reading it
out of the rendered HTML. Never by calling ``generate_csrf()`` — that would prove
the extension works while saying nothing about whether the templates emit it, and
a missing hidden field is exactly the bug this module is here to catch.
"""

import re

import pytest

from pixelvault import invites
from pixelvault.extensions import db
from pixelvault.models import Album, AllowedEmail, AlbumAccess, Photo, User

from tests.conftest import Ref, TEST_CHUNK_SIZE, login
from tests.protocol import HEADER_CSRF, ProtocolClient

#: What Flask-WTF answers a bad or missing token with. Deliberately not 403: a 403
#: from an upload endpoint is uploader.js's "Uploads are disabled for this album".
REFUSED = 400

#: Endpoints named in #37, by Flask endpoint name. Asserted to still exist and to
#: still be mutating, so that renaming one cannot silently drop it out of the
#: url_map sweep at the bottom of this file.
ISSUE_37_ENDPOINTS = {
    'create_album',
    'delete_album',
    'album_settings',
    'set_album_access',
    'delete_photo',
    'do_upload',
    'upload_init',
    'upload_chunk',
    'upload_complete',
    'upload_cancel',
    'admin_add_email',
    'admin_remove_email',
    'admin_resend_invite',
    'admin_invite_link',
    'admin_delete_album',
    'login',
    'invite_submit',
}

_META_TOKEN = re.compile(rb'<meta name="csrf-token" content="([^"]+)"')
_FIELD_TOKEN = re.compile(rb'name="csrf_token" value="([^"]+)"')


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def csrf(app):
    """Turn the protection on for the duration of one test, then off again.

    A config flag rather than a second app: ``create_app()`` re-registers every
    route on the module-level limiter singleton, so a second app would stack a
    duplicate copy of every rate limit and quietly halve the budgets the upload
    tests measure. ``CSRFProtect.protect`` reads this key on each request, so
    flipping it is exact — the same wiring, the same hooks, the switch the
    operator actually has.
    """
    app.config['WTF_CSRF_ENABLED'] = True
    yield
    app.config['WTF_CSRF_ENABLED'] = False


def token_from(client, path):
    """Fetch ``path`` as a browser would and read the token out of the response.

    Prefers the hidden form field over the meta tag when the page has one, because
    that is the copy a form POST would actually submit — the two are the same
    string, but asserting on the one under test is what makes a page that renders
    the meta tag and forgets the field fail here.
    """
    response = client.get(path)
    assert response.status_code == 200, f"GET {path} -> {response.status_code}"
    match = _FIELD_TOKEN.search(response.data) or _META_TOKEN.search(response.data)
    assert match is not None, f"no CSRF token rendered on {path}"
    return match.group(1).decode()


@pytest.fixture
def album_row(app):
    """Return a callable answering whether an album id still exists."""
    def _exists(album_id):
        with app.app_context():
            return db.session.get(Album, album_id) is not None
    return _exists


@pytest.fixture
def photo(app, album, user):
    """One committed Photo row in ``album``, for the delete-photo fetch."""
    with app.app_context():
        row = Photo(
            album_id=album.id,
            uploader_id=user.id,
            uploader_name='alice',
            stored_filename='deadbeef.jpg',
            original_filename='holiday.jpg',
            mime_type='image/jpeg',
            file_size=1234,
            has_thumbnail=False,
        )
        db.session.add(row)
        db.session.commit()
        return Ref(id=row.id)


@pytest.fixture
def invite(app):
    """A live invitation whose acceptance form is a POST worth forging."""
    with app.app_context():
        row, token = invites.issue(db.session, 'invitee@example.com')
        return Ref(id=row.id, email=row.email, token=token)


# ── 1. Browser form POSTs — the hidden field ───────────────────────────────

def test_album_delete_is_refused_without_a_token(csrf, client, album, album_row):
    """The reproduction in #37: a cross-site POST destroyed an album outright.

    Asserting the album survives, not merely that the status is 400, is the point.
    A refusal that arrives after the deletion has committed is not a refusal.
    """
    response = client.post(f'/album/{album.token}/delete')

    assert response.status_code == REFUSED
    assert album_row(album.id) is True


def test_album_delete_succeeds_with_a_token(csrf, client, album, album_row):
    """...and the owner's own click, carrying the token the page rendered, still works."""
    token = token_from(client, f'/album/{album.token}')

    response = client.post(f'/album/{album.token}/delete',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert album_row(album.id) is False


def test_album_create_is_refused_without_a_token(csrf, client, app):
    """A forged POST cannot make albums."""
    response = client.post('/album/create', data={'name': 'Forged'})

    assert response.status_code == REFUSED
    with app.app_context():
        assert db.session.query(Album).filter_by(name='Forged').count() == 0


def test_album_create_succeeds_with_a_token(csrf, client, app):
    token = token_from(client, '/album/create')

    response = client.post('/album/create',
                           data={'name': 'Genuine', 'csrf_token': token})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.query(Album).filter_by(name='Genuine').count() == 1


def test_album_settings_toggle_needs_a_token(csrf, client, app, album):
    """``allow_upload`` is a one-field form and the cheapest thing to forge."""
    tokenless = client.post(f'/album/{album.token}/settings', data={})
    assert tokenless.status_code == REFUSED

    token = token_from(client, f'/album/{album.token}')
    accepted = client.post(f'/album/{album.token}/settings',
                           data={'allow_upload': 'on', 'csrf_token': token})

    assert accepted.status_code == 302
    with app.app_context():
        assert db.session.get(Album, album.id).allow_upload is True


def test_album_access_change_needs_a_token(csrf, client, app, album, other_user):
    """Downgrading a guest's access is a state change like any other."""
    with app.app_context():
        db.session.add(AlbumAccess(user_id=other_user.id, album_id=album.id,
                                   access_type='upload'))
        db.session.commit()

    path = f'/album/{album.token}/access/{other_user.id}'
    tokenless = client.post(path, data={'access_type': 'view'})
    assert tokenless.status_code == REFUSED

    token = token_from(client, f'/album/{album.token}')
    accepted = client.post(path, data={'access_type': 'view', 'csrf_token': token})

    assert accepted.status_code == 302
    with app.app_context():
        row = db.session.query(AlbumAccess).filter_by(
            user_id=other_user.id, album_id=album.id).one()
        assert row.access_type == 'view'


def test_login_needs_a_token(csrf, anon_client):
    """The login form is not exempt.

    A forged login is a real attack — it signs the victim into an account the
    attacker controls, so everything they subsequently upload lands where the
    attacker can read it. The 200 on the second half is the login form re-rendered
    with "Invalid username or password": what is being asserted is that the request
    reached the view at all, which the tokenless one did not.
    """
    tokenless = anon_client.post('/login', data={'username': 'alice',
                                                 'password': 'whatever'})
    assert tokenless.status_code == REFUSED

    token = token_from(anon_client, '/login')
    accepted = anon_client.post('/login', data={'username': 'alice',
                                               'password': 'whatever',
                                               'csrf_token': token})
    assert accepted.status_code == 200


def test_invite_acceptance_needs_a_token(csrf, anon_client, app, invite):
    """Account creation, the one POST that mints an identity."""
    anon_client.get(f'/invite/{invite.token}')

    tokenless = anon_client.post('/invite', data={
        'username': 'newcomer',
        'password': 'correct horse battery staple',
        'confirm_password': 'correct horse battery staple',
    })
    assert tokenless.status_code == REFUSED
    with app.app_context():
        assert db.session.query(User).filter_by(username='newcomer').count() == 0

    token = token_from(anon_client, '/invite')
    accepted = anon_client.post('/invite', data={
        'username': 'newcomer',
        'password': 'correct horse battery staple',
        'confirm_password': 'correct horse battery staple',
        'csrf_token': token,
    })
    assert accepted.status_code == 302
    with app.app_context():
        assert db.session.query(User).filter_by(username='newcomer').count() == 1


# ── 2. Admin actions ───────────────────────────────────────────────────────

def test_admin_add_email_is_refused_without_a_token(csrf, admin_client, app, mailer):
    """The account-creation primitive #37 singles out.

    An admin who merely *loads* an attacker's page must not thereby invite an
    address the attacker controls — on an invite-only app that is the difference
    between a closed system and an open one. The empty outbox is the assertion
    that matters: no invite row means no mail, and no mail means no link.
    """
    response = admin_client.post('/admin/email/add',
                                 data={'email': 'attacker@evil.example'})

    assert response.status_code == REFUSED
    with app.app_context():
        assert db.session.query(AllowedEmail).count() == 0
    assert mailer.outbox == []


def test_admin_add_email_succeeds_with_a_token(csrf, admin_client, app, mailer):
    token = token_from(admin_client, '/admin')

    response = admin_client.post('/admin/email/add',
                                 data={'email': 'friend@example.com',
                                       'csrf_token': token})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.query(AllowedEmail).filter_by(
            email='friend@example.com').count() == 1
    assert len(mailer.outbox) == 1


def test_admin_invite_resend_needs_a_token(csrf, admin_client, invite, mailer):
    """Resend is a "make this server send mail" button; forging it is a mail bomb."""
    tokenless = admin_client.post(f'/admin/invite/{invite.id}/resend')
    assert tokenless.status_code == REFUSED
    assert mailer.outbox == []

    token = token_from(admin_client, '/admin')
    accepted = admin_client.post(f'/admin/invite/{invite.id}/resend',
                                 data={'csrf_token': token})
    assert accepted.status_code == 302
    assert len(mailer.outbox) == 1


def test_admin_invite_link_needs_a_token(csrf, admin_client, app, invite):
    """Copy-link rotates the token, so forging it silently kills a live invitation."""
    with app.app_context():
        before = db.session.get(AllowedEmail, invite.id).token_hash

    tokenless = admin_client.post(f'/admin/invite/{invite.id}/link')
    assert tokenless.status_code == REFUSED
    with app.app_context():
        assert db.session.get(AllowedEmail, invite.id).token_hash == before

    token = token_from(admin_client, '/admin')
    accepted = admin_client.post(f'/admin/invite/{invite.id}/link',
                                 data={'csrf_token': token})
    assert accepted.status_code == 302
    with app.app_context():
        assert db.session.get(AllowedEmail, invite.id).token_hash != before


def test_admin_remove_email_needs_a_token(csrf, admin_client, app, invite):
    tokenless = admin_client.post(f'/admin/email/{invite.id}/remove')
    assert tokenless.status_code == REFUSED
    with app.app_context():
        assert db.session.get(AllowedEmail, invite.id) is not None

    token = token_from(admin_client, '/admin')
    accepted = admin_client.post(f'/admin/email/{invite.id}/remove',
                                 data={'csrf_token': token})
    assert accepted.status_code == 302
    with app.app_context():
        assert db.session.get(AllowedEmail, invite.id) is None


def test_admin_delete_album_needs_a_token(csrf, admin_client, album, album_row):
    """The admin route deletes *anyone's* album, so it is the worst one to leave open."""
    tokenless = admin_client.post(f'/admin/album/{album.id}/delete')
    assert tokenless.status_code == REFUSED
    assert album_row(album.id) is True

    token = token_from(admin_client, '/admin')
    accepted = admin_client.post(f'/admin/album/{album.id}/delete',
                                 data={'csrf_token': token})
    assert accepted.status_code == 302
    assert album_row(album.id) is False


# ── 3. In-page fetch — the X-CSRFToken header ──────────────────────────────

def test_photo_delete_fetch_needs_the_header(csrf, client, app, album, photo):
    """``deletePhoto`` in album_view.html sends no body at all, only a header.

    There is nowhere to put a form field on a bodyless POST, which is why the
    header exists — and why the header spelling is part of the contract rather
    than an implementation detail of the extension.
    """
    tokenless = client.post(f'/photo/{photo.id}/delete')
    assert tokenless.status_code == REFUSED
    with app.app_context():
        assert db.session.get(Photo, photo.id) is not None

    token = token_from(client, f'/album/{album.token}')
    accepted = client.post(f'/photo/{photo.id}/delete',
                           headers={HEADER_CSRF: token})

    assert accepted.status_code == 200
    assert accepted.get_json() == {'success': True}
    with app.app_context():
        assert db.session.get(Photo, photo.id) is None


def test_refusal_is_json_for_an_xhr_caller(csrf, client, photo):
    """A rejected token must be readable by the code that hit the wall.

    uploader.js and the gallery fetches parse every response as JSON and surface
    ``body.error``; an HTML error page reaches them as "Upload failed (HTTP 400)",
    which tells the user nothing and hides the one action that fixes it.
    """
    response = client.post(f'/photo/{photo.id}/delete',
                           headers={'X-Requested-With': 'XMLHttpRequest'})

    assert response.status_code == REFUSED
    assert response.is_json
    assert 'reload' in response.get_json()['error'].lower()


def test_refusal_is_html_for_a_browser(csrf, client, album):
    """A form post from a real browser gets the styled error page, not a JSON blob."""
    response = client.post(f'/album/{album.token}/delete',
                           headers={'Accept': 'text/html'})

    assert response.status_code == REFUSED
    assert response.mimetype == 'text/html'


# ── 4. Chunked uploads — header on a raw octet-stream body ─────────────────

def test_chunked_upload_works_end_to_end_with_the_token(csrf, client, album,
                                                        multi_chunk_jpeg, photos):
    """The whole init -> chunks -> complete dance, token-carrying, unchanged.

    Driven through ``ProtocolClient`` rather than by hand so this is the real wire
    contract from docs/upload_protocol.md and not a convenient approximation of it.
    """
    token = token_from(client, f'/album/{album.token}')
    protocol = ProtocolClient(client, album.token, csrf_token=token)

    init, chunks, complete = protocol.upload('holiday.jpg', multi_chunk_jpeg,
                                             TEST_CHUNK_SIZE)

    assert init.status_code == 201
    assert [c.status_code for c in chunks] == [200] * len(chunks)
    assert complete.status_code == 200
    assert complete.get_json()['results'][0]['success'] is True
    assert len(photos(album.id)) == 1


def test_chunk_without_the_header_is_refused(csrf, client, album, multi_chunk_jpeg,
                                             session_row):
    """A chunk is ``application/octet-stream`` — the token can only be a header.

    The session is opened *with* a token so what is under test is the chunk request
    alone, and the byte cursor is asserted unmoved: a refusal that has already
    appended the body to the ``.part`` file would be a refusal in name only.
    """
    token = token_from(client, f'/album/{album.token}')
    protocol = ProtocolClient(client, album.token, csrf_token=token)
    init = protocol.init_for('holiday.jpg', multi_chunk_jpeg)
    upload_id = init.get_json()['upload_id']

    tokenless = ProtocolClient(client, album.token)
    response = tokenless.chunk(upload_id, 0, multi_chunk_jpeg[:TEST_CHUNK_SIZE])

    assert response.status_code == REFUSED
    assert session_row(upload_id).received_bytes == 0


def test_init_and_complete_need_the_header(csrf, client, album, small_jpeg,
                                           session_row):
    """``init`` is JSON and ``complete`` is empty; neither can carry a form field."""
    tokenless = ProtocolClient(client, album.token)
    assert tokenless.init_for('holiday.jpg', small_jpeg).status_code == REFUSED

    token = token_from(client, f'/album/{album.token}')
    protocol = ProtocolClient(client, album.token, csrf_token=token)
    init = protocol.init_for('holiday.jpg', small_jpeg)
    upload_id = init.get_json()['upload_id']
    protocol.send_chunks(upload_id, small_jpeg, TEST_CHUNK_SIZE)

    assert tokenless.complete(upload_id).status_code == REFUSED
    # Still on disk, still resumable: the refusal committed nothing.
    assert session_row(upload_id).received_bytes == len(small_jpeg)
    assert protocol.complete(upload_id).status_code == 200


def test_legacy_single_request_upload_needs_the_token(csrf, client, album,
                                                      small_jpeg, photos):
    """The pre-chunking multipart path is a POST like any other.

    Sent as a header rather than as an extra multipart field, matching what
    ``_uploadOne`` in uploader.js does — one place to look when a 400 turns up,
    whichever transport produced it.
    """
    import io

    def _files():
        return {'files': (io.BytesIO(small_jpeg), 'holiday.jpg')}

    tokenless = client.post(f'/share/{album.token}/upload', data=_files(),
                            content_type='multipart/form-data')
    assert tokenless.status_code == REFUSED
    assert photos(album.id) == []

    token = token_from(client, f'/album/{album.token}')
    accepted = client.post(f'/share/{album.token}/upload', data=_files(),
                           content_type='multipart/form-data',
                           headers={HEADER_CSRF: token})

    assert accepted.status_code == 200
    assert accepted.get_json()['results'][0]['success'] is True
    assert len(photos(album.id)) == 1


def test_a_rejected_chunk_costs_no_rate_budget(csrf, client, album,
                                               multi_chunk_jpeg):
    """A stale token must not be able to lock a user out of uploading.

    The chunk endpoint charges its 600/hour budget on every non-200 (§8 of
    docs/upload_protocol.md), so this pairing matters: if a CSRF rejection were
    charged, one page left open past a logout would spend the whole budget on
    retries and then keep the user out for an hour *after* they reloaded and
    fixed the actual problem. The refusal that costs nothing is the one that is
    recoverable by reloading.

    It falls out of ordering rather than from a decision anyone wrote down —
    CSRFProtect aborts before the limiter's deferred deduction can see the
    response — so it is pinned here, because an upgrade to either extension could
    reverse it silently. The asymmetry is also safe in the direction that matters:
    a rejected chunk is refused on its headers, so unlike a 409 it never reads the
    8 MiB body the budget exists to protect.
    """
    from pixelvault.extensions import limiter

    token = token_from(client, f'/album/{album.token}')
    protocol = ProtocolClient(client, album.token, csrf_token=token)
    upload_id = protocol.init_for('holiday.jpg', multi_chunk_jpeg).get_json()['upload_id']
    tokenless = ProtocolClient(client, album.token)

    def charged():
        return sum(count for key, count in dict(limiter.storage.storage).items()
                   if 'upload_chunk' in key)

    before = charged()
    for _ in range(3):
        assert tokenless.chunk(upload_id, 0, b'x' * 1024).status_code == REFUSED
    assert charged() == before

    # ...whereas an offset mismatch, the cheap refusal it is most like, is charged.
    for _ in range(3):
        assert protocol.chunk(upload_id, 999_999, b'x' * 1024).status_code == 409
    assert charged() == before + 3


# ── 5. The fire-and-forget DELETE ──────────────────────────────────────────

def test_cancel_still_releases_quota_with_the_token(csrf, client, album,
                                                    multi_chunk_jpeg, session_row):
    """``cancel`` is the one caller whose failure would be invisible.

    Nothing reads its response — ``_cancelSession`` fires it and forgets — so a
    rejected token here would not surface as an error anywhere. It would surface a
    day later as a user unable to start new uploads, their in-flight byte quota
    still reserved by sessions they thought they had removed. That is why it is
    tested against the row rather than against the status code.
    """
    token = token_from(client, f'/album/{album.token}')
    protocol = ProtocolClient(client, album.token, csrf_token=token)
    upload_id = protocol.init_for('holiday.jpg', multi_chunk_jpeg).get_json()['upload_id']

    response = protocol.cancel(upload_id)

    assert response.status_code == 200
    assert response.get_json() == {'cancelled': True}
    assert session_row(upload_id) is None


def test_cancel_without_a_token_is_refused(csrf, client, album, multi_chunk_jpeg,
                                           session_row):
    """DELETE is state-changing, so CSRFProtect checks it exactly like a POST."""
    token = token_from(client, f'/album/{album.token}')
    protocol = ProtocolClient(client, album.token, csrf_token=token)
    upload_id = protocol.init_for('holiday.jpg', multi_chunk_jpeg).get_json()['upload_id']

    response = ProtocolClient(client, album.token).cancel(upload_id)

    assert response.status_code == REFUSED
    assert session_row(upload_id) is not None


# ── 6. Reads are untouched ─────────────────────────────────────────────────

@pytest.mark.parametrize('path', [
    '/dashboard',
    '/api/dashboard/filter-options',
])
def test_get_requests_need_no_token(csrf, client, path):
    """Safe methods are not checked, and must not start being.

    Every gallery fetch, the dashboard filter probe and the resume ``status`` call
    are GETs. Requiring a token on them would buy nothing — a GET that changes
    state is the bug, not the missing token — and would break every one of them.
    """
    assert client.get(path).status_code == 200


def test_status_probe_needs_no_token(csrf, client, album, multi_chunk_jpeg):
    """The resume probe in ``_prepareResume`` is a GET and stays one."""
    token = token_from(client, f'/album/{album.token}')
    protocol = ProtocolClient(client, album.token, csrf_token=token)
    upload_id = protocol.init_for('holiday.jpg', multi_chunk_jpeg).get_json()['upload_id']

    assert ProtocolClient(client, album.token).status(upload_id).status_code == 200


# ── 7. Policy sweeps — what protects the routes nobody has written yet ─────

_URL_PARAM = re.compile(r'<[^>]+>')


def _mutating_rules(app):
    """Every rule in the live url_map that accepts a state-changing method."""
    for rule in app.url_map.iter_rules():
        methods = rule.methods - {'GET', 'HEAD', 'OPTIONS'}
        if methods:
            yield rule, sorted(methods)[0]


def _concrete_path(rule):
    """Fill a rule's converters with placeholders that satisfy them.

    Built by substitution on ``rule.rule`` rather than through the map adapter,
    which validates every value against its converter and would have to be taught
    each converter the app ever adds. The sweep needs a URL that *routes*, not one
    that resolves: CSRF is checked before the view runs, so a nonexistent id is
    refused for its missing token, never for being nonexistent.
    """
    def placeholder(match):
        return '1' if match.group(0).startswith('<int:') else 'x'

    return _URL_PARAM.sub(placeholder, rule.rule)


def test_every_mutating_route_refuses_a_tokenless_request(csrf, admin_client, app):
    """The sweep that covers the route somebody adds next year.

    Protection is a ``before_request`` hook over the whole app rather than a
    decorator per view, precisely so a new route is covered by default. This
    asserts that of the url_map as it actually is, so the day someone reaches for
    ``@csrf.exempt`` — or registers a blueprint that skips the hook — a test fails
    instead of a hole opening quietly.

    Driven as an admin so nothing is refused for a reason other than the token; the
    URLs are built from placeholders and need not resolve to real rows, because the
    check happens before the view is entered.
    """
    unprotected = []

    for rule, method in _mutating_rules(app):
        path = _concrete_path(rule)
        response = admin_client.open(path, method=method)
        if response.status_code != REFUSED:
            unprotected.append(f'{method} {path} ({rule.endpoint}) '
                               f'-> {response.status_code}')

    assert unprotected == []


def test_the_endpoints_named_in_the_issue_are_all_still_mutating(app):
    """A rename must not be able to drop an endpoint out of the sweep above.

    The sweep is written against the url_map, so it protects whatever is there —
    including, silently, a much shorter list than #37's if a route were renamed or
    lost. This pins the names.
    """
    found = {rule.endpoint for rule, _ in _mutating_rules(app)}

    assert ISSUE_37_ENDPOINTS <= found, ISSUE_37_ENDPOINTS - found


_FORM_TAG = re.compile(r'<form\b[^>]*>', re.IGNORECASE | re.DOTALL)
_POST_METHOD = re.compile(r'method\s*=\s*["\']post["\']', re.IGNORECASE)


def test_every_post_form_in_the_templates_renders_a_token(app):
    """Read against the templates as source, not against rendered pages.

    Several forms live behind ``{% if %}`` branches — the admin panel's per-row
    actions, the album danger zone — so a rendered-output test would cover only the
    branches a fixture happens to take, and a new form added inside an unrendered
    branch would sail through. Scanning the source covers every branch at once and
    fails at the moment the form is written rather than the moment it is first
    forged.
    """
    from pathlib import Path

    missing = []
    for path in sorted(Path(app.template_folder).glob('*.html')):
        source = path.read_text()
        for match in _FORM_TAG.finditer(source):
            if not _POST_METHOD.search(match.group(0)):
                continue
            end = source.find('</form>', match.end())
            body = source[match.end():end if end != -1 else len(source)]
            if 'csrf_token()' not in body:
                line = source.count('\n', 0, match.start()) + 1
                missing.append(f'{path.name}:{line}')

    assert missing == []
