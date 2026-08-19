"""Outbound mail transport — the app's only side effect that leaves the process.

This module knows how to put an ``EmailMessage`` on the wire and nothing else. It
has never heard of invites, users or albums, which is what lets the next feature
that needs email (password reset, notifications) reuse it unchanged, and what
keeps a relay migration to a provider's HTTP API down to one new ``Mailer``
subclass plus one line in :func:`build_mailer`.

Composition of the message lives in ``emails.py``; the lifecycle that decides a
message is warranted lives in ``invites.py``. Nothing here flashes, aborts, or
retries — every failure surfaces as :class:`MailError` and the caller decides.

**Nothing in this module may log a message body.** An invite token is a bearer
credential that creates an account bound to a real person's email address, and it
travels in the body; a copy of it in a log file, an aggregator, or a crash report
is a copy of the credential. Recipient and subject are the most any log line here
carries. :class:`ConsoleMailer` is the one deliberate exception, and only because
its console *is* the mailbox — see its docstring.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import (
    MAIL_ENABLED,
    MAIL_TIMEOUT_SECONDS,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_SECURITY,
    SMTP_USERNAME,
)

logger = logging.getLogger(__name__)

#: Accepted values for ``SMTP_SECURITY``. Validated at construction rather than at
#: send time so a typo in ``.env`` is a boot failure, not a failed invite months later.
SECURITY_MODES = ('starttls', 'ssl', 'none')


class MailError(RuntimeError):
    """Delivery failed. Wraps every ``smtplib``/socket fault this module can hit.

    Callers are expected to catch this and nothing else — a raw ``SMTPException``
    or ``OSError`` escaping would push relay-specific error handling up into the
    routes, which is exactly the coupling this module exists to prevent.
    """


class Mailer:
    """Interface every transport implements: hand it a message, it delivers or raises."""

    def send(self, message: EmailMessage) -> None:
        """Deliver ``message``, or raise :class:`MailError`."""
        raise NotImplementedError


class SMTPMailer(Mailer):
    """Sends over SMTP. Relay-agnostic: host, port, credentials and TLS mode all come from config.

    Gmail, Postmark and a relay on localhost differ only in those values, so
    switching between them is an ``.env`` edit and no code change. Keep it that
    way — nothing above :func:`build_mailer` should be able to tell which relay is
    in use.

    A fresh connection per send. Invites are rare and admin-triggered, so pooling
    would trade a real risk (a stale socket surfacing as a failed invite the admin
    has to diagnose) for throughput this app has no use for.
    """

    def __init__(self, host, port=587, username='', password='',
                 security='starttls', timeout=MAIL_TIMEOUT_SECONDS):
        if security not in SECURITY_MODES:
            raise ValueError(
                f"SMTP_SECURITY must be one of {', '.join(SECURITY_MODES)}; got {security!r}"
            )
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.security = security
        self.timeout = timeout

    def send(self, message: EmailMessage) -> None:
        """Open a connection, authenticate if credentials are configured, and send.

        The timeout is the *socket* timeout, applied to every step of the
        conversation including the initial connect. It is load-bearing: sends run
        synchronously inside the admin's request and production is 2 workers x 4
        threads, so an unbounded send against a relay that accepts the TCP
        connection and then goes quiet holds an eighth of the server hostage until
        the kernel gives up — minutes, not seconds.
        """
        try:
            with self._connect() as smtp:
                if self.security == 'starttls':
                    # Upgrade before AUTH: on a plaintext socket the credentials
                    # would go out in the clear, and most relays refuse AUTH there
                    # anyway.
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            # No message, no body, no recipient list beyond the header we already
            # log — just the fault. ssl.SSLError and socket.timeout are OSError
            # subclasses, so both arrive here.
            raise MailError(f"SMTP delivery failed via {self.host}:{self.port}: {exc}") from exc
        logger.info("Sent mail to %s (subject: %s)", message.get('To', ''), message.get('Subject', ''))

    def _connect(self):
        """Return a connected SMTP object for the configured security mode."""
        if self.security == 'ssl':
            # Implicit TLS (port 465): the socket is encrypted from the first byte,
            # so there is no plaintext window and no STARTTLS to strip.
            return smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                    context=ssl.create_default_context())
        return smtplib.SMTP(self.host, self.port, timeout=self.timeout)


class ConsoleMailer(Mailer):
    """Writes the whole message to the log. The default when no relay is configured.

    A dev checkout has to be able to complete an invite flow, and the invite is
    only completable if the link is reachable — so this backend deliberately does
    what the rest of the module forbids and renders the body, token and all. That
    is sound precisely because it is unreachable in any deployment that sends
    mail: :func:`build_mailer` only selects it when ``SMTP_HOST`` is unset, which
    on a real install means invites are not being emailed at all.
    """

    def send(self, message: EmailMessage) -> None:
        """Render the message to the log so a developer can read it and click the link."""
        logger.warning(
            "ConsoleMailer: no SMTP_HOST configured, mail is being printed rather than sent.\n"
            "%s", message.as_string()
        )


class NullMailer(Mailer):
    """Discards everything. Selected by ``MAIL_ENABLED=false``.

    For an operator who wants invites (issued, with the admin copy-link fallback)
    but no outbound mail at all — and for the console-noise-free case where
    ConsoleMailer's full dump would be worse than silence.
    """

    def send(self, message: EmailMessage) -> None:
        """Drop the message, recording only who it would have gone to."""
        logger.info("Mail disabled (MAIL_ENABLED=false); dropped message to %s",
                    message.get('To', ''))


class MemoryMailer(Mailer):
    """Keeps every message in :attr:`outbox`. The test fixture.

    Swapped into ``app.extensions['mailer']`` so a test can assert on what would
    have been sent without a relay, a socket, or a monkeypatched module global.
    """

    def __init__(self):
        self.outbox: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        """Append the message to :attr:`outbox`."""
        self.outbox.append(message)


def build_mailer() -> Mailer:
    """Choose a backend from configuration.

    The order matters, and it is *disabled first*: ``MAIL_ENABLED=false`` is an
    explicit instruction to send nothing, so it must win even on a host that still
    has SMTP credentials sitting in its ``.env``.

    1. ``MAIL_ENABLED=false``  -> :class:`NullMailer`
    2. ``SMTP_HOST`` set       -> :class:`SMTPMailer`
    3. otherwise               -> :class:`ConsoleMailer`
    """
    if not MAIL_ENABLED:
        return NullMailer()
    if SMTP_HOST:
        return SMTPMailer(
            SMTP_HOST,
            port=SMTP_PORT,
            username=SMTP_USERNAME,
            password=SMTP_PASSWORD,
            security=SMTP_SECURITY,
            timeout=MAIL_TIMEOUT_SECONDS,
        )
    return ConsoleMailer()
