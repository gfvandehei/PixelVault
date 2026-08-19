"""What an invitee actually receives — ``pixelvault.emails``.

The module under test is the only one that knows an invite has wording, so these
tests read the message the way a mail client would: part order, headers, and the
bytes of each body. Three of them are not about copy at all and are the reason
this file exists:

* **The text part must come first.** ``multipart/alternative`` means the client
  picks the *last* part it can render, so ordering is the mechanism that makes the
  HTML disposable. If a future edit swaps the two calls, the guaranteed-readable
  copy becomes the fallback for a client that cannot read HTML — and nothing else
  in the suite would notice.
* **The link's origin comes from configuration, never from the request.** One test
  builds the message inside a request context carrying a hostile ``Host`` header
  and asserts the link still points at ``TEST_PUBLIC_BASE_URL``. That is the whole
  point of design §6, and it is only observable here.
* **The token never reaches a log record**, on the success path *and* the failure
  path — the failure path being the one whose output ends up in a bug report.

Configuration comes from ``_TEST_ENV`` in ``conftest.py``, read at import time:
``TEST_PUBLIC_BASE_URL`` is the origin every link below is checked against, and
``TEST_INVITE_TTL_HOURS`` is 72, which is why the expiry reads "about 3 days".
"""

import logging

import pytest

from pixelvault import emails, invites
from pixelvault.extensions import db, mailer as mailer_proxy
from pixelvault.mailer import MailError, Mailer
from pixelvault.models import InviteState

from tests.conftest import TEST_MAIL_FROM, TEST_PUBLIC_BASE_URL


@pytest.fixture
def session(app):
    """The ORM session, with an app context — ``render_template`` needs one too."""
    with app.app_context():
        yield db.session


@pytest.fixture
def invite_and_token(session):
    """A freshly issued invite and its plaintext token, exactly as a route holds them."""
    return invites.issue(session, "invitee@example.com")


class ExplodingMailer(Mailer):
    """A transport that always refuses, the way a relay rejecting the envelope does.

    Its message deliberately looks like a real SMTP complaint: ``last_send_error``
    is rendered back to the admin, so the assertion that it is stored verbatim is
    an assertion about what the panel will say.
    """

    message = "SMTP delivery failed via smtp.example.com:587: [Errno 111] Connection refused"

    def send(self, message):
        raise MailError(self.message)


def parts_of(message):
    """Return the message's parts in wire order, as (content_type, body) pairs."""
    return [(part.get_content_type(), part.get_content()) for part in message.iter_parts()]


# ── Structure ──────────────────────────────────────────────────────────────

def test_message_is_multipart_alternative_with_text_first(invite_and_token):
    """Part order is the contract: the HTML is allowed to be broken, the text is not."""
    invite, token = invite_and_token

    message = emails.build_invite_email(invite, token)

    assert message.get_content_type() == 'multipart/alternative'
    assert [content_type for content_type, _ in parts_of(message)] == ['text/plain', 'text/html']


def test_both_parts_carry_the_invite_url(invite_and_token):
    """A part without the link is a part that cannot complete the invite."""
    invite, token = invite_and_token
    expected = f"{TEST_PUBLIC_BASE_URL}/invite/{token}"

    message = emails.build_invite_email(invite, token)

    for content_type, body in parts_of(message):
        assert expected in body, f"{content_type} part is missing the invite link"


def test_html_part_shows_the_url_as_text_not_only_as_a_link(invite_and_token):
    """Some clients and gateways rewrite or strip anchors; the plain URL survives that.

    Asserted by finding the URL *outside* an ``href`` — i.e. as visible body text —
    because an occurrence only inside the button's attribute would not be readable
    once the anchor is mangled.
    """
    invite, token = invite_and_token
    expected = f"{TEST_PUBLIC_BASE_URL}/invite/{token}"

    message = emails.build_invite_email(invite, token)
    html = dict(parts_of(message))['text/html']

    assert f">{expected}<" in html


# ── Where the link points ──────────────────────────────────────────────────

def test_url_is_base_url_plus_invite_path(invite_and_token):
    """Exactly ``<base_url>/invite/<token>`` — the path the acceptance route claims."""
    invite, token = invite_and_token

    message = emails.build_invite_email(invite, token)
    text = dict(parts_of(message))['text/plain']

    assert f"{TEST_PUBLIC_BASE_URL}/invite/{token}" in text


def test_base_url_argument_is_the_only_origin(invite_and_token):
    """Passing a different origin moves the link, so nothing else is contributing one."""
    invite, token = invite_and_token

    message = emails.build_invite_email(invite, token, base_url="https://elsewhere.example")
    text = dict(parts_of(message))['text/plain']

    assert f"https://elsewhere.example/invite/{token}" in text
    assert TEST_PUBLIC_BASE_URL not in text


def test_a_hostile_host_header_cannot_steer_the_link(app, invite_and_token):
    """The attack design §6 exists to close, asserted rather than argued.

    An admin adding an email is an ordinary HTTP request, and behind
    Cloudflare -> nginx the ``Host`` on it is attacker-supplied. Were the link
    reconstructed from the request — ``url_for(_external=True)`` — this message
    would invite a real person to a domain the attacker controls, from a genuine
    sending address. Building from configuration is what makes the header inert.
    """
    invite, token = invite_and_token

    with app.test_request_context('/admin/email/add',
                                  headers={'Host': 'phish.example'},
                                  base_url='https://phish.example'):
        message = emails.build_invite_email(invite, token)

    for _, body in parts_of(message):
        assert 'phish.example' not in body
        assert f"{TEST_PUBLIC_BASE_URL}/invite/{token}" in body


def test_no_expiry_on_the_row_is_refused(session, invite_and_token):
    """A link that cannot say when it dies is the silent failure this email prevents."""
    invite, token = invite_and_token
    invite.expires_at = None

    with pytest.raises(ValueError):
        emails.build_invite_email(invite, token)


# ── Headers a client will show ─────────────────────────────────────────────

def test_headers_are_what_the_client_displays(invite_and_token):
    invite, token = invite_and_token

    message = emails.build_invite_email(invite, token)

    assert message['To'] == 'invitee@example.com'
    assert message['From'] == f"PixelVault <{TEST_MAIL_FROM}>"
    assert message['Subject'] == 'You have been invited to PixelVault'


def test_date_and_message_id_are_present(invite_and_token):
    """Both are absent-header spam signals, and an invite must not land in spam.

    The Message-ID's domain is the sender's rather than the host's own name, which
    ``make_msgid()`` would otherwise leak into every recipient's mailbox.
    """
    invite, token = invite_and_token

    message = emails.build_invite_email(invite, token)

    assert message['Date']
    assert message['Message-ID'].endswith(f"@{TEST_MAIL_FROM.split('@')[1]}>")


# ── The copy itself ────────────────────────────────────────────────────────

def test_expiry_is_stated_in_both_parts(invite_and_token):
    """An invite that silently stops working is the support ticket this feature prevents.

    Both halves are asserted: the absolute instant, labelled UTC because an
    unlabelled time is wrong rather than merely vague for a reader in another zone,
    and the coarse "about 3 days" that is the part people actually read. Three days
    because ``TEST_INVITE_TTL_HOURS`` is 72.
    """
    invite, token = invite_and_token
    expires_at = invite.expires_at

    message = emails.build_invite_email(invite, token)

    for content_type, body in parts_of(message):
        assert f"{expires_at.day} {expires_at:%B %Y}" in body, content_type
        assert f"{expires_at:%H:%M} UTC" in body, content_type
        assert 'about 3 days from now' in body, content_type


def test_copy_states_the_invite_is_single_use_and_bound_to_the_address(invite_and_token):
    """Two facts an invitee needs before clicking, in both parts."""
    invite, token = invite_and_token

    message = emails.build_invite_email(invite, token)

    for content_type, body in parts_of(message):
        assert 'once' in body, content_type
        assert 'invitee@example.com' in body, content_type


def test_a_suggested_username_is_mentioned_only_when_there_is_one(session):
    """``prefill_username`` is optional; an empty one must not leave a dangling sentence."""
    with_name, token = invites.issue(session, "named@example.com", prefill_username="jamie")
    without_name, other_token = invites.issue(session, "plain@example.com")

    named = dict(parts_of(emails.build_invite_email(with_name, token)))
    plain = dict(parts_of(emails.build_invite_email(without_name, other_token)))

    assert 'jamie' in named['text/plain'] and 'jamie' in named['text/html']
    assert 'suggested' not in plain['text/plain']
    assert 'suggested' not in plain['text/html']


def test_html_carries_no_remote_assets_or_script(invite_and_token):
    """Images, web fonts and script are blocked, stripped, or both — so there are none."""
    invite, token = invite_and_token

    html = dict(parts_of(emails.build_invite_email(invite, token)))['text/html']

    assert '<img' not in html
    assert '<script' not in html
    assert '<style' not in html          # Gmail strips these; every rule is inline instead
    assert 'src=' not in html
    assert 'fonts.googleapis.com' not in html


# ── send_invite: the row is stamped either way ─────────────────────────────

def test_send_invite_sends_through_the_configured_backend(session, mailer, invite_and_token):
    """Routes will hand it the ``extensions.mailer`` proxy; that path is the one tested."""
    invite, token = invite_and_token

    emails.send_invite(mailer_proxy, session, invite, token)

    assert len(mailer.outbox) == 1
    assert mailer.outbox[0]['To'] == 'invitee@example.com'


def test_success_stamps_the_row_and_clears_a_previous_failure(session, mailer, invite_and_token):
    """A resend that works must move the row out of SEND_FAILED.

    Without the clear, one bad relay day leaves the panel telling the admin to keep
    retrying a delivery that already succeeded.
    """
    invite, token = invite_and_token
    invites.mark_sent(session, invite, error="550 5.1.1 user unknown")
    assert invite.state is InviteState.SEND_FAILED
    before = invite.send_count

    emails.send_invite(mailer, session, invite, token)

    assert invite.last_sent_at is not None
    assert invite.send_count == before + 1
    assert invite.last_send_error == ''
    assert invite.state is InviteState.SENT


def test_failure_records_the_error_bumps_the_count_and_re_raises(session, invite_and_token):
    """``mark_sent`` runs on the failure path too — design §13 calls this out by name.

    Skipping it there would leave ``send_count`` undercounting *and* leave
    ``last_send_error`` empty, so a message that never left the process would read
    as SENT in the admin panel: the failure mode is an invitee waiting for mail
    nobody can see was lost.
    """
    invite, token = invite_and_token
    before = invite.send_count

    with pytest.raises(MailError):
        emails.send_invite(ExplodingMailer(), session, invite, token)

    assert invite.send_count == before + 1
    assert invite.last_sent_at is not None
    assert invite.last_send_error == ExplodingMailer.message
    assert invite.state is InviteState.SEND_FAILED


def test_failure_leaves_the_token_usable(session, invite_and_token):
    """Delivery failed; the credential did not. The copy-link fallback depends on this."""
    invite, token = invite_and_token

    with pytest.raises(MailError):
        emails.send_invite(ExplodingMailer(), session, invite, token)

    assert invites.validate(session, token).id == invite.id


def test_a_long_relay_error_is_truncated_to_the_column(session, invite_and_token):
    """``last_send_error`` is ``String(256)`` and SQLite would not complain until Postgres did."""
    invite, token = invite_and_token

    class Verbose(Mailer):
        def send(self, message):
            raise MailError('x' * 1000)

    with pytest.raises(MailError):
        emails.send_invite(Verbose(), session, invite, token)

    assert len(invite.last_send_error) == invites.MAX_SEND_ERROR_LEN


# ── The token stays out of the logs ────────────────────────────────────────

def test_a_successful_send_never_logs_the_token(session, mailer, invite_and_token, caplog):
    """The token creates an account bound to a real address; a log copy is a credential copy."""
    invite, token = invite_and_token
    caplog.set_level(logging.DEBUG)

    emails.send_invite(mailer, session, invite, token)

    assert caplog.text                      # something really was logged
    assert token not in caplog.text
    assert 'invitee@example.com' in caplog.text   # the address is fine, and useful


def test_a_failed_send_never_logs_the_token_or_the_body(session, invite_and_token, caplog):
    """The path whose output reaches a bug tracker is the one that must be cleanest."""
    invite, token = invite_and_token
    caplog.set_level(logging.DEBUG)

    with pytest.raises(MailError) as caught:
        emails.send_invite(ExplodingMailer(), session, invite, token)

    assert caplog.text
    assert token not in caplog.text
    assert token not in str(caught.value)
    assert 'Create your account' not in caplog.text   # nor any of the body
