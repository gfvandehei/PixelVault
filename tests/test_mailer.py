"""Transport-level tests for ``src/pixelvault/mailer.py``.

**No test here opens a socket.** ``smtplib.SMTP`` and ``smtplib.SMTP_SSL`` are
replaced with a recorder that captures the arguments the real class would have
been constructed with, which is what makes the timeout assertion possible at all:
the timeout is a constructor argument, so the only place to observe it is the
constructor. A test that actually connected could only observe it by hanging.

Configuration comes from ``_TEST_ENV`` in ``conftest.py`` (read at import time —
see that module's docstring). The selection tests are the one exception: they
rebind the constants on ``pixelvault.mailer`` itself, which is legitimate because
``build_mailer`` reads them as module globals *at call time*, not as defaults
bound at ``def``.
"""

import smtplib

import pytest

import pixelvault
from pixelvault import mailer as mailer_module
from pixelvault.mailer import (
    ConsoleMailer,
    MailError,
    MemoryMailer,
    NullMailer,
    SMTPMailer,
    build_mailer,
)

from .conftest import TEST_MAIL_TIMEOUT_SECONDS


def make_message(to="invitee@example.com", subject="You have been invited to PixelVault",
                 text="Open https://vault.test.invalid/invite/SECRET-TOKEN",
                 html="<p>Open <a href='https://vault.test.invalid/invite/SECRET-TOKEN'>here</a></p>"):
    """A two-part invite-shaped message, so assertions match what emails.py will send."""
    from email.message import EmailMessage

    message = EmailMessage()
    message['To'] = to
    message['From'] = 'pixelvault@test.invalid'
    message['Subject'] = subject
    message.set_content(text)
    message.add_alternative(html, subtype='html')
    return message


class RecordingSMTP:
    """Stand-in for ``smtplib.SMTP`` that records the conversation instead of having one."""

    instances = []

    def __init__(self, host=None, port=None, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.started_tls = False
        self.login_args = None
        self.sent = []
        RecordingSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def ehlo(self):
        pass

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture
def recording_smtp(monkeypatch):
    """Replace both smtplib entry points with :class:`RecordingSMTP` and hand back the class."""
    RecordingSMTP.instances = []
    monkeypatch.setattr(smtplib, 'SMTP', RecordingSMTP)
    monkeypatch.setattr(smtplib, 'SMTP_SSL', RecordingSMTP)
    return RecordingSMTP


# ── build_mailer() selection ───────────────────────────────────────────────

def test_build_mailer_defaults_to_console_without_smtp_host(monkeypatch):
    """No relay configured is a working dev checkout, not an error."""
    monkeypatch.setattr(mailer_module, 'MAIL_ENABLED', True)
    monkeypatch.setattr(mailer_module, 'SMTP_HOST', '')
    assert isinstance(build_mailer(), ConsoleMailer)


def test_build_mailer_uses_smtp_when_host_is_set(monkeypatch):
    monkeypatch.setattr(mailer_module, 'MAIL_ENABLED', True)
    monkeypatch.setattr(mailer_module, 'SMTP_HOST', 'smtp.example.com')
    monkeypatch.setattr(mailer_module, 'SMTP_PORT', 465)
    monkeypatch.setattr(mailer_module, 'SMTP_SECURITY', 'ssl')

    backend = build_mailer()

    assert isinstance(backend, SMTPMailer)
    assert (backend.host, backend.port, backend.security) == ('smtp.example.com', 465, 'ssl')


def test_mail_disabled_beats_a_configured_relay(monkeypatch):
    """MAIL_ENABLED=false is an instruction, so it must outrank leftover SMTP settings.

    The realistic case is an operator who turns sending off on a host whose .env
    still carries working credentials; if host-set won, the switch would do nothing.
    """
    monkeypatch.setattr(mailer_module, 'MAIL_ENABLED', False)
    monkeypatch.setattr(mailer_module, 'SMTP_HOST', 'smtp.example.com')
    assert isinstance(build_mailer(), NullMailer)


def test_smtp_mailer_rejects_an_unknown_security_mode():
    with pytest.raises(ValueError):
        SMTPMailer('smtp.example.com', security='tls-ish')


# ── SMTPMailer behaviour ───────────────────────────────────────────────────

def test_timeout_reaches_the_socket(recording_smtp):
    """The configured timeout must land on the connection, not just sit in config.

    This is the assertion that keeps a hung relay from pinning one of only eight
    Gunicorn threads: without the argument, smtplib blocks on the OS default,
    which is effectively forever.
    """
    SMTPMailer('smtp.example.com', port=587, timeout=TEST_MAIL_TIMEOUT_SECONDS).send(make_message())

    assert recording_smtp.instances[0].timeout == TEST_MAIL_TIMEOUT_SECONDS


def test_starttls_upgrades_before_authenticating(recording_smtp, monkeypatch):
    """Credentials must never cross a plaintext socket."""
    calls = []
    monkeypatch.setattr(recording_smtp, 'starttls', lambda self, context=None: calls.append('starttls'))
    monkeypatch.setattr(recording_smtp, 'login', lambda self, u, p: calls.append('login'))

    SMTPMailer('smtp.example.com', username='bot', password='pw', security='starttls').send(make_message())

    assert calls == ['starttls', 'login']


def test_ssl_mode_skips_starttls(recording_smtp):
    """Implicit TLS is encrypted from the first byte; there is nothing to upgrade."""
    SMTPMailer('smtp.example.com', port=465, security='ssl').send(make_message())

    assert recording_smtp.instances[0].started_tls is False


def test_no_login_without_credentials(recording_smtp):
    """A blank username means an unauthenticated relay, not an empty-string login."""
    SMTPMailer('smtp.example.com', security='none').send(make_message())

    assert recording_smtp.instances[0].login_args is None


@pytest.mark.parametrize('failure', [
    smtplib.SMTPAuthenticationError(535, b'auth failed'),
    smtplib.SMTPRecipientsRefused({'invitee@example.com': (550, b'no such user')}),
    smtplib.SMTPServerDisconnected('connection reset'),
    TimeoutError('timed out'),
    ConnectionRefusedError('nothing listening'),
])
def test_every_transport_fault_becomes_a_mail_error(recording_smtp, monkeypatch, failure):
    """Callers catch MailError and nothing else; a raw smtplib fault escaping is the bug."""
    def explode(self, message):
        raise failure
    monkeypatch.setattr(recording_smtp, 'send_message', explode)

    with pytest.raises(MailError):
        SMTPMailer('smtp.example.com', security='none').send(make_message())


def test_mail_error_chains_the_original_fault(recording_smtp, monkeypatch):
    """The relay's own words survive on __cause__, so a failure is still diagnosable."""
    def explode(self, message):
        raise smtplib.SMTPAuthenticationError(535, b'Application-specific password required')
    monkeypatch.setattr(recording_smtp, 'send_message', explode)

    with pytest.raises(MailError) as caught:
        SMTPMailer('smtp.example.com', security='none').send(make_message())

    assert isinstance(caught.value.__cause__, smtplib.SMTPAuthenticationError)


def test_smtp_mailer_never_logs_the_body(recording_smtp, caplog):
    """The invite token lives in the body; a copy in a log file is a copy of the credential."""
    caplog.set_level('DEBUG')

    SMTPMailer('smtp.example.com', security='none').send(make_message())

    assert 'SECRET-TOKEN' not in caplog.text
    assert 'invitee@example.com' in caplog.text  # recipient is fine, and useful


def test_failed_send_does_not_log_the_body(recording_smtp, monkeypatch, caplog):
    """Especially not on the error path, which is the one that reaches a bug tracker."""
    caplog.set_level('DEBUG')

    def explode(self, message):
        raise smtplib.SMTPDataError(554, b'rejected')
    monkeypatch.setattr(recording_smtp, 'send_message', explode)

    with pytest.raises(MailError) as caught:
        SMTPMailer('smtp.example.com', security='none').send(make_message())

    assert 'SECRET-TOKEN' not in caplog.text
    assert 'SECRET-TOKEN' not in str(caught.value)


# ── The other backends ─────────────────────────────────────────────────────

def test_console_mailer_renders_the_link(caplog):
    """ConsoleMailer's console *is* the mailbox — an unclickable link makes it useless."""
    caplog.set_level('DEBUG')

    ConsoleMailer().send(make_message())

    assert 'SECRET-TOKEN' in caplog.text


def test_null_mailer_discards_without_raising():
    assert NullMailer().send(make_message()) is None


def test_memory_mailer_captures_recipient_subject_and_both_parts():
    backend = MemoryMailer()
    backend.send(make_message())

    assert len(backend.outbox) == 1
    message = backend.outbox[0]
    assert message['To'] == 'invitee@example.com'
    assert message['Subject'] == 'You have been invited to PixelVault'
    assert message.get_content_type() == 'multipart/alternative'
    parts = {p.get_content_type(): p.get_content() for p in message.iter_parts()}
    assert 'SECRET-TOKEN' in parts['text/plain']
    assert 'SECRET-TOKEN' in parts['text/html']


# ── The extensions proxy ───────────────────────────────────────────────────

def test_app_gets_a_backend_at_boot(app):
    """create_app() must leave a usable mailer behind, as it does for db and limiter."""
    from pixelvault.mailer import Mailer

    assert isinstance(app.extensions['mailer'], Mailer)


def test_proxy_delegates_to_the_swapped_in_backend(app, mailer):
    """The `mailer` fixture is what every later invite test asserts against."""
    from pixelvault.extensions import mailer as proxy

    with app.app_context():
        proxy.send(make_message())
        assert proxy.backend is mailer

    assert len(mailer.outbox) == 1


# ── Boot-time configuration validation ─────────────────────────────────────

@pytest.fixture
def mail_config(monkeypatch):
    """Rebind the mail constants ``_validate_mail_config`` reads, for one test.

    They live on the ``pixelvault`` package namespace, not on ``config``, because
    ``__init__.py`` pulls them in with ``from .config import *`` — so that is where
    the function resolves them.
    """
    def _set(**values):
        for name, value in values.items():
            monkeypatch.setattr(pixelvault, name, value)
    return _set


def test_unconfigured_mail_still_boots(mail_config):
    """A dev checkout with no relay must not be blocked from starting."""
    mail_config(MAIL_ENABLED=True, SMTP_HOST='', MAIL_FROM='', PUBLIC_BASE_URL='')
    pixelvault._validate_mail_config()


def test_smtp_without_mail_from_fails_loudly(mail_config):
    mail_config(MAIL_ENABLED=True, SMTP_HOST='smtp.example.com',
                SMTP_SECURITY='starttls', MAIL_FROM='', PUBLIC_BASE_URL='https://vault.test.invalid')

    with pytest.raises(RuntimeError, match='MAIL_FROM'):
        pixelvault._validate_mail_config()


def test_smtp_without_public_base_url_fails_loudly(mail_config):
    """Otherwise the invite arrives carrying a link to nowhere — a failure at read time, hours later."""
    mail_config(MAIL_ENABLED=True, SMTP_HOST='smtp.example.com',
                SMTP_SECURITY='starttls', MAIL_FROM='bot@example.com', PUBLIC_BASE_URL='')

    with pytest.raises(RuntimeError, match='PUBLIC_BASE_URL'):
        pixelvault._validate_mail_config()


def test_bad_security_mode_fails_loudly(mail_config):
    mail_config(MAIL_ENABLED=True, SMTP_HOST='smtp.example.com', SMTP_SECURITY='tls',
                MAIL_FROM='bot@example.com', PUBLIC_BASE_URL='https://vault.test.invalid')

    with pytest.raises(RuntimeError, match='SMTP_SECURITY'):
        pixelvault._validate_mail_config()


def test_disabled_mail_is_never_incoherent(mail_config):
    """MAIL_ENABLED=false sends nothing, so half-filled SMTP settings cannot hurt anyone."""
    mail_config(MAIL_ENABLED=False, SMTP_HOST='smtp.example.com', SMTP_SECURITY='nonsense',
                MAIL_FROM='', PUBLIC_BASE_URL='')
    pixelvault._validate_mail_config()


# ── Config normalisation ───────────────────────────────────────────────────

def test_public_base_url_has_no_trailing_slash():
    """Link building downstream concatenates a path; a trailing slash would double it."""
    from pixelvault.config import PUBLIC_BASE_URL

    assert not PUBLIC_BASE_URL.endswith('/')
