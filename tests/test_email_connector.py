"""Tests for connectors/email_connector.py — IMAP polling + draft build.

The IMAP MailBox is mocked end-to-end (no real network). MailMessage objects
come from raw RFC 5322 bytes via `MailMessage.from_bytes(...)` so we exercise
the same parser the live code uses.
"""
from unittest.mock import MagicMock
import pytest
from imap_tools import MailMessage

from connectors.email_connector import EmailConnector, _strip_html, _parse_references


# ── small helpers ────────────────────────────────────────────────────────────

def _raw(headers: dict, body: str = "hi") -> bytes:
    head = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
    return f"{head}\r\n\r\n{body}".encode("utf-8")


def _mailmessage(headers, body="hi", uid="1"):
    mm = MailMessage.from_bytes(_raw(headers, body))
    # MailMessage.uid is set by the fetch layer; back-fill for tests.
    mm._uid = uid  # noqa: SLF001 — test-only injection
    type(mm).uid = property(lambda self: getattr(self, "_uid", None))
    return mm


def _connector(**overrides):
    defaults = dict(
        host="imap.example.com",
        username="me@example.com",
        password="pw",
        source_name="work_email",
        folders=["INBOX"],
        db=MagicMock(),
    )
    defaults.update(overrides)
    return EmailConnector(**defaults)


# ── _strip_html / _parse_references unit helpers ─────────────────────────────

def test_strip_html_removes_tags_and_collapses_whitespace():
    assert _strip_html("<p>hi   <b>there</b></p>") == "hi there"


def test_strip_html_empty_returns_empty():
    assert _strip_html("") == ""
    assert _strip_html(None) == ""


def test_parse_references_picks_out_angle_bracketed_ids():
    refs = _parse_references("<a@x> <b@y>\r\n <c@z>")
    assert refs == ["<a@x>", "<b@y>", "<c@z>"]


def test_parse_references_empty_returns_empty():
    assert _parse_references("") == []
    assert _parse_references(None) == []


# ── _normalize ───────────────────────────────────────────────────────────────

def test_normalize_basic_email_fields_populated():
    c = _connector()
    mm = _mailmessage({
        "Message-ID": "<abc@x>",
        "From": "Alice <alice@MyCompany.com>",
        "To": "Bob <bob@client.com>",
        "Subject": "Hello there",
    }, body="hi body")
    out = c._normalize(mm, "INBOX")
    assert out["id"] == "work_email:<abc@x>"
    assert out["source"] == "work_email"
    assert out["channel_id"] == "folder:INBOX"
    assert out["sender"] == "Alice"
    assert out["sender_id"] == "alice@mycompany.com"
    assert out["sender_email"] == "alice@mycompany.com"
    assert out["thread_id"] == "<abc@x>"   # orphan → self
    assert out["reply_to_id"] is None
    assert out["raw"]["subject"] == "Hello there"
    assert out["raw"]["from"] == "alice@mycompany.com"
    assert out["raw"]["to"] == [{"name": "Bob", "email": "bob@client.com"}]
    assert "Subject: Hello there" in out["text"]
    assert "hi body" in out["text"]


def test_normalize_reply_threading_uses_first_reference_as_root():
    c = _connector()
    mm = _mailmessage({
        "Message-ID": "<reply2@x>",
        "From": "alice@example.com",
        "To": "bob@example.com",
        "Subject": "Re: thread",
        "In-Reply-To": "<reply1@x>",
        "References": "<root@x> <reply1@x>",
    })
    out = c._normalize(mm, "INBOX")
    assert out["thread_id"] == "<root@x>"
    assert out["reply_to_id"] == "<reply1@x>"
    assert out["raw"]["references"] == ["<root@x>", "<reply1@x>"]


def test_normalize_recipient_emails_helper_includes_cc():
    c = _connector()
    mm = _mailmessage({
        "Message-ID": "<m@x>",
        "From": "alice@example.com",
        "To": "bob@example.com",
        "Cc": "carol@example.com, dave@example.com",
        "Subject": "Hi",
    })
    out = c._normalize(mm, "INBOX")
    assert out["_recipient_emails"] == [
        "bob@example.com", "carol@example.com", "dave@example.com"
    ]
    assert [r["email"] for r in out["raw"]["cc"]] == ["carol@example.com", "dave@example.com"]


def test_normalize_synthetic_message_id_when_missing():
    """No Message-ID header → connector mints a stable UID-based id."""
    c = _connector()
    mm = _mailmessage({
        "From": "alice@example.com",
        "To": "me@example.com",
        "Subject": "no id",
    }, uid="42")
    out = c._normalize(mm, "INBOX")
    assert out["id"].startswith("work_email:<uid-INBOX-42@work_email>")


def test_normalize_hoists_attachment_marker_above_body():
    """The [attachment: ..., file_id=...] line must come BEFORE the body so
    format_message's 200-char preview truncation doesn't hide the file_id."""
    c = _connector(text_extensions={".txt"})

    class FakeAttachment:
        filename = "report.pdf"
        payload = b"%PDF-1.4 binary bytes here"

    mm = _mailmessage({
        "Message-ID": "<a@x>",
        "From": "alice@example.com",
        "To": "bob@example.com",
        "Subject": "See attached",
    }, body="See attachment.\n\n-- \nLong signature line that pushes total text past 200 chars " * 3)
    mm.attachments = [FakeAttachment()]

    out = c._normalize(mm, "INBOX")
    text = out["text"]
    # Marker must appear within the first 200 chars (the format_message cap).
    head = text[:200]
    assert "[attachment: report.pdf, file_id=" in head
    # And the body still lands afterwards in the full stored text.
    assert "See attachment." in text


def test_normalize_text_attachment_preview_trails_after_markers():
    """Text-typed attachment previews stay in the full text but don't crowd the
    head — markers list first, previews after, then body."""
    c = _connector(text_extensions={".txt"})

    class FakeAttachment:
        filename = "notes.txt"
        payload = b"line one\nline two\n"

    mm = _mailmessage({
        "Message-ID": "<a@x>",
        "From": "alice@example.com",
        "To": "bob@example.com",
        "Subject": "Notes",
    }, body="body here")
    mm.attachments = [FakeAttachment()]

    out = c._normalize(mm, "INBOX")
    text = out["text"]
    marker_pos = text.find("[attachment: notes.txt")
    preview_pos = text.find("line one")
    body_pos = text.find("body here")
    assert 0 <= marker_pos < preview_pos < body_pos


def test_normalize_html_only_body_is_stripped_to_text():
    raw = (
        "Message-ID: <h@x>\r\n"
        "From: alice@example.com\r\n"
        "To: bob@example.com\r\n"
        "Subject: HTML\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "\r\n"
        "<html><body><p>hello <b>world</b></p></body></html>"
    ).encode("utf-8")
    mm = MailMessage.from_bytes(raw)
    c = _connector()
    out = c._normalize(mm, "INBOX")
    assert "hello world" in out["text"]
    assert "<p>" not in out["text"]


# ── channel name remap ──────────────────────────────────────────────────────

def test_display_name_uses_channel_names_remap():
    c = _connector(channel_names={"INBOX/Project Alpha": "alpha-email"})
    assert c._display_name("INBOX/Project Alpha") == "alpha-email"
    assert c._display_name("INBOX") == "INBOX"  # untouched


# ── fetch_messages flow with mocked mailbox ─────────────────────────────────

def test_fetch_messages_skips_folders_in_skip_list():
    c = _connector(folders=["INBOX", "Spam"], skip_folders=["Spam"])
    fake_mb = MagicMock()
    fake_mb.folder.status.return_value = {"UIDVALIDITY": 7, "UIDNEXT": 100}
    fake_mb.fetch.return_value = iter([])
    c._mailbox = fake_mb

    result = c.fetch_messages()
    assert result["channels_scanned"] == 1
    assert result["channels_skipped"] == 1


def test_fetch_folder_first_run_no_state_uses_ALL_with_limit():
    c = _connector(folders=["INBOX"], default_limit=200)
    # db.get_channel_row returns None for "no prior state".
    c._db.get_channel_row.return_value = None
    fake_mb = MagicMock()
    fake_mb.folder.status.return_value = {"UIDVALIDITY": 5, "UIDNEXT": 10}
    fake_mb.fetch.return_value = iter([
        _mailmessage({"Message-ID": "<a@x>", "From": "a@x", "To": "b@x", "Subject": "s"}, uid="1"),
    ])
    c._mailbox = fake_mb

    c._fetch_folder("INBOX")
    args, kwargs = fake_mb.fetch.call_args
    assert args[0] == "ALL"
    assert kwargs["limit"] == 200


def test_fetch_folder_with_saved_state_uses_uid_range():
    c = _connector(folders=["INBOX"])
    c._db.get_channel_row.return_value = {
        "extra": {"uidvalidity": 5, "last_uid": 42}
    }
    fake_mb = MagicMock()
    fake_mb.folder.status.return_value = {"UIDVALIDITY": 5, "UIDNEXT": 50}
    fake_mb.fetch.return_value = iter([])
    c._mailbox = fake_mb

    c._fetch_folder("INBOX")
    args, kwargs = fake_mb.fetch.call_args
    assert args[0] == "UID 43:*"
    assert kwargs["limit"] is None


def test_fetch_folder_uidvalidity_change_forces_full_rescan():
    c = _connector(folders=["INBOX"])
    c._db.get_channel_row.return_value = {
        "extra": {"uidvalidity": 1, "last_uid": 42}
    }
    fake_mb = MagicMock()
    fake_mb.folder.status.return_value = {"UIDVALIDITY": 999, "UIDNEXT": 50}
    fake_mb.fetch.return_value = iter([])
    c._mailbox = fake_mb

    c._fetch_folder("INBOX")
    assert fake_mb.fetch.call_args[0][0] == "ALL"


def test_fetch_folder_persists_new_high_water_mark():
    c = _connector(folders=["INBOX"])
    c._db.get_channel_row.return_value = {"extra": {"uidvalidity": 5, "last_uid": 10}}
    fake_mb = MagicMock()
    fake_mb.folder.status.return_value = {"UIDVALIDITY": 5, "UIDNEXT": 30}
    fake_mb.fetch.return_value = iter([
        _mailmessage({"Message-ID": "<a@x>", "From": "a@x", "To": "b@x", "Subject": "s"}, uid="20"),
        _mailmessage({"Message-ID": "<b@x>", "From": "a@x", "To": "b@x", "Subject": "s"}, uid="25"),
    ])
    c._mailbox = fake_mb

    c._fetch_folder("INBOX")
    call = c._db.upsert_channel.call_args[0][0]
    assert call["extra"]["last_uid"] == 25
    assert call["extra"]["uidvalidity"] == 5
    assert call["id"] == "folder:INBOX"


def test_fetch_folder_skips_uids_at_or_below_last_seen():
    """Belt-and-suspenders: the UID range is inclusive; we must filter again client-side."""
    c = _connector(folders=["INBOX"])
    c._db.get_channel_row.return_value = {"extra": {"uidvalidity": 5, "last_uid": 10}}
    fake_mb = MagicMock()
    fake_mb.folder.status.return_value = {"UIDVALIDITY": 5, "UIDNEXT": 30}
    fake_mb.fetch.return_value = iter([
        _mailmessage({"Message-ID": "<old@x>", "From": "a@x", "To": "b@x"}, uid="10"),
        _mailmessage({"Message-ID": "<new@x>", "From": "a@x", "To": "b@x"}, uid="11"),
    ])
    c._mailbox = fake_mb

    msgs = c._fetch_folder("INBOX", force=False)
    ids = [m["id"] for m in msgs]
    assert ids == ["work_email:<new@x>"]


# ── fetch_new punts ──────────────────────────────────────────────────────────

def test_fetch_new_raises_not_implemented():
    c = _connector()
    with pytest.raises(NotImplementedError, match="Live fetch not supported"):
        c.fetch_new("folder:INBOX")


# ── send_message draft path ──────────────────────────────────────────────────

def _parent_row(message_id="<root@x>", subject="Project Alpha",
                from_addr="alice@client.com",
                to=None, cc=None, references=None):
    return {
        "id": f"work_email:{message_id}",
        "raw": {
            "from": from_addr,
            "to": to or [{"name": "Me", "email": "me@example.com"}],
            "cc": cc or [],
            "subject": subject,
            "message_id": message_id,
            "references": references or [],
        },
    }


def test_send_message_refuses_empty_text():
    c = _connector()
    c._mailbox = MagicMock()
    with pytest.raises(ValueError, match="empty"):
        c.send_message("folder:INBOX", "   ", reply_to="<x@y>")


def test_send_message_refuses_without_reply_to():
    c = _connector()
    c._mailbox = MagicMock()
    with pytest.raises(ValueError, match="reply_to"):
        c.send_message("folder:INBOX", "hello", reply_to=None)


def test_send_message_refuses_when_parent_not_found():
    c = _connector()
    c._mailbox = MagicMock()
    c._db.get_by_ids.return_value = []  # no parent
    with pytest.raises(ValueError, match="not found"):
        c.send_message("folder:INBOX", "hello", reply_to="<ghost@x>")


def test_send_message_builds_reply_all_minus_self_and_appends_draft():
    c = _connector()
    c._db.get_by_ids.return_value = [_parent_row(
        from_addr="alice@client.com",
        to=[{"name": "Me", "email": "me@example.com"},
            {"name": "Bob", "email": "bob@client.com"}],
        cc=[{"name": "Carol", "email": "carol@client.com"}],
    )]
    fake_mb = MagicMock()
    c._mailbox = fake_mb

    new_id = c.send_message("folder:INBOX", "Replying body.", reply_to="<root@x>")

    assert new_id.startswith("<") and "@" in new_id
    args, kwargs = fake_mb.append.call_args
    raw_bytes = args[0]
    assert kwargs["folder"] == "Drafts"
    # \Draft and \Seen flags requested.
    flags = kwargs["flag_set"]
    assert any("Draft" in str(f) for f in flags)
    assert any("Seen" in str(f) for f in flags)
    # Reply-all: To = original sender; Cc = original To/Cc minus self and minus sender.
    body = raw_bytes.decode("utf-8")
    assert "To: alice@client.com" in body
    assert "Cc: bob@client.com, carol@client.com" in body or \
           "Cc: bob@client.com,\r\n carol@client.com" in body  # folded form
    assert "me@example.com" not in body.split("\r\n\r\n", 1)[0].replace("From: me@example.com", "")


def test_send_message_subject_gets_re_prefix():
    c = _connector()
    c._db.get_by_ids.return_value = [_parent_row(subject="Project Alpha")]
    c._mailbox = MagicMock()
    c.send_message("folder:INBOX", "body", reply_to="<root@x>")
    body = c._mailbox.append.call_args[0][0].decode("utf-8")
    assert "Subject: Re: Project Alpha" in body


def test_send_message_subject_re_not_duplicated_when_already_re():
    c = _connector()
    c._db.get_by_ids.return_value = [_parent_row(subject="Re: Project Alpha")]
    c._mailbox = MagicMock()
    c.send_message("folder:INBOX", "body", reply_to="<root@x>")
    body = c._mailbox.append.call_args[0][0].decode("utf-8")
    assert "Subject: Re: Project Alpha" in body
    assert "Re: Re:" not in body


def test_send_message_sets_in_reply_to_and_extends_references():
    c = _connector()
    c._db.get_by_ids.return_value = [_parent_row(
        message_id="<reply1@x>", references=["<root@x>"],
    )]
    c._mailbox = MagicMock()
    c.send_message("folder:INBOX", "body", reply_to="<reply1@x>")
    body = c._mailbox.append.call_args[0][0].decode("utf-8")
    assert "In-Reply-To: <reply1@x>" in body
    assert "References: <root@x> <reply1@x>" in body


def test_send_message_uses_configured_drafts_folder():
    c = _connector(drafts_folder="[Gmail]/Drafts")
    c._db.get_by_ids.return_value = [_parent_row()]
    c._mailbox = MagicMock()
    c.send_message("folder:INBOX", "body", reply_to="<root@x>")
    assert c._mailbox.append.call_args[1]["folder"] == "[Gmail]/Drafts"


def test_send_message_from_header_is_account_address():
    c = _connector(username="me@example.com")
    c._db.get_by_ids.return_value = [_parent_row()]
    c._mailbox = MagicMock()
    c.send_message("folder:INBOX", "body", reply_to="<root@x>")
    body = c._mailbox.append.call_args[0][0].decode("utf-8")
    assert "From: me@example.com" in body


# ── get_sender_info trivial passthrough ──────────────────────────────────────

def test_get_sender_info_returns_address_as_username_and_email():
    c = _connector()
    info = c.get_sender_info("Alice@Example.COM")
    assert info["email"] == "alice@example.com"
    assert info["username"] == "alice@example.com"
    assert info["sender_id"] == "alice@example.com"


def test_get_sender_info_uses_cached_display_name_for_full_name():
    """After normalize sees `From: Alice Smith <alice@x.com>`, the senders cache
    should be filled so the next ingest's get_sender_info returns "Alice Smith"
    in full_name (not None)."""
    c = _connector()
    mm = _mailmessage({
        "Message-ID": "<n@x>",
        "From": "Alice Smith <alice@example.com>",
        "To": "bob@example.com",
        "Subject": "Hi",
    })
    c._normalize(mm, "INBOX")
    info = c.get_sender_info("alice@example.com")
    assert info["full_name"] == "Alice Smith"


def test_get_sender_info_falls_back_to_local_part_when_unseen():
    """No display name seen → use the address's local part rather than None,
    so senders.full_name beats a bare email column for display purposes."""
    c = _connector()
    info = c.get_sender_info("alice@example.com")
    assert info["full_name"] == "alice"


# ── _fetch_folder respects `since` (follow-up fix) ──────────────────

def test_fetch_folder_first_run_with_since_uses_imap_since_predicate(monkeypatch):
    """No saved state + `since` provided → IMAP `SINCE` filter, no limit cap."""
    from datetime import datetime, timezone
    c = _connector(folders=["INBOX"])
    c._db.get_channel_row.return_value = None
    fake_mb = MagicMock()
    fake_mb.folder.status.return_value = {"UIDVALIDITY": 5, "UIDNEXT": 10}
    fake_mb.fetch.return_value = iter([])
    c._mailbox = fake_mb

    since = datetime(2026, 5, 20, tzinfo=timezone.utc)
    c._fetch_folder("INBOX", since=since, force=False)

    args, kwargs = fake_mb.fetch.call_args
    # The IMAP predicate gets stringified as "(SINCE 20-May-2026)".
    assert "SINCE" in str(args[0])
    assert "20-May-2026" in str(args[0])
    assert kwargs["limit"] is None  # date filter, no count cap


def test_fetch_folder_force_with_since_uses_imap_since_not_all(monkeypatch):
    """--force with `since` should still use SINCE, not haul in the whole archive."""
    from datetime import datetime, timezone
    c = _connector(folders=["INBOX"])
    c._db.get_channel_row.return_value = {"extra": {"uidvalidity": 5, "last_uid": 99}}
    fake_mb = MagicMock()
    fake_mb.folder.status.return_value = {"UIDVALIDITY": 5, "UIDNEXT": 200}
    fake_mb.fetch.return_value = iter([])
    c._mailbox = fake_mb

    since = datetime(2026, 5, 20, tzinfo=timezone.utc)
    c._fetch_folder("INBOX", since=since, force=True)

    args, _ = fake_mb.fetch.call_args
    assert "SINCE" in str(args[0])


def test_fetch_folder_first_run_without_since_falls_back_to_all_with_limit():
    """Edge case: `since=None` (e.g. --hours 0). Keep the old behavior so we
    never silently fetch zero messages, but cap with default_limit."""
    c = _connector(folders=["INBOX"], default_limit=50)
    c._db.get_channel_row.return_value = None
    fake_mb = MagicMock()
    fake_mb.folder.status.return_value = {"UIDVALIDITY": 5, "UIDNEXT": 10}
    fake_mb.fetch.return_value = iter([])
    c._mailbox = fake_mb

    c._fetch_folder("INBOX", since=None, force=False)
    args, kwargs = fake_mb.fetch.call_args
    assert args[0] == "ALL"
    assert kwargs["limit"] == 50
