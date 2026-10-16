from __future__ import annotations

import pytest

from mod_private.imap import ImapError, ImapMailbox, parse_message
from tests.private.fakes import FakeImap, rfc822


def mailbox(fake: FakeImap, password: str = "pw") -> ImapMailbox:  # noqa: S107
    return ImapMailbox("imap.example.org", "owner", password, connect=lambda h, p: fake)


def test_fetch_new_reads_without_changing_the_mailbox() -> None:
    fake = FakeImap({3: rfc822("Hi", "Lunch on Thursday?"), 5: rfc822("Bill", "Pay by Friday")})
    validity, mails = mailbox(fake).fetch_new(0, None)
    assert validity == "7"
    assert [(m.uid, m.subject, m.mail_id) for m in mails] == [
        (3, "Hi", "INBOX:7:3"),
        (5, "Bill", "INBOX:7:5"),
    ]
    assert mails[0].body == "Lunch on Thursday?"
    assert ("select", "INBOX", True) in fake.log
    assert ("logout",) in fake.log


def test_fetch_new_continues_after_last_uid_and_resets_on_new_uidvalidity() -> None:
    fake = FakeImap({3: rfc822("a", "a"), 5: rfc822("b", "b")})
    _, mails = mailbox(fake).fetch_new(3, "7")
    assert [m.uid for m in mails] == [5]
    _, mails = mailbox(fake).fetch_new(5, "7")
    assert mails == []
    _, mails = mailbox(fake).fetch_new(5, "6")
    assert [m.uid for m in mails] == [3, 5]


def test_oversized_messages_are_skipped() -> None:
    fake = FakeImap({1: rfc822("big", "x" * (3 * 1024 * 1024))})
    _, mails = mailbox(fake).fetch_new(0, None)
    assert mails == []


def test_login_failure() -> None:
    with pytest.raises(ImapError):
        mailbox(FakeImap({1: rfc822("a", "a")}), password="wrong").fetch_new(0, None)  # noqa: S106


def test_html_mail_is_reduced_to_text() -> None:
    mail = parse_message(
        rfc822("News", "<p>Hello&nbsp;<b>you</b></p><script>x</script>", html=True),
        mail_id="INBOX:1:1",
        uid=1,
    )
    assert "Hello" in mail.body and "<b>" not in mail.body
    assert mail.sender == "Ann <ann@example.org>"
    assert mail.received_at is not None and mail.received_at.utcoffset() is not None


def test_append_draft_uses_draft_flag() -> None:
    fake = FakeImap({1: rfc822("a", "a")})
    mailbox(fake).append_draft(b"Subject: x\r\n\r\nbody")
    assert fake.appended == [("Drafts", r"(\Draft \Seen)", b"Subject: x\r\n\r\nbody")]
