"""Tests for mcp_server/server.py helper functions.

VectorStore is mocked globally in conftest.py so the BGE-M3 model is never loaded.
"""
from mcp.types import TextContent as _TC
import pytest as _pytest
import json as _json
import responses as rsps_lib
import asyncio
import json
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

from mcp_server.server import (
    format_timestamp,
    format_message,
    _build_aliases_text,
    get_display_tz,
    make_message_url,
    _find_cached_file,
    _serve_file_content,
    _try_telegram_download,
    _get_new_messages,
    _list_channels,
    _send_message,
    _short_description,
    _build_channel_desc_lookup,
    _summarize_messages,
    _find_by_issue,
    _who_mentioned,
    _upsert_user_alias,
    _remove_user_alias,
    _update_user_alias_strings,
    _args_summary_for,
    call_tool,
)


# ── make_message_url ──────────────────────────────────────────────────────────

def _mm_config(url="https://mm.example.com"):
    return {
        "sources": {
            "mm_src": {"type": "mattermost", "url": url}
        }
    }


def _tg_config():
    return {
        "sources": {
            "tg_src": {"type": "telegram"}
        }
    }


def test_make_message_url_mattermost_permalink():
    msg = {
        "source": "mm_src",
        "channel_name": "dev",
        "raw": {"team_name": "myteam", "post_id": "abc123"},
    }
    url = make_message_url(msg, _mm_config())
    assert url == "https://mm.example.com/myteam/channels/dev/p/abc123"


def test_make_message_url_mattermost_raw_as_string():
    raw = json.dumps({"team_name": "myteam", "post_id": "abc123"})
    msg = {"source": "mm_src", "channel_name": "dev", "raw": raw}
    url = make_message_url(msg, _mm_config())
    assert url == "https://mm.example.com/myteam/channels/dev/p/abc123"


def test_make_message_url_mattermost_no_source_url():
    msg = {
        "source": "mm_src",
        "channel_name": "dev",
        "raw": {"team_name": "myteam", "post_id": "abc123"},
    }
    assert make_message_url(msg, _mm_config(url="")) is None


def test_make_message_url_mattermost_missing_fields():
    msg = {"source": "mm_src", "channel_name": "", "raw": {"team_name": "myteam", "post_id": ""}}
    assert make_message_url(msg, _mm_config()) is None


def test_make_message_url_telegram():
    msg = {
        "source": "tg_src",
        "raw": {"chat_id": "1234567", "message_id": 42, "chat_type": "supergroup"},
    }
    url = make_message_url(msg, _tg_config())
    assert url == "https://t.me/c/1234567/42"


def test_make_message_url_telegram_channel():
    msg = {
        "source": "tg_src",
        "raw": {"chat_id": "1234567", "message_id": 5, "chat_type": "channel"},
    }
    url = make_message_url(msg, _tg_config())
    assert url == "https://t.me/c/1234567/5"


def test_make_message_url_telegram_private_chat_returns_none():
    msg = {
        "source": "tg_src",
        "raw": {"chat_id": "1234567", "message_id": 42, "chat_type": "private"},
    }
    assert make_message_url(msg, _tg_config()) is None


def test_make_message_url_telegram_group_returns_none():
    msg = {
        "source": "tg_src",
        "raw": {"chat_id": "1234567", "message_id": 42, "chat_type": "group"},
    }
    assert make_message_url(msg, _tg_config()) is None


def test_make_message_url_telegram_missing_fields():
    msg = {"source": "tg_src", "raw": {"chat_id": "", "message_id": "", "chat_type": "supergroup"}}
    assert make_message_url(msg, _tg_config()) is None


def test_make_message_url_unknown_source_type():
    config = {"sources": {"other": {"type": "irc"}}}
    msg = {"source": "other", "raw": {}}
    assert make_message_url(msg, config) is None


def test_make_message_url_source_not_in_config():
    msg = {"source": "ghost", "raw": {}}
    assert make_message_url(msg, _mm_config()) is None


def test_make_message_url_pachca():
    config = {"sources": {"pa_src": {"type": "pachca"}}}
    msg = {"source": "pa_src", "raw": {"chat_id": 123, "message_id": 456}}
    url = make_message_url(msg, config)
    assert url == "https://app.pachca.com/chats/123?message=456"


def test_make_message_url_pachca_missing_fields():
    config = {"sources": {"pa_src": {"type": "pachca"}}}
    msg = {"source": "pa_src", "raw": {"chat_id": "", "message_id": ""}}
    assert make_message_url(msg, config) is None


# ── format_timestamp ─────────────────────────────────────────────────────────

def test_format_timestamp_converts_utc_to_local():
    tz = ZoneInfo("Europe/Moscow")  # UTC+3
    result = format_timestamp("2024-01-15T10:00:00+00:00", tz)
    assert result == "2024-01-15 13:00"


def test_format_timestamp_empty_string():
    assert format_timestamp("", ZoneInfo("UTC")) == ""


def test_format_timestamp_unparseable_falls_back_to_slice():
    ts = "not-a-date-string!"
    result = format_timestamp(ts, ZoneInfo("UTC"))
    assert result == ts[:16]


# ── get_display_tz ────────────────────────────────────────────────────────────

def test_get_display_tz_returns_configured_timezone():
    tz = get_display_tz({"display_timezone": "Europe/Moscow"})
    assert tz.key == "Europe/Moscow"


def test_get_display_tz_falls_back_to_utc_for_invalid():
    tz = get_display_tz({"display_timezone": "Invalid/Zone_XYZ"})
    assert tz.key == "UTC"


def test_get_display_tz_defaults_to_utc_when_absent():
    tz = get_display_tz({})
    assert tz.key == "UTC"


# ── format_message ────────────────────────────────────────────────────────────

def test_format_message_includes_url_when_config_provided():
    msg = {
        "source": "mm_src",
        "channel": "dev",
        "channel_name": "dev",
        "sender": "alice",
        "timestamp": "2024-01-15T10:00:00+00:00",
        "text": "hello",
        "raw": {"team_name": "myteam", "post_id": "post99"},
    }
    line = format_message(msg, config=_mm_config())
    assert "https://mm.example.com/myteam/channels/dev/p/post99" in line


def test_format_message_shows_thread_marker_when_threaded():
    msg = {
        "source": "mm_src",
        "channel": "dev",
        "channel_name": "dev",
        "sender": "alice",
        "timestamp": "2024-01-15T10:00:00+00:00",
        "text": "reply",
        "thread_id": "rootpost123",
        "raw": {},
    }
    line = format_message(msg, config=None)
    assert "🧵 thread:rootpost123" in line


def test_format_message_omits_thread_marker_when_not_threaded():
    msg = {
        "source": "mm_src",
        "channel": "dev",
        "sender": "alice",
        "timestamp": "2024-01-15T10:00:00+00:00",
        "text": "hello",
        "thread_id": None,
        "raw": {},
    }
    line = format_message(msg, config=None)
    assert "🧵" not in line


def test_format_message_marks_external_sender():
    msg = {
        "source": "tg", "channel": "dev", "sender": "client",
        "timestamp": "2024-01-15T10:00:00+00:00", "text": "hi", "internal": 0, "raw": {},
    }
    assert "[external]" in format_message(msg, config=None)


def test_format_message_no_marker_for_internal_sender():
    msg = {
        "source": "tg", "channel": "dev", "sender": "staff",
        "timestamp": "2024-01-15T10:00:00+00:00", "text": "hi", "internal": 1, "raw": {},
    }
    assert "[external]" not in format_message(msg, config=None)


def test_format_message_no_marker_when_internal_absent():
    msg = {
        "source": "tg", "channel": "dev", "sender": "someone",
        "timestamp": "2024-01-15T10:00:00+00:00", "text": "hi", "raw": {},
    }
    assert "[external]" not in format_message(msg, config=None)


def test_format_message_omits_url_when_config_is_none():
    msg = {
        "source": "mm_src",
        "channel": "dev",
        "channel_name": "dev",
        "sender": "alice",
        "timestamp": "2024-01-15T10:00:00+00:00",
        "text": "hello",
        "raw": {"team_name": "myteam", "post_id": "post99"},
    }
    line = format_message(msg, config=None)
    assert "http" not in line


# ── _get_new_messages ─────────────────────────────────────────────────────────

@patch("mcp_server.server.get_config")
def test_get_new_messages_unknown_source_returns_error(mock_cfg):
    mock_cfg.return_value = {"sources": {}}
    out = asyncio.run(_get_new_messages({"source": "ghost", "channel": "c"}))
    assert "unknown source" in out[0].text.lower()


def test_get_new_messages_missing_args_returns_error():
    out = asyncio.run(_get_new_messages({"source": "", "channel": ""}))
    assert "required" in out[0].text.lower()


@patch("mcp_server.server.get_db")
@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_get_new_messages_dedupes_against_db(mock_cfg, mock_build, mock_get_db):
    mock_cfg.return_value = {"sources": {"mm": {"type": "mattermost", "url": "u", "token": "t"}}}

    connector = MagicMock()
    connector.fetch_new.return_value = [
        {"id": "mm:old", "source": "mm", "channel": "dev", "sender": "a",
         "timestamp": "2024-01-15T10:00:00+00:00", "text": "old", "raw": {}},
        {"id": "mm:new", "source": "mm", "channel": "dev", "sender": "a",
         "timestamp": "2024-01-15T10:01:00+00:00", "text": "new", "raw": {}},
    ]
    mock_build.return_value = connector

    db = MagicMock()
    db.exists.side_effect = lambda i: i == "mm:old"  # old already stored
    mock_get_db.return_value = db

    out = asyncio.run(_get_new_messages({"source": "mm", "channel": "ch1"}))
    text = out[0].text
    assert "new" in text and "1 new message" in text
    assert "mm:old" not in text


@patch("mcp_server.server.get_db")
@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_get_new_messages_marks_external_live(mock_cfg, mock_build, mock_get_db):
    mock_cfg.return_value = {"sources": {"tg": {"type": "telegram", "token": "t"}}}  # no internal flag
    connector = MagicMock()
    connector.fetch_new.return_value = [
        {"id": "tg:1", "source": "tg", "channel": "c", "sender": "client",
         "timestamp": "2024-01-15T10:00:00+00:00", "text": "hi", "raw": {}},
    ]
    mock_build.return_value = connector
    db = MagicMock()
    db.exists.return_value = False
    mock_get_db.return_value = db

    out = asyncio.run(_get_new_messages({"source": "tg", "channel": "c"}))
    assert "[external]" in out[0].text


@patch("mcp_server.server.get_db")
@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_get_new_messages_internal_source_not_marked(mock_cfg, mock_build, mock_get_db):
    mock_cfg.return_value = {"sources": {"mm": {"type": "mattermost", "url": "u",
                                                "token": "t", "internal": True}}}
    connector = MagicMock()
    connector.fetch_new.return_value = [
        {"id": "mm:1", "source": "mm", "channel": "c", "sender": "staff",
         "timestamp": "2024-01-15T10:00:00+00:00", "text": "hi", "raw": {}},
    ]
    mock_build.return_value = connector
    db = MagicMock()
    db.exists.return_value = False
    mock_get_db.return_value = db

    out = asyncio.run(_get_new_messages({"source": "mm", "channel": "c"}))
    assert "[external]" not in out[0].text


@patch("mcp_server.server.get_db")
@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_get_new_messages_none_new_returns_message(mock_cfg, mock_build, mock_get_db):
    mock_cfg.return_value = {"sources": {"mm": {"type": "mattermost", "url": "u", "token": "t"}}}
    connector = MagicMock()
    connector.fetch_new.return_value = []
    mock_build.return_value = connector
    mock_get_db.return_value = MagicMock()

    out = asyncio.run(_get_new_messages({"source": "mm", "channel": "ch1"}))
    assert "no new messages" in out[0].text.lower()


# ── _send_message ─────────────────────────────────────────────────────────────

def test_send_message_missing_args_returns_error():
    out = asyncio.run(_send_message({"source": "mm", "channel": "c", "text": ""}))
    assert "required" in out[0].text.lower()


@patch("mcp_server.server.get_config")
def test_send_message_unknown_source_returns_error(mock_cfg):
    mock_cfg.return_value = {"sources": {}}
    out = asyncio.run(_send_message({"source": "ghost", "channel": "c", "text": "hi"}))
    assert "unknown source" in out[0].text.lower()


@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_send_message_refuses_when_allow_send_absent(mock_cfg, mock_build):
    mock_cfg.return_value = {"sources": {"mm": {"type": "mattermost", "url": "u", "token": "t"}}}
    out = asyncio.run(_send_message({"source": "mm", "channel": "c", "text": "hi"}))
    assert "disabled" in out[0].text.lower()
    mock_build.assert_not_called()


@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_send_message_refuses_when_allow_send_false(mock_cfg, mock_build):
    mock_cfg.return_value = {"sources": {"mm": {"type": "mattermost", "url": "u",
                                                "token": "t", "allow_send": False}}}
    out = asyncio.run(_send_message({"source": "mm", "channel": "c", "text": "hi"}))
    assert "disabled" in out[0].text.lower()
    mock_build.assert_not_called()


@patch("mcp_server.server.get_db")
@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_send_message_dispatches_pachca_and_returns_result(mock_cfg, mock_build, mock_get_db):
    mock_cfg.return_value = {"sources": {"pa": {"type": "pachca", "token": "t",
                                                "allow_send": True}}}
    connector = MagicMock()
    connector.send_message.return_value = 9001
    mock_build.return_value = connector
    mock_get_db.return_value = MagicMock()

    out = asyncio.run(_send_message({"source": "pa", "channel": "55", "text": "hi"}))
    result = json.loads(out[0].text)

    assert result["success"] is True
    assert result["message_id"] == 9001
    assert result["url"] == "https://app.pachca.com/chats/55?message=9001"
    connector.send_message.assert_called_once_with("55", "hi", parent_message_id=None)


@patch("mcp_server.server.get_db")
@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_send_message_passes_reply_to(mock_cfg, mock_build, mock_get_db):
    mock_cfg.return_value = {"sources": {"mm": {"type": "mattermost", "url": "u",
                                                "token": "t", "allow_send": True}}}
    connector = MagicMock()
    connector.send_message.return_value = "post1"
    connector._resolve_channel.return_value = None
    mock_build.return_value = connector
    mock_get_db.return_value = MagicMock()

    asyncio.run(_send_message({"source": "mm", "channel": "ch1", "text": "hi",
                               "reply_to": "root9"}))
    connector.send_message.assert_called_once_with("ch1", "hi", root_id="root9")


@patch("mcp_server.server.get_db")
@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_send_message_telegram_uses_stored_business_connection_id(mock_cfg, mock_build, mock_get_db):
    mock_cfg.return_value = {"sources": {"tg": {"type": "telegram", "token": "t",
                                                "allow_send": True}}}
    connector = MagicMock()
    connector.send_message.return_value = 777
    mock_build.return_value = connector
    db = MagicMock()
    db.get_channel_row.return_value = {"extra": {"business_connection_id": "bconn-xyz"}}
    mock_get_db.return_value = db

    asyncio.run(_send_message({"source": "tg", "channel": "9153987", "text": "hi"}))

    connector.send_message.assert_called_once_with(
        "9153987", "hi", reply_to=None, business_connection_id="bconn-xyz")


@patch("mcp_server.server.get_db")
@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_send_message_telegram_no_business_id_passes_none(mock_cfg, mock_build, mock_get_db):
    mock_cfg.return_value = {"sources": {"tg": {"type": "telegram", "token": "t",
                                                "allow_send": True}}}
    connector = MagicMock()
    connector.send_message.return_value = 778
    mock_build.return_value = connector
    db = MagicMock()
    db.get_channel_row.return_value = None  # channel not in DB
    mock_get_db.return_value = db

    asyncio.run(_send_message({"source": "tg", "channel": "-100999", "text": "hi"}))

    connector.send_message.assert_called_once_with(
        "-100999", "hi", reply_to=None, business_connection_id=None)


@patch("mcp_server.server.get_db")
@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_send_message_logs_success_to_db(mock_cfg, mock_build, mock_get_db):
    mock_cfg.return_value = {"sources": {"pa": {"type": "pachca", "token": "t",
                                                "allow_send": True}}}
    connector = MagicMock()
    connector.send_message.return_value = 9001
    mock_build.return_value = connector
    db = MagicMock()
    mock_get_db.return_value = db

    asyncio.run(_send_message({"source": "pa", "channel": "55", "text": "hi"}))

    db.record_sent_message.assert_called_once()
    logged = db.record_sent_message.call_args[0][0]
    assert logged["source"] == "pa" and logged["channel"] == "55"
    assert logged["success"] is True and logged["message_id"] == 9001


@patch("mcp_server.server.get_db")
@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_send_message_reports_connector_error(mock_cfg, mock_build, mock_get_db):
    mock_cfg.return_value = {"sources": {"mm": {"type": "mattermost", "url": "u",
                                                "token": "t", "allow_send": True}}}
    connector = MagicMock()
    connector.send_message.side_effect = RuntimeError("rate limit hit")
    connector._resolve_channel.return_value = None
    mock_build.return_value = connector
    db = MagicMock()
    mock_get_db.return_value = db

    out = asyncio.run(_send_message({"source": "mm", "channel": "ch1", "text": "hi"}))
    assert "failed to send" in out[0].text.lower()
    assert "rate limit" in out[0].text.lower()

    db.record_sent_message.assert_called_once()
    logged = db.record_sent_message.call_args[0][0]
    assert logged["success"] is False and "rate limit" in logged["error"]


# ── _list_channels ────────────────────────────────────────────────────────────

@patch("mcp_server.server.get_db")
def test_list_channels_lists_id_and_name(mock_get_db):
    db = MagicMock()
    db.list_channels.return_value = [
        {"id": "ch1", "source": "mm", "name": "dev", "display_name": "Dev", "channel_type": "O"},
    ]
    mock_get_db.return_value = db
    out = asyncio.run(_list_channels({}))
    assert "ch1" in out[0].text and "Dev" in out[0].text


@patch("mcp_server.server.get_db")
def test_list_channels_includes_description_when_present(mock_get_db):
    db = MagicMock()
    db.list_channels.return_value = [
        {"id": "ch1", "source": "mm", "name": "dev", "display_name": "Dev",
         "description": "team announcements", "channel_type": "O"},
        {"id": "ch2", "source": "mm", "name": "off", "display_name": "Off",
         "description": None, "channel_type": "O"},
    ]
    mock_get_db.return_value = db
    out = asyncio.run(_list_channels({}))
    assert "team announcements" in out[0].text
    # No description line for the second channel
    assert out[0].text.count("_") >= 2  # one pair around the description


@patch("mcp_server.server.get_db")
def test_list_channels_empty_prompts_ingest(mock_get_db):
    db = MagicMock()
    db.list_channels.return_value = []
    mock_get_db.return_value = db
    out = asyncio.run(_list_channels({}))
    assert "no channels" in out[0].text.lower()


# ── _build_aliases_text ───────────────────────────────────────────────────────

def test_build_aliases_text_shows_my_aliases():
    config = {"my_aliases": ["john", "johnd"]}
    text = _build_aliases_text(config)
    assert "john" in text
    assert "Current User" in text


def test_build_aliases_text_shows_user_aliases():
    config = {
        "user_aliases": [{"canonical_name": "Jane Smith", "aliases": ["jane", "jsmith"]}]
    }
    text = _build_aliases_text(config)
    assert "Jane Smith" in text
    assert "jane" in text


def test_build_aliases_text_no_config_returns_placeholder():
    text = _build_aliases_text({})
    assert "No aliases" in text


def test_build_aliases_text_marks_internal_users():
    config = {
        "user_aliases": [
            {"canonical_name": "Jane Smith", "internal": True, "aliases": ["jane"]},
            {"canonical_name": "Bob Wilson", "aliases": ["bob"]},
        ]
    }
    text = _build_aliases_text(config)
    assert "Jane Smith** [internal]" in text
    assert "Bob Wilson**:" in text  # no [internal] tag


def test_build_aliases_text_shows_role_and_team_inline():
    config = {
        "user_aliases": [
            {"canonical_name": "Jane Smith", "internal": True, "aliases": ["jane"],
             "role": "Backend lead", "team": "Platform"},
        ]
    }
    text = _build_aliases_text(config)
    assert "**Jane Smith** [internal] — Backend lead (Platform): jane" in text


def test_build_aliases_text_role_only_no_team():
    config = {
        "user_aliases": [
            {"canonical_name": "Bob Wilson", "aliases": ["bob"], "role": "CTO @ Acme"},
        ]
    }
    text = _build_aliases_text(config)
    assert "**Bob Wilson** — CTO @ Acme: bob" in text


def test_build_aliases_text_team_only_no_role():
    config = {
        "user_aliases": [
            {"canonical_name": "X", "aliases": ["x"], "team": "Platform"},
        ]
    }
    text = _build_aliases_text(config)
    assert "**X** — (Platform): x" in text


def test_build_aliases_text_shows_reports_to_and_responsible_for():
    config = {
        "user_aliases": [
            {"canonical_name": "Alex Petrov", "internal": True, "aliases": ["alex"],
             "role": "Junior backend", "team": "Platform",
             "reports_to": "Jane Smith",
             "responsible_for": ["dev-pl", "PL-*"]},
        ]
    }
    text = _build_aliases_text(config)
    assert "**Alex Petrov** [internal] — Junior backend (Platform): alex" in text
    assert "reports to Jane Smith" in text
    assert "responsible for: dev-pl, PL-*" in text


def test_build_aliases_text_responsible_for_string_form():
    config = {
        "user_aliases": [
            {"canonical_name": "Sam", "aliases": ["sam"],
             "responsible_for": "the deploy pipeline"},
        ]
    }
    text = _build_aliases_text(config)
    assert "responsible for: the deploy pipeline" in text


def test_build_aliases_text_omits_extras_when_unset():
    config = {
        "user_aliases": [
            {"canonical_name": "Bare", "aliases": ["b"]},
        ]
    }
    text = _build_aliases_text(config)
    assert "**Bare**: b" in text
    assert "reports to" not in text
    assert "responsible for" not in text


# ── _find_cached_file ─────────────────────────────────────────────────────────

def test_find_cached_file_returns_path_for_existing_file(tmp_path):
    f = tmp_path / "AgACBQADtest.jpg"
    f.write_bytes(b"\xff\xd8\xff")
    found = _find_cached_file(tmp_path, "AgACBQADtest")
    assert found == f


def test_find_cached_file_returns_none_when_missing(tmp_path):
    assert _find_cached_file(tmp_path, "nonexistent") is None


def test_find_cached_file_matches_any_extension(tmp_path):
    (tmp_path / "abc123.txt").write_text("hello")
    found = _find_cached_file(tmp_path, "abc123")
    assert found is not None
    assert found.suffix == ".txt"


# ── _serve_file_content ───────────────────────────────────────────────────────

def test_serve_file_content_text_returns_decoded():
    result = _serve_file_content(b"hello world", ".txt", {".txt"})
    assert result["content"] == "hello world"
    assert result["size"] == len(b"hello world")
    assert result["content_type"] == "text/plain"
    assert result["file_path"] is None


def test_serve_file_content_binary_returns_base64():
    result = _serve_file_content(b"\xff\xd8\xff", ".jpg", {".txt"})
    assert result["content"].startswith("[base64]:")
    assert result["content_type"] == "image/jpeg"
    assert result["size"] == 3


def test_serve_file_content_no_extension_returns_text():
    result = _serve_file_content(b"plain text", "", {".txt"})
    assert result["content"] == "plain text"


def test_serve_file_content_includes_resolved_file_path(tmp_path):
    p = tmp_path / "img.jpg"
    p.write_bytes(b"\xff\xd8\xff")
    result = _serve_file_content(p.read_bytes(), ".jpg", {".txt"}, file_path=p)
    assert result["file_path"] == str(p.resolve())


# ── _try_telegram_download ────────────────────────────────────────────────────


@rsps_lib.activate
def test_try_telegram_download_returns_text_for_text_file(tmp_path):
    token = "99999:TESTTOKEN"
    file_id = "txtfile123"
    rsps_lib.add(
        rsps_lib.GET,
        f"https://api.telegram.org/bot{token}/getFile",
        json={"ok": True, "result": {"file_path": "documents/file.txt"}},
    )
    rsps_lib.add(
        rsps_lib.GET,
        f"https://api.telegram.org/file/bot{token}/documents/file.txt",
        body=b"hello from telegram",
    )

    result = _try_telegram_download(token, file_id, tmp_path, {".txt"})
    cached = tmp_path / f"{file_id}.txt"
    assert cached.exists()
    assert result["content"] == "hello from telegram"
    assert result["file_path"] == str(cached.resolve())
    assert result["size"] == len(b"hello from telegram")


@rsps_lib.activate
def test_try_telegram_download_returns_base64_for_photo(tmp_path):
    token = "99999:TESTTOKEN"
    file_id = "photo456"
    rsps_lib.add(
        rsps_lib.GET,
        f"https://api.telegram.org/bot{token}/getFile",
        json={"ok": True, "result": {"file_path": "photos/photo.jpg"}},
    )
    rsps_lib.add(
        rsps_lib.GET,
        f"https://api.telegram.org/file/bot{token}/photos/photo.jpg",
        body=b"\xff\xd8\xff",
    )

    result = _try_telegram_download(token, file_id, tmp_path, {".txt"})
    cached = tmp_path / f"{file_id}.jpg"
    assert result is not None
    assert result["content"].startswith("[base64]:")
    assert result["file_path"] == str(cached.resolve())
    assert result["content_type"] == "image/jpeg"


@rsps_lib.activate
def test_try_telegram_download_returns_none_on_api_error(tmp_path):
    token = "99999:TESTTOKEN"
    rsps_lib.add(
        rsps_lib.GET,
        f"https://api.telegram.org/bot{token}/getFile",
        json={"ok": False, "description": "file not found"},
    )
    result = _try_telegram_download(token, "badid", tmp_path, {".txt"})
    assert result is None


# ── channel descriptions ──────────────────────────────────────────────────────

def test_short_description_collapses_whitespace_and_passes_short_text():
    assert _short_description("hello\n\nworld") == "hello world"
    assert _short_description(None) is None
    assert _short_description("") is None


def test_short_description_truncates_long_text():
    desc = "x" * 500
    out = _short_description(desc)
    assert out.endswith("…")
    assert len(out) <= 300


def test_build_channel_desc_lookup_keys_by_source_and_display_name():
    db = MagicMock()
    db.list_channels.return_value = [
        {"id": "1", "source": "mm", "display_name": "Dev", "description": "team"},
        {"id": "2", "source": "mm", "display_name": "Off", "description": None},
        {"id": "3", "source": "tg", "display_name": "Dev", "description": "other team"},
    ]
    out = _build_channel_desc_lookup(db)
    assert out[("mm", "Dev")] == "team"
    assert out[("tg", "Dev")] == "other team"
    assert ("mm", "Off") not in out


@patch("mcp_server.server.get_db")
@patch("mcp_server.server.get_config")
def test_summarize_messages_includes_channel_description(mock_cfg, mock_get_db):
    mock_cfg.return_value = {"sources": {}, "display_timezone": "UTC"}
    db = MagicMock()
    db.search.return_value = [
        {"id": "mm:1", "source": "mm", "channel": "Dev", "sender": "alice",
         "timestamp": "2026-05-27T10:00:00+00:00", "text": "hi", "raw": {}, "internal": 1},
    ]
    db.list_channels.return_value = [
        {"id": "ch1", "source": "mm", "display_name": "Dev",
         "description": "team announcements"},
    ]
    mock_get_db.return_value = db

    out = asyncio.run(_summarize_messages({"hours": 1}))
    assert "team announcements" in out[0].text


# ── _find_by_issue ────────────────────────────────────────────────────────────

def test_find_by_issue_requires_issue_id():
    out = asyncio.run(_find_by_issue({}))
    assert "required" in out[0].text.lower()


@patch("mcp_server.server.get_config")
@patch("mcp_server.server.get_db")
def test_find_by_issue_returns_no_match_text(mock_get_db, mock_cfg):
    mock_cfg.return_value = {"sources": {}}
    db = MagicMock()
    db.find_by_issue_id.return_value = []
    mock_get_db.return_value = db
    out = asyncio.run(_find_by_issue({"issue_id": "PL-999"}))
    assert "no messages found" in out[0].text.lower()


@patch("mcp_server.server.get_config")
@patch("mcp_server.server.get_db")
def test_find_by_issue_formats_matching_messages(mock_get_db, mock_cfg):
    mock_cfg.return_value = {"sources": {"mm": {"type": "mattermost", "url": "u"}},
                             "display_timezone": "UTC"}
    db = MagicMock()
    db.find_by_issue_id.return_value = [
        {"id": "mm:1", "source": "mm", "channel": "Dev",
         "sender": "alice", "timestamp": "2026-05-27T10:00:00+00:00",
         "text": "see PL-1", "raw": {}, "internal": 1},
    ]
    mock_get_db.return_value = db
    out = asyncio.run(_find_by_issue({"issue_id": "PL-1"}))
    assert "PL-1" in out[0].text and "alice" in out[0].text
    db.find_by_issue_id.assert_called_once_with("PL-1", limit=50)


# ── _who_mentioned ───────────────────────────────────────────────────────────

def _wm_config(my_aliases=None, user_aliases=None):
    return {
        "sources": {"mm": {"type": "mattermost", "url": "u"}},
        "display_timezone": "UTC",
        "my_aliases": my_aliases or [],
        "user_aliases": user_aliases or [],
    }


def test_who_mentioned_missing_target_returns_error():
    out = asyncio.run(_who_mentioned({}))
    assert "required" in out[0].text.lower()


@patch("mcp_server.server.get_config")
@patch("mcp_server.server.get_db")
def test_who_mentioned_resolves_alias_and_queries(mock_get_db, mock_cfg):
    mock_cfg.return_value = _wm_config(user_aliases=[
        {"canonical_name": "Bob Wilson", "aliases": ["bob", "bobw"]}
    ])
    db = MagicMock()
    db.get_mentions.return_value = [
        {"id": "mm:1", "source": "mm", "channel": "Dev",
         "sender": "alice", "timestamp": "2026-05-27T10:00:00+00:00",
         "text": "ping @bob", "raw": {}, "internal": 1,
         "mentioned_token": "@bob", "mentioned_canonical": "Bob Wilson"},
    ]
    mock_get_db.return_value = db

    out = asyncio.run(_who_mentioned({"target": "bobw"}))

    call_kwargs = db.get_mentions.call_args[1]
    assert call_kwargs["mentioned_canonical"] == "Bob Wilson"
    assert "Bob Wilson" in out[0].text
    assert "@bob" in out[0].text  # token marker


@patch("mcp_server.server.get_config")
@patch("mcp_server.server.get_db")
def test_who_mentioned_me_uses_identity_widening(mock_get_db, mock_cfg):
    """For target='me', the handler routes through get_mentions_for_identity so it
    matches mentions stored under any of {canonical, @-token, sender_id}."""
    mock_cfg.return_value = _wm_config(my_aliases=["john", "johnd"])
    db = MagicMock()
    db.get_mentions_for_identity.return_value = []
    db.find_sender_id_by_username.return_value = None  # nothing cached
    mock_get_db.return_value = db

    asyncio.run(_who_mentioned({"target": "me"}))

    db.get_mentions.assert_not_called()
    call_kwargs = db.get_mentions_for_identity.call_args[1]
    assert call_kwargs["canonicals"] == ["john"]
    assert call_kwargs["tokens"] == ["@john", "@johnd"]
    # No senders cached → sender_ids list is empty (handler silently skips misses).
    assert call_kwargs["sender_ids"] == []


@patch("mcp_server.server.get_config")
@patch("mcp_server.server.get_db")
def test_who_mentioned_me_finds_rows_when_only_token_matches(mock_get_db, mock_cfg):
    """End-to-end: a mention stored with mentioned_canonical=NULL and only the
    raw token populated must still be returned by target='me'. This is the live-
    data shape on a fresh ingest (before the ordering fix backfills sender_ids)."""
    mock_cfg.return_value = _wm_config(my_aliases=["john.doe"])
    db = MagicMock()
    db.find_sender_id_by_username.return_value = None
    db.get_mentions_for_identity.return_value = [{
        "id": "mm:1", "source": "mm", "channel": "Dev",
        "sender": "bob", "timestamp": "2026-05-27T10:00:00+00:00",
        "text": "ping @john.doe", "raw": {}, "internal": 1,
        "mentioned_token": "@john.doe",
        "mentioned_canonical": None,
        "mentioned_sender_id": None,
    }]
    mock_get_db.return_value = db

    out = asyncio.run(_who_mentioned({"target": "me"}))
    text = out[0].text
    assert "john.doe" in text
    assert "@john.doe" in text  # token marker preserved


@patch("mcp_server.server.get_config")
@patch("mcp_server.server.get_db")
def test_who_mentioned_me_includes_resolved_sender_ids(mock_get_db, mock_cfg):
    """When my_aliases entries map to real senders.username rows across sources,
    those sender_ids are included in the identity widening so future-ingest rows
    (which DO have mentioned_sender_id populated) also match."""
    mock_cfg.return_value = _wm_config(my_aliases=["john.doe", "Иван Иванов"])
    db = MagicMock()
    # First positional is source name, second is the alias being looked up.

    def fake_lookup(source, alias):
        if source == "mm" and alias == "john.doe":
            return "mm_uid_42"
        return None
    db.find_sender_id_by_username.side_effect = fake_lookup
    db.get_mentions_for_identity.return_value = []
    mock_get_db.return_value = db

    asyncio.run(_who_mentioned({"target": "me"}))
    call_kwargs = db.get_mentions_for_identity.call_args[1]
    assert call_kwargs["sender_ids"] == ["mm_uid_42"]


@patch("mcp_server.server.get_config")
def test_who_mentioned_me_without_my_aliases_errors(mock_cfg):
    mock_cfg.return_value = _wm_config(my_aliases=[])
    out = asyncio.run(_who_mentioned({"target": "me"}))
    assert "my_aliases" in out[0].text


@patch("mcp_server.server.get_config")
@patch("mcp_server.server.get_db")
def test_who_mentioned_by_filter_resolved(mock_get_db, mock_cfg):
    mock_cfg.return_value = _wm_config(user_aliases=[
        {"canonical_name": "Alice Adams", "aliases": ["alice"]},
        {"canonical_name": "Bob Wilson", "aliases": ["bob"]},
    ])
    db = MagicMock()
    db.get_mentions.return_value = []
    mock_get_db.return_value = db

    asyncio.run(_who_mentioned({"target": "bob", "by": "alice"}))

    call_kwargs = db.get_mentions.call_args[1]
    assert call_kwargs["mentioned_canonical"] == "Bob Wilson"
    assert call_kwargs["sender_canonical"] == "Alice Adams"


@patch("mcp_server.server.get_config")
@patch("mcp_server.server.get_db")
def test_who_mentioned_empty_result_returns_message(mock_get_db, mock_cfg):
    mock_cfg.return_value = _wm_config()
    db = MagicMock()
    db.get_mentions.return_value = []
    mock_get_db.return_value = db

    out = asyncio.run(_who_mentioned({"target": "Bob"}))
    assert "No mentions of 'Bob'" in out[0].text


@patch("mcp_server.server.get_config")
@patch("mcp_server.server.get_db")
def test_who_mentioned_passes_source_since_until_limit(mock_get_db, mock_cfg):
    mock_cfg.return_value = _wm_config()
    db = MagicMock()
    db.get_mentions.return_value = []
    mock_get_db.return_value = db

    asyncio.run(_who_mentioned({
        "target": "Bob", "source": "mm",
        "since": "2026-05-20T00:00:00", "until": "2026-05-28T00:00:00",
        "limit": 10,
    }))
    call_kwargs = db.get_mentions.call_args[1]
    assert call_kwargs["source"] == "mm"
    assert call_kwargs["since"] == "2026-05-20T00:00:00"
    assert call_kwargs["until"] == "2026-05-28T00:00:00"
    assert call_kwargs["limit"] == 10


# ── email source dispatch (TASK-012) ─────────────────────────────────────────

def _email_config():
    return {"sources": {"em": {
        "type": "email", "host": "h", "port": 993,
        "username": "me@example.com", "password": "p",
        "allow_send": True,
    }}}


@patch("mcp_server.server.get_config")
def test_get_new_messages_punts_for_email_source(mock_cfg):
    mock_cfg.return_value = _email_config()
    out = asyncio.run(_get_new_messages({"source": "em", "channel": "folder:INBOX"}))
    assert "Live fetch is not supported for IMAP" in out[0].text


@patch("mcp_server.server.get_db")
@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_send_message_email_dispatch_returns_draft_marker(mock_cfg, mock_build, mock_get_db):
    mock_cfg.return_value = _email_config()
    fake_conn = MagicMock()
    fake_conn.send_message.return_value = "<new-id@example.com>"
    mock_build.return_value = fake_conn
    mock_get_db.return_value = MagicMock()

    out = asyncio.run(_send_message({
        "source": "em", "channel": "folder:INBOX",
        "text": "hi", "reply_to": "<root@x>",
    }))
    import json as _json
    payload = _json.loads(out[0].text)
    assert payload["success"] is True
    assert payload["message_id"] == "<new-id@example.com>"
    assert payload["draft"] is True
    assert "Drafts folder" in payload["note"]
    fake_conn.send_message.assert_called_once_with("folder:INBOX", "hi", reply_to="<root@x>")


@patch("mcp_server.server._build_live_connector")
@patch("mcp_server.server.get_config")
def test_send_message_email_refuses_without_allow_send(mock_cfg, mock_build):
    mock_cfg.return_value = {"sources": {"em": {
        "type": "email", "host": "h", "username": "u", "password": "p",
    }}}  # allow_send absent → False by default
    out = asyncio.run(_send_message({
        "source": "em", "channel": "folder:INBOX",
        "text": "hi", "reply_to": "<root@x>",
    }))
    assert "Sending is disabled" in out[0].text
    mock_build.assert_not_called()


# ── user_aliases write tools (TASK-029) ──────────────────────────────────────


_ALIAS_SEED_YAML = """\
my_aliases:
  - "John Doe"
  - "johnd"
user_aliases:
  - canonical_name: "Jane Smith"
    aliases: ["jane", "jsmith"]
    role: "Backend lead"
  - canonical_name: "Bob Wilson"
    aliases: ["bob", "bwilson"]
"""


def _alias_cfg(tmp_path, extra: str = "") -> str:
    """Write a config.yaml with the alias seed + any extra top-level lines, return its path.

    Also points the MCP server's global `_config_path` at it and clears the cache
    so the handlers operate on this test config.
    """
    p = tmp_path / "config.yaml"
    p.write_text(_ALIAS_SEED_YAML + extra)
    import mcp_server.server as srv
    srv._config_path = str(p)
    srv._config = None
    srv._db = None
    return str(p)


# upsert_user_alias

def test_upsert_user_alias_creates_new_entry(tmp_path):
    path = _alias_cfg(tmp_path)
    out = asyncio.run(_upsert_user_alias({
        "canonical_name": "Carol Doe", "role": "Designer",
        "aliases": ["carol", "cd"],
    }))
    payload = _json.loads(out[0].text)
    assert payload["ok"] is True
    assert payload["entry"]["canonical_name"] == "Carol Doe"
    assert payload["entry"]["role"] == "Designer"
    # File on disk reflects it.
    assert "Carol Doe" in open(path).read()


def test_upsert_user_alias_merges_into_existing(tmp_path):
    path = _alias_cfg(tmp_path)
    asyncio.run(_upsert_user_alias({
        "canonical_name": "Jane Smith",
        "aliases": ["jane.s"],          # adds; jane/jsmith preserved
        "role": "Engineering lead",      # overwrites
        "responsible_for": ["PL-*"],     # new list field
    }))
    text = open(path).read()
    assert "Engineering lead" in text
    assert "jane.s" in text
    assert "Backend lead" not in text  # was overwritten


def test_upsert_user_alias_refuses_when_disabled(tmp_path):
    _alias_cfg(tmp_path, extra="\nallow_alias_edits: false\n")
    out = asyncio.run(_upsert_user_alias({"canonical_name": "Carol"}))
    assert "disabled" in out[0].text.lower()


def test_upsert_user_alias_refuses_my_aliases_target(tmp_path):
    _alias_cfg(tmp_path)
    out = asyncio.run(_upsert_user_alias({"canonical_name": "john doe"}))  # case-insensitive
    assert "my_aliases" in out[0].text


def test_upsert_user_alias_refuses_alias_owned_by_other(tmp_path):
    _alias_cfg(tmp_path)
    out = asyncio.run(_upsert_user_alias({
        "canonical_name": "Jane Smith",
        "aliases": ["bob"],   # owned by Bob Wilson
    }))
    assert "already owned" in out[0].text
    assert "Bob Wilson" in out[0].text


def test_upsert_user_alias_enforces_max_aliases_cap(tmp_path):
    _alias_cfg(tmp_path, extra="\nmax_aliases_per_entry: 3\n")
    out = asyncio.run(_upsert_user_alias({
        "canonical_name": "Carol Doe",
        "aliases": ["a", "b", "c", "d"],
    }))
    assert "max_aliases_per_entry" in out[0].text


def test_upsert_user_alias_enforces_max_entries_cap(tmp_path):
    # max_entries=2, seed has 2 → adding a third should refuse.
    _alias_cfg(tmp_path, extra="\nmax_entries: 2\n")
    out = asyncio.run(_upsert_user_alias({"canonical_name": "Carol Doe"}))
    assert "max_entries" in out[0].text


def test_upsert_user_alias_invalidates_config_cache(tmp_path):
    _alias_cfg(tmp_path)
    import mcp_server.server as srv
    # Prime the cache:
    _ = srv.get_config()
    assert srv._config is not None
    asyncio.run(_upsert_user_alias({"canonical_name": "Carol Doe"}))
    assert srv._config is None  # cache invalidated


# remove_user_alias

def test_remove_user_alias_returns_removed_entry(tmp_path):
    path = _alias_cfg(tmp_path)
    out = asyncio.run(_remove_user_alias({"canonical_name": "Bob Wilson"}))
    payload = _json.loads(out[0].text)
    assert payload["ok"] is True
    assert payload["removed"]["canonical_name"] == "Bob Wilson"
    assert "Bob Wilson" not in open(path).read()


def test_remove_user_alias_case_insensitive(tmp_path):
    _alias_cfg(tmp_path)
    out = asyncio.run(_remove_user_alias({"canonical_name": "bob WILSON"}))
    payload = _json.loads(out[0].text)
    assert payload["ok"] is True


def test_remove_user_alias_missing_returns_not_found(tmp_path):
    _alias_cfg(tmp_path)
    out = asyncio.run(_remove_user_alias({"canonical_name": "Ghost"}))
    assert "No entry found" in out[0].text


def test_remove_user_alias_refuses_my_aliases_target(tmp_path):
    _alias_cfg(tmp_path)
    out = asyncio.run(_remove_user_alias({"canonical_name": "John Doe"}))
    assert "my_aliases" in out[0].text


def test_remove_user_alias_refuses_when_disabled(tmp_path):
    _alias_cfg(tmp_path, extra="\nallow_alias_edits: false\n")
    out = asyncio.run(_remove_user_alias({"canonical_name": "Bob Wilson"}))
    assert "disabled" in out[0].text.lower()


# update_user_alias_strings

def test_update_user_alias_strings_adds_and_removes(tmp_path):
    _alias_cfg(tmp_path)
    out = asyncio.run(_update_user_alias_strings({
        "canonical_name": "Jane Smith",
        "add": ["jane.smith"],
        "remove": ["jsmith"],
    }))
    payload = _json.loads(out[0].text)
    assert payload["ok"] is True
    aliases = payload["entry"]["aliases"]
    assert "jane.smith" in aliases
    assert "jsmith" not in aliases
    assert "jane" in aliases  # untouched


def test_update_user_alias_strings_refuses_to_steal_from_other(tmp_path):
    _alias_cfg(tmp_path)
    out = asyncio.run(_update_user_alias_strings({
        "canonical_name": "Jane Smith", "add": ["bob"],
    }))
    assert "owned by" in out[0].text
    assert "Bob Wilson" in out[0].text


def test_update_user_alias_strings_refuses_to_empty(tmp_path):
    _alias_cfg(tmp_path)
    out = asyncio.run(_update_user_alias_strings({
        "canonical_name": "Bob Wilson", "remove": ["bob", "bwilson"],
    }))
    assert "no aliases" in out[0].text.lower()


def test_update_user_alias_strings_refuses_my_aliases(tmp_path):
    _alias_cfg(tmp_path)
    out = asyncio.run(_update_user_alias_strings({
        "canonical_name": "John Doe", "add": ["jd"],
    }))
    assert "my_aliases" in out[0].text


def test_update_user_alias_strings_requires_at_least_one(tmp_path):
    _alias_cfg(tmp_path)
    out = asyncio.run(_update_user_alias_strings({"canonical_name": "Jane Smith"}))
    assert "at least one" in out[0].text.lower()


def test_update_user_alias_strings_enforces_cap(tmp_path):
    _alias_cfg(tmp_path, extra="\nmax_aliases_per_entry: 3\n")
    # Jane has 2 aliases; adding 2 more would push to 4.
    out = asyncio.run(_update_user_alias_strings({
        "canonical_name": "Jane Smith", "add": ["a", "b"],
    }))
    assert "max_aliases_per_entry" in out[0].text


# ── args_summary redaction (TASK-026) ────────────────────────────────────────

def test_args_summary_send_message_omits_text():
    s = _args_summary_for("send_message", {
        "source": "mm", "channel": "ch1",
        "text": "very secret message body that should not leak",
        "reply_to": "abc123",
    })
    assert "secret" not in s
    payload = _json.loads(s)
    assert payload["source"] == "mm"
    assert payload["channel"] == "ch1"
    assert payload["text_len"] == len("very secret message body that should not leak")
    assert payload["has_reply_to"] is True


def test_args_summary_send_message_no_reply_to_flag_false():
    payload = _json.loads(_args_summary_for("send_message",
                                            {"source": "mm", "channel": "c", "text": "x"}))
    assert payload["has_reply_to"] is False


def test_args_summary_search_messages_caps_query():
    long_query = "x" * 500
    payload = _json.loads(_args_summary_for("search_messages", {
        "query": long_query, "mode": "semantic", "limit": 10,
    }))
    assert payload["mode"] == "semantic"
    assert payload["limit"] == 10
    assert len(payload["query"]) <= 121  # 120 chars + ellipsis


def test_args_summary_alias_write_tools_only_record_canonical():
    for tool in ("upsert_user_alias", "remove_user_alias", "update_user_alias_strings"):
        s = _args_summary_for(tool, {
            "canonical_name": "Bob",
            "aliases": ["sensitive", "private"],
            "role": "Secret role",
        })
        assert "sensitive" not in s
        assert "Secret" not in s
        assert _json.loads(s) == {"canonical_name": "Bob"}


def test_args_summary_unknown_tool_falls_back_to_verbatim():
    payload = _json.loads(_args_summary_for("get_health", {"foo": "bar"}))
    assert payload == {"foo": "bar"}


# ── call_tool wrapper logs to tool_calls (TASK-026) ──────────────────────────

async def _fake_ok(name, args):
    return [_TC(type="text", text="ok")]


async def _fake_boom(name, args):
    raise RuntimeError("upstream 500")


@patch("mcp_server.server.get_db")
def test_call_tool_wrapper_logs_success_row(mock_get_db):
    db = MagicMock()
    mock_get_db.return_value = db
    with patch("mcp_server.server._dispatch_tool", side_effect=_fake_ok):
        asyncio.run(call_tool("get_health", {}))
    db.log_tool_call.assert_called_once()
    kwargs = db.log_tool_call.call_args.kwargs
    assert kwargs["tool_name"] == "get_health"
    assert kwargs["success"] is True
    assert kwargs["duration_ms"] >= 0


@patch("mcp_server.server.get_db")
def test_call_tool_wrapper_logs_failure_and_re_raises(mock_get_db):
    db = MagicMock()
    mock_get_db.return_value = db
    with patch("mcp_server.server._dispatch_tool", side_effect=_fake_boom):
        with _pytest.raises(RuntimeError, match="upstream 500"):
            asyncio.run(call_tool("get_health", {}))
    db.log_tool_call.assert_called_once()
    kwargs = db.log_tool_call.call_args.kwargs
    assert kwargs["success"] is False
    assert "upstream 500" in kwargs["error"]


@patch("mcp_server.server.get_db")
def test_call_tool_logging_failure_does_not_break_tool_response(mock_get_db):
    """Even if log_tool_call itself raises, the tool result must still come through."""
    db = MagicMock()
    db.log_tool_call.side_effect = RuntimeError("logger died")
    mock_get_db.return_value = db
    with patch("mcp_server.server._dispatch_tool", side_effect=_fake_ok):
        out = asyncio.run(call_tool("get_health", {}))
    assert out[0].text == "ok"
