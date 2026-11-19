"""SMTP/IMAP replies: a reply some filter put in Junk is still read, and Junk is
never changed (nothing marked seen or moved)."""

from datetime import date
from types import SimpleNamespace

import pytest

from sellbird.integrations import smtp_mail
from sellbird.integrations.mail_provider import MailIdentity


def _raw(sender, subject, *, in_reply_to="", references="", body="Thanks, tell me more."):
    lines = [f"From: {sender}", "To: sales@example.org", f"Subject: {subject}",
             f"Message-ID: <{abs(hash((sender, subject)))}@example.net>",
             "Date: Tue, 17 Nov 2026 10:00:00 +0000"]
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references:
        lines.append(f"References: {references}")
    return ("\r\n".join(lines) + "\r\n\r\n" + body + "\r\n").encode()


def _headers_only(raw, names):
    head = raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
    keep = [h for h in head if h.split(b":", 1)[0].lower() in names]
    return b"\r\n".join(keep) + b"\r\n\r\n"


class FakeImap:
    """Just enough IMAP: folders of messages, UNSEEN/SINCE search, the fetches
    Sellbird makes. Records whatever would change a message's flags."""

    def __init__(self, folders, list_lines, broken_junk=False):
        self.folders = {name: [{"raw": raw, "seen": False} for raw in msgs]
                        for name, msgs in folders.items()}
        self.list_lines = list_lines
        self.broken_junk = broken_junk
        self.selected = None
        self.readonly = {}
        self.marked_seen = []
        self.searches = []

    def login(self, user, password):
        return "OK", [b"logged in"]

    def logout(self):
        return "BYE", [b""]

    def list(self):
        if self.broken_junk:
            raise OSError("connection reset")
        return "OK", self.list_lines

    def select(self, mailbox="INBOX", readonly=False):
        name = mailbox.strip('"')
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.selected = name
        self.readonly[name] = readonly
        return "OK", [str(len(self.folders[name])).encode()]

    def search(self, charset, *criteria):
        self.searches.append((self.selected, criteria))
        msgs = self.folders[self.selected]
        ids = [n for n, m in enumerate(msgs, 1) if criteria != ("UNSEEN",) or not m["seen"]]
        return "OK", [b" ".join(str(n).encode() for n in ids)]

    def fetch(self, message_set, spec):
        out = []
        for n in message_set.split(b","):
            m = self.folders[self.selected][int(n) - 1]
            if spec == "(RFC822)":
                m["seen"] = True
                self.marked_seen.append((self.selected, int(n)))
                data = m["raw"]
            elif "HEADER.FIELDS" in spec:
                data = _headers_only(m["raw"], {b"in-reply-to", b"references"})
            elif spec == "(BODY.PEEK[])":
                data = m["raw"]
            else:
                raise AssertionError(f"unexpected fetch {spec}")
            out += [(n + b" (BODY[] {%d}" % len(data), data), b")"]
        return "OK", out


ZIMBRA_LIST = [b'(\\HasNoChildren \\Drafts) "/" "Drafts"', b'(\\HasNoChildren) "/" "INBOX"',
               b'(\\NoInferiors \\Junk) "/" "Junk"', b'(\\HasNoChildren \\Trash) "/" "Trash"']


def _provider(monkeypatch, fake):
    monkeypatch.setattr(smtp_mail.imaplib, "IMAP4_SSL", lambda host, port: fake)
    return smtp_mail.SmtpImapProvider(
        SimpleNamespace(), SimpleNamespace(), identity=MailIdentity("sales@example.org", "Sam"),
        smtp=smtp_mail.SmtpSettings(host="mail.example.org", username="sales@example.org",
                                    password="pw"))


@pytest.mark.asyncio
async def test_a_reply_filed_in_junk_is_read_and_junk_is_left_alone(monkeypatch):
    fake = FakeImap({
        "INBOX": [_raw("lee@example.net", "Re: Quick question",
                       in_reply_to="<a1@example.org>")],
        "Junk": [
            _raw("pat@example.com", "Re: Your site", in_reply_to="<b2@example.org>"),
            _raw("kim@example.com", "Re: Hello", references="<c3@example.org>"),
            _raw("someone@warmup.test", "Louis, I need your advice"),
            _raw("mailer-daemon@mx.example.com", "Undelivered Mail Returned to Sender"),
        ],
    }, ZIMBRA_LIST)

    replies = await _provider(monkeypatch, fake).get_replies()

    assert sorted(r.from_email for r in replies) == [
        "kim@example.com", "lee@example.net", "pat@example.com"]
    # Junk was opened read-only and nothing in it was marked seen.
    assert fake.readonly["Junk"] is True
    assert all(folder == "INBOX" for folder, _ in fake.marked_seen)
    assert not any(m["seen"] for m in fake.folders["Junk"])
    # Junk is read by date (an English IMAP date), not by UNSEEN.
    (criteria,) = [c for folder, c in fake.searches if folder == "Junk"]
    assert criteria[0] == "SINCE" and criteria[1].split("-")[1] in smtp_mail._MONTHS


@pytest.mark.asyncio
async def test_a_broken_junk_folder_keeps_the_inbox_replies(monkeypatch):
    fake = FakeImap({"INBOX": [_raw("lee@example.net", "Re: Quick question",
                                    in_reply_to="<a1@example.org>")]},
                    ZIMBRA_LIST, broken_junk=True)
    replies = await _provider(monkeypatch, fake).get_replies()
    assert [r.from_email for r in replies] == ["lee@example.net"]


@pytest.mark.asyncio
async def test_no_junk_folder_reads_the_inbox_as_before(monkeypatch):
    fake = FakeImap({"INBOX": [_raw("lee@example.net", "Re: Quick question",
                                    in_reply_to="<a1@example.org>")]},
                    [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Archive"'])
    replies = await _provider(monkeypatch, fake).get_replies()
    assert [r.from_email for r in replies] == ["lee@example.net"]
    assert [folder for folder, _ in fake.searches] == ["INBOX"]


@pytest.mark.parametrize("lines, expected", [
    ([b'(\\HasNoChildren \\Junk) "/" "Bulk"', b'(\\HasNoChildren) "/" "Spam"'], '"Bulk"'),
    ([b'(\\HasNoChildren) "." "INBOX.Spam"'], '"INBOX.Spam"'),
    ([b'(\\HasNoChildren) "/" "Junk E-mail"'], '"Junk E-mail"'),
    ([b'(\\HasNoChildren) "/" "Courrier ind&AOk-sirable"'], '"Courrier ind&AOk-sirable"'),
    ([b'(\\HasNoChildren) "/" Junk'], "Junk"),
    ([b'(\\Noselect \\Junk) "/" "Junk"'], None),
    ([b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Junk mail tips"'], None),
])
def test_finds_the_junk_folder_by_its_flag_or_its_name(lines, expected):
    conn = SimpleNamespace(list=lambda: ("OK", lines))
    assert smtp_mail._junk_folder(conn) == expected


def test_imap_dates_are_english_whatever_the_locale():
    assert smtp_mail._imap_date(date(2026, 11, 3)) == "3-Nov-2026"
