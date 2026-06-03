"""Tests for pipeline/filter_engine.py."""
from pipeline.filter_engine import FilterEngine


def _msg(**kwargs):
    defaults = {
        "sender": "alice",
        "sender_id": "u1",
        "channel": "general",
        "channel_id": "ch1",
        "text": "hello world",
    }
    defaults.update(kwargs)
    return defaults


def test_no_rules_passes_all():
    fe = FilterEngine({})
    assert fe.should_keep(_msg()) is True


def test_skip_senders_by_name():
    fe = FilterEngine({"skip_senders": ["alice"]})
    assert fe.should_keep(_msg(sender="alice")) is False
    assert fe.should_keep(_msg(sender="bob")) is True


def test_skip_senders_by_sender_id():
    fe = FilterEngine({"skip_senders": ["u1"]})
    assert fe.should_keep(_msg(sender_id="u1")) is False
    assert fe.should_keep(_msg(sender_id="u2")) is True


def test_skip_channels_by_channel_id():
    fe = FilterEngine({"skip_channels": ["ch1"]})
    assert fe.should_keep(_msg(channel_id="ch1")) is False
    assert fe.should_keep(_msg(channel_id="ch2")) is True


def test_only_channels_rejects_unlisted():
    fe = FilterEngine({"only_channels": ["ch_dev"]})
    assert fe.should_keep(_msg(channel_id="ch_general")) is False


def test_only_channels_passes_listed_by_id():
    fe = FilterEngine({"only_channels": ["ch99"]})
    assert fe.should_keep(_msg(channel_id="ch99")) is True


def test_skip_patterns_blocks_match_case_insensitive():
    fe = FilterEngine({"skip_patterns": ["SPAM"]})
    assert fe.should_keep(_msg(text="this is spam content")) is False
    assert fe.should_keep(_msg(text="SPAM alert")) is False
    assert fe.should_keep(_msg(text="normal message")) is True


def test_filter_messages_returns_kept_only():
    fe = FilterEngine({"skip_senders": ["bot"]})
    messages = [
        _msg(sender="alice"),
        _msg(sender="bot"),
        _msg(sender="bob"),
    ]
    kept = fe.filter_messages(messages)
    assert len(kept) == 2
    assert all(m["sender"] != "bot" for m in kept)


# ── Email-specific filters ────────────────────────────────────────

def test_skip_folders_via_raw_folder_drops_message():
    fe = FilterEngine({"skip_folders": ["Spam"]})
    msg = _msg()
    msg["raw"] = {"folder": "Spam"}
    assert fe.should_keep(msg) is False


def test_skip_folders_via_channel_id_prefix_drops_message():
    fe = FilterEngine({"skip_folders": ["Spam"]})
    msg = _msg()
    msg["channel_id"] = "folder:Spam"
    assert fe.should_keep(msg) is False


def test_skip_folders_keeps_unrelated_folder():
    fe = FilterEngine({"skip_folders": ["Spam"]})
    msg = _msg()
    msg["raw"] = {"folder": "INBOX"}
    assert fe.should_keep(msg) is True


def test_skip_subjects_matches_regex_in_raw_subject():
    fe = FilterEngine({"skip_subjects": ["^Out of office"]})
    msg = _msg()
    msg["raw"] = {"subject": "Out of office: back Monday"}
    assert fe.should_keep(msg) is False


def test_skip_subjects_is_case_insensitive():
    fe = FilterEngine({"skip_subjects": [r"auto[-\s]?reply"]})
    msg = _msg()
    msg["raw"] = {"subject": "AUTO REPLY: thanks"}
    assert fe.should_keep(msg) is False


def test_skip_senders_matches_email_address_too():
    fe = FilterEngine({"skip_senders": ["noreply@example.com"]})
    msg = _msg(sender="Notifier")  # display name doesn't match
    msg["sender_email"] = "noreply@example.com"
    assert fe.should_keep(msg) is False


def test_skip_senders_matches_raw_from_when_no_sender_email_set():
    fe = FilterEngine({"skip_senders": ["noreply@example.com"]})
    msg = _msg(sender="Notifier")
    msg["raw"] = {"from": "noreply@example.com"}
    assert fe.should_keep(msg) is False


def test_chat_message_unaffected_by_email_filter_keys():
    """Chat msgs (no raw.folder / raw.subject) should not be impacted by email keys."""
    fe = FilterEngine({"skip_folders": ["Spam"], "skip_subjects": ["^Test"]})
    msg = _msg()  # no `raw` block
    assert fe.should_keep(msg) is True
