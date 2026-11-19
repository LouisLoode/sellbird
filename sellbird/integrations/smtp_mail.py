"""Generic SMTP + IMAP mailbox provider.

Works with any real mailbox: AgentMail (their IMAP/SMTP relay), Fastmail,
a Google Workspace account with an app password, or self-hosted mail.

.env:
    SMTP_HOST=            IMAP_HOST=
    SMTP_PORT=587         IMAP_PORT=993
    SMTP_USERNAME=        IMAP_USERNAME=   (defaults to SMTP_USERNAME)
    SMTP_PASSWORD=        IMAP_PASSWORD=   (defaults to SMTP_PASSWORD)

Sends as the configured persona (name + email). IMAP polling fetches
UNSEEN inbox messages and marks them seen after retrieval. It also reads the
Junk folder for replies that landed there, without changing anything in it.
"""

import asyncio
import email
import email.policy
import imaplib
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

import aiosmtplib

from sellbird.integrations.mail_provider import (
    InboundMessage,
    MailIdentity,
    MailProvider,
    SendResult,
    identity_from_config,
    looks_like_bounce,
)
from sellbird.integrations.signature import SignatureError, load_signature, set_body

_AUTO_REPLY_HEADERS = ("Auto-Submitted", "X-Autoreply", "X-Autorespond", "X-Autoresponse",
                       "Precedence")
logger = logging.getLogger("sellbird.smtp")

# Some hosts' filters put ordinary mail in Junk, so a prospect's answer can
# land there and its follow-ups would carry on. Junk is read too, but only for
# messages in a thread (In-Reply-To or References): warmup mail and spam never
# reach the handler, and neither does a bare bounce, which the handler would
# count toward the bounce kill switch even when it isn't about our mail. Junk
# is opened read-only (nothing is marked seen or moved) and read by date, not
# UNSEEN, so a reply opened in webmail first is still read; the handler's
# dedup skips what it already handled.
JUNK_DAYS = 7
JUNK_SCAN = 500  # newest Junk messages whose thread headers are checked per read
_JUNK_NAMES = frozenset({"junk", "spam", "junk e-mail", "junk email", "bulk mail",
                         "courrier ind&aok-sirable"})  # IMAP's UTF-7 for "indésirable"
_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\) (?:"(?:[^"\\]|\\.)*"|NIL) (?P<name>.+)$')
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # IMAP dates are never localised
_THREAD_HEADERS = "(BODY.PEEK[HEADER.FIELDS (IN-REPLY-TO REFERENCES)])"


def _imap_date(day: date) -> str:
    return f"{day.day}-{_MONTHS[day.month - 1]}-{day.year}"


def _junk_folder(conn) -> str | None:
    """The mailbox's Junk folder as LIST names it: the one flagged \\Junk
    (RFC 6154), else one with a usual junk name. None if there's neither."""
    status, lines = conn.list()
    if status != "OK":
        return None
    by_name = None
    for line in lines or ():
        match = _LIST_LINE.match(line) if isinstance(line, bytes) else None
        if not match:
            continue
        flags = match["flags"].lower().split()
        if b"\\noselect" in flags or b"\\nonexistent" in flags:
            continue
        name = match["name"].decode("utf-8", "replace")
        if b"\\junk" in flags:
            return name
        leaf = name.strip('"').lower().replace(".", "/").rsplit("/", 1)[-1]
        if by_name is None and leaf in _JUNK_NAMES:
            by_name = name
    return by_name


# Refused or never connected: the server definitely has no copy. Anything else
# (a drop or timeout mid-conversation, an unknown error) might have come after
# DATA was accepted, so the outcome is uncertain.
_DEFINITE_SMTP_ERRORS = (
    aiosmtplib.SMTPConnectError, aiosmtplib.SMTPConnectTimeoutError,
    aiosmtplib.SMTPAuthenticationError, aiosmtplib.SMTPHeloError,
    aiosmtplib.SMTPNotSupported, aiosmtplib.SMTPRecipientsRefused,
    aiosmtplib.SMTPSenderRefused, aiosmtplib.SMTPDataError,
)


@dataclass
class SmtpSettings:
    """Another inbox's server settings, for a caller that keeps them itself.
    Store the password somewhere safe; only public() is fit for a database."""

    host: str
    username: str
    password: str = field(repr=False)
    port: int = 587
    imap_host: str = ""
    imap_port: int = 993

    def public(self) -> dict:
        data = asdict(self)
        data.pop("password")
        return data


class SmtpImapProvider(MailProvider):
    name = "smtp"
    carries_headers = True

    def __init__(self, config, env, *, identity: MailIdentity | None = None,
                 smtp: SmtpSettings | None = None):
        self.config = config
        # Settable: a mailbox transport points it at the inbox it sends from.
        self.identity = identity or identity_from_config(config)
        if smtp is not None:  # another inbox: its own settings, not the .env's
            self.smtp_host, self.smtp_port = smtp.host, int(smtp.port or 587)
            self.smtp_user, self.smtp_pass = smtp.username, smtp.password
            self.imap_host = smtp.imap_host or smtp.host
            self.imap_port = int(smtp.imap_port or 993)
            self.imap_user, self.imap_pass = smtp.username, smtp.password
            return
        self.smtp_host = getattr(env, "smtp_host", "")
        self.smtp_port = int(getattr(env, "smtp_port", 587) or 587)
        self.smtp_user = getattr(env, "smtp_username", "")
        self.smtp_pass = getattr(env, "smtp_password", "")
        self.imap_host = getattr(env, "imap_host", "") or self.smtp_host
        self.imap_port = int(getattr(env, "imap_port", 993) or 993)
        self.imap_user = getattr(env, "imap_username", "") or self.smtp_user
        self.imap_pass = getattr(env, "imap_password", "") or self.smtp_pass

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass)

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
        headers=(),
    ) -> SendResult:
        try:
            sig = load_signature(self.config)
        except SignatureError as e:
            return SendResult(ok=False, error=f"signature: {e}"[:300])
        address = self.identity.address or self.smtp_user
        msg = EmailMessage()
        msg["To"] = to_email
        msg["From"] = formataddr((self.identity.display_name, address))
        msg["Subject"] = subject
        for name, value in headers:
            msg[name] = value
        message_id = make_msgid(domain=address.split("@")[-1])
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        set_body(msg, body, sig)

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_pass,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465),
                timeout=30,
            )
        except Exception as e:
            logger.error(f"SMTP send failed to {to_email}: {e}")
            return SendResult(ok=False, error=str(e)[:300],
                              uncertain=not isinstance(e, _DEFINITE_SMTP_ERRORS))
        return SendResult(ok=True, message_id=message_id, thread_ref=message_id)

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._fetch_inbound, limit)
        except Exception as e:
            logger.error(f"IMAP fetch failed: {e}")
            return []

    def _fetch_inbound(self, limit: int) -> list[InboundMessage]:
        out: list[InboundMessage] = []
        conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        try:
            conn.login(self.imap_user, self.imap_pass)
            conn.select("INBOX")
            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                return []
            ids = data[0].split()[-limit:]
            for msg_id in ids:
                status, parts = conn.fetch(msg_id, "(RFC822)")
                if status != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                parsed = self._parse_rfc822(parts[0][1])
                if parsed:
                    out.append(parsed)
            try:
                out.extend(self._fetch_junk_replies(conn, limit))
            except Exception as e:
                # Best effort on top of the inbox: a quirky Junk folder must
                # not hold sending over replies the inbox already gave.
                logger.warning(f"IMAP: couldn't read the Junk folder: {e}")
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out

    def _fetch_junk_replies(self, conn, limit: int) -> list[InboundMessage]:
        """Messages in a thread from the last JUNK_DAYS of Junk, untouched."""
        folder = _junk_folder(conn)
        if not folder:
            return []
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            return []
        since = _imap_date(date.today() - timedelta(days=JUNK_DAYS))
        status, data = conn.search(None, "SINCE", since)
        if status != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()[-JUNK_SCAN:]
        status, parts = conn.fetch(b",".join(ids), _THREAD_HEADERS)
        if status != "OK":
            return []
        in_thread = []
        for part in parts or ():
            if not isinstance(part, tuple) or not part[0]:
                continue
            headers = email.message_from_bytes(part[1] or b"")
            if headers.get("In-Reply-To") or headers.get("References"):
                in_thread.append(part[0].split()[0])
        out: list[InboundMessage] = []
        for msg_id in in_thread[-limit:]:
            status, parts = conn.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            parsed = self._parse_rfc822(parts[0][1])
            if parsed:
                out.append(parsed)
        return out

    @staticmethod
    def _parse_rfc822(raw: bytes) -> InboundMessage | None:
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            return None
        from_email = parseaddr(str(msg.get("From", "")))[1].lower()
        if not from_email:
            return None
        subject = str(msg.get("Subject", ""))

        body = ""
        try:
            part = msg.get_body(preferencelist=("plain", "html"))
            if part is not None:
                body = part.get_content()
                if part.get_content_type() == "text/html":
                    import re
                    body = re.sub(r"<[^>]+>", " ", body)
        except Exception:
            pass

        message_id = str(msg.get("Message-ID", "")).strip()
        return InboundMessage(
            provider_id=message_id or f"{from_email}:{subject}",
            from_email=from_email,
            subject=subject,
            body=(body or "")[:5000],
            message_id=message_id,
            in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
            thread_ref=str(msg.get("In-Reply-To", "")).strip(),
            date=str(msg.get("Date", "")),
            is_bounce=looks_like_bounce(from_email, subject),
            # Hosted H7: what tells an automatic reply from a person's.
            headers={name.lower(): str(msg.get(name, "")) for name in _AUTO_REPLY_HEADERS
                     if msg.get(name)},
        )

    async def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD missing from .env"
        # SMTP handshake
        try:
            smtp = aiosmtplib.SMTP(
                hostname=self.smtp_host, port=self.smtp_port,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465), timeout=15,
            )
            await smtp.connect()
            await smtp.login(self.smtp_user, self.smtp_pass)
            await smtp.quit()
        except Exception as e:
            return False, f"SMTP login failed: {str(e)[:200]}"
        # IMAP handshake
        loop = asyncio.get_event_loop()

        def _imap_check():
            conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
            try:
                conn.login(self.imap_user, self.imap_pass)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

        try:
            await loop.run_in_executor(None, _imap_check)
        except Exception as e:
            return False, f"SMTP OK but IMAP login failed: {str(e)[:200]}"
        return True, f"SMTP + IMAP connected as {self.smtp_user}"
