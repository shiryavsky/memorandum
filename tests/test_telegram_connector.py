"""Tests for connectors/telegram_connector.py using the responses library."""
from unittest.mock import MagicMock
import pytest
import responses as rsps

from connectors.telegram_connector import TelegramConnector

TOKEN = "12345:ABCDEF"
API_BASE = f"https://api.telegram.org/bot{TOKEN}"


def _make_connector(source_name="test_tg", **kwargs):
    return TelegramConnector(bot_token=TOKEN, source_name=source_name, **kwargs)


def _me_response():
    return {"ok": True, "result": {"id": 99, "username": "testbot", "is_bot": True}}


def _updates(*msgs, offset=None):
    """Build a getUpdates response with the given message dicts (supergroup)."""
    updates = []
    for i, msg in enumerate(msgs):
        updates.append({"update_id": 1000 + i, "message": msg})
    return {"ok": True, "result": updates}


def _channel_updates(*msgs):
    """Build a getUpdates response with channel_post entries."""
    updates = []
    for i, msg in enumerate(msgs):
        updates.append({"update_id": 2000 + i, "channel_post": msg})
    return {"ok": True, "result": updates}


def _business_updates(*msgs):
    """Build a getUpdates response with business_message entries."""
    updates = []
    for i, msg in enumerate(msgs):
        updates.append({"update_id": 3000 + i, "business_message": msg})
    return {"ok": True, "result": updates}


def _make_msg(chat_id=-1001234567890, message_id=1, text="hello", chat_type="supergroup"):
    chat: dict = {"id": chat_id, "type": chat_type}
    if chat_type in ("supergroup", "group"):
        chat["title"] = "Test Group"
    else:
        chat["first_name"] = "Alice"
    return {
        "message_id": message_id,
        "date": 1705312200,
        "chat": chat,
        "from": {"id": 42, "username": "user42", "first_name": "User", "last_name": "FortyTwo"},
        "text": text,
    }


def _make_private_msg(message_id=1, text="hi bot"):
    """DM sent directly to the bot."""
    return {
        "message_id": message_id,
        "date": 1705312200,
        "chat": {"id": 8575109205, "type": "private", "first_name": "Alice"},
        "from": {"id": 8575109205, "username": "alice"},
        "text": text,
    }


# ── connect() ─────────────────────────────────────────────────────────────────

@rsps.activate
def test_connect_calls_getme_and_logs_username():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    conn = _make_connector()
    conn.connect()
    assert conn._bot_info.get("username") == "testbot"


@rsps.activate
def test_connect_raises_on_api_error():
    rsps.add(rsps.GET, f"{API_BASE}/getMe",
             json={"ok": False, "description": "Unauthorized"})
    conn = _make_connector()
    with pytest.raises(RuntimeError):
        conn.connect()


# ── fetch_messages: offset handling ──────────────────────────────────────────

@rsps.activate
def test_fetch_messages_no_saved_offset_fetches_from_beginning():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json=_updates(_make_msg()))
    # Second call returns empty (stops the loop)
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": []})

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages_count"] == 1
    # First getUpdates call should have no offset param
    req = rsps.calls[1].request
    assert "offset" not in (req.url.split("?")[1] if "?" in req.url else "")


@rsps.activate
def test_fetch_messages_with_saved_offset_passes_it():
    saved_offset = 5000
    db_callback = MagicMock(return_value=saved_offset)

    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": []})

    conn = _make_connector(db_callback=db_callback)
    conn.connect()
    conn.fetch_messages()

    req = rsps.calls[1].request
    assert f"offset={saved_offset}" in req.url


@rsps.activate
def test_fetch_messages_stops_when_updates_empty():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": []})

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()
    assert result["messages_count"] == 0


# ── message dict format ───────────────────────────────────────────────────────

@rsps.activate
def test_message_id_format():
    chat_id = -1001234567890
    message_id = 42

    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json=_updates(_make_msg(chat_id=chat_id, message_id=message_id)))
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": []})

    conn = _make_connector(source_name="my_tg")
    conn.connect()
    result = conn.fetch_messages()

    msg = result["messages"][0]
    assert msg["id"] == f"my_tg:{chat_id}:{message_id}"


# ── chat_ids whitelist ────────────────────────────────────────────────────────

@rsps.activate
def test_chat_ids_whitelist_skips_non_matching():
    wanted_id = -1009999999999
    other_id = -1001234567890

    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json=_updates(
                 _make_msg(chat_id=other_id),
                 _make_msg(chat_id=wanted_id),
             ))
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": []})

    conn = _make_connector(chat_ids=[str(wanted_id)])
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages_count"] == 1
    assert result["messages"][0]["channel_id"] == str(wanted_id)


# ── _url_chat_id ──────────────────────────────────────────────────────────────

def test_url_chat_id_strips_minus100_prefix():
    conn = _make_connector()
    assert conn._url_chat_id(-1001234567890) == "1234567890"


def test_url_chat_id_strips_regular_minus():
    conn = _make_connector()
    assert conn._url_chat_id(-1234567) == "1234567"


# ── offset sentinel ───────────────────────────────────────────────────────────

@rsps.activate
def test_offset_sentinel_upserted_with_correct_id_and_source():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json=_updates(_make_msg(message_id=7)))
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": []})

    mock_db = MagicMock()
    conn = _make_connector(source_name="my_tg", db=mock_db)
    conn.connect()
    conn.fetch_messages()

    upsert_calls = mock_db.upsert_channel.call_args_list
    sentinel_calls = [
        c for c in upsert_calls if c[0][0].get("id") == "__offset__"
    ]
    assert len(sentinel_calls) >= 1
    arg = sentinel_calls[0][0][0]
    assert arg["source"] == "my_tg"
    assert arg["id"] == "__offset__"


# ── DM filtering ──────────────────────────────────────────────────────────────

@rsps.activate
def test_private_dm_to_bot_is_skipped():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json=_updates(_make_private_msg()))
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": []})

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages_count"] == 0


@rsps.activate
def test_private_dm_skipped_but_group_message_kept():
    group_msg = _make_msg(chat_id=-1001234567890, message_id=5)

    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": [
                 {"update_id": 1000, "message": _make_private_msg()},
                 {"update_id": 1001, "message": group_msg},
             ]})
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": []})

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages_count"] == 1
    assert result["messages"][0]["channel_id"] == str(-1001234567890)


@rsps.activate
def test_business_message_private_chat_is_kept():
    """Business messages in private chats are kept (they're not DMs to the bot)."""
    biz_msg = _make_msg(chat_id=9153987, message_id=1, chat_type="private")

    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": [
                 {"update_id": 3000, "business_message": biz_msg},
             ]})
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": []})

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages_count"] == 1


@rsps.activate
def test_business_message_stores_connection_id_in_channel_extra():
    """A business_message persists its business_connection_id on the chat's extra."""
    biz_msg = _make_msg(chat_id=9153987, message_id=1, chat_type="private")
    biz_msg["business_connection_id"] = "bconn-xyz"

    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": [
                 {"update_id": 3000, "business_message": biz_msg},
             ]})
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json={"ok": True, "result": []})

    mock_db = MagicMock()
    conn = _make_connector(db=mock_db)
    conn.connect()
    conn.fetch_messages()

    upserted = next(c[0][0] for c in mock_db.upsert_channel.call_args_list
                    if c[0][0]["id"] == "9153987")
    assert upserted["extra"] == {"business_connection_id": "bconn-xyz"}


# ── channel_post ──────────────────────────────────────────────────────────────

@rsps.activate
def test_channel_post_is_collected():
    channel_msg = {
        "message_id": 10,
        "date": 1705312200,
        "chat": {"id": -1001111111111, "type": "channel", "title": "My Channel"},
        "text": "channel announcement",
    }

    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json=_channel_updates(channel_msg))
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": []})

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages_count"] == 1
    assert result["messages"][0]["text"] == "channel announcement"


@rsps.activate
def test_channel_post_raw_chat_type_is_channel():
    channel_msg = {
        "message_id": 10,
        "date": 1705312200,
        "chat": {"id": -1001111111111, "type": "channel", "title": "My Channel"},
        "text": "hello",
    }

    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json=_channel_updates(channel_msg))
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": []})

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    raw = result["messages"][0]["raw"]
    assert raw["chat_type"] == "channel"
    assert raw["chat_id"]  # not empty — linkable


# ── sender full name ──────────────────────────────────────────────────────────

@rsps.activate
def test_sender_full_name_stored_in_cache():
    msg = {
        "message_id": 1,
        "date": 1705312200,
        "chat": {"id": -1001234567890, "type": "supergroup", "title": "G"},
        "from": {"id": 77, "username": "jdoe", "first_name": "John", "last_name": "Doe"},
        "text": "hi",
    }

    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json=_updates(msg))
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json={"ok": True, "result": []})

    conn = _make_connector()
    conn.connect()
    conn.fetch_messages()

    info = conn.get_sender_info("77")
    assert info["full_name"] == "John Doe"
    assert info["username"] == "jdoe"


@rsps.activate
def test_sender_without_username_uses_full_name():
    msg = {
        "message_id": 1,
        "date": 1705312200,
        "chat": {"id": -1001234567890, "type": "supergroup", "title": "G"},
        "from": {"id": 88, "first_name": "Anna", "last_name": "K"},
        "text": "hello",
    }

    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json=_updates(msg))
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json={"ok": True, "result": []})

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    # sender field uses full name when no username
    assert result["messages"][0]["sender"] == "Anna K"


# ── file indicators ───────────────────────────────────────────────────────────

@rsps.activate
def test_photo_message_shows_indicator_in_text():
    msg = {
        "message_id": 1,
        "date": 1705312200,
        "chat": {"id": -1001234567890, "type": "supergroup", "title": "G"},
        "from": {"id": 42, "username": "u"},
        "photo": [
            {"file_id": "small_id", "file_unique_id": "s", "file_size": 100, "width": 90, "height": 34},
            {"file_id": "big_id", "file_unique_id": "b", "file_size": 5000, "width": 640, "height": 480},
        ],
    }

    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json=_updates(msg))
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json={"ok": True, "result": []})

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages_count"] == 1
    text = result["messages"][0]["text"]
    assert "[photo," in text
    assert "file_id=big_id" in text


@rsps.activate
def test_voice_message_shows_indicator():
    msg = {
        "message_id": 2,
        "date": 1705312200,
        "chat": {"id": -1001234567890, "type": "supergroup", "title": "G"},
        "from": {"id": 42, "username": "u"},
        "voice": {"file_id": "voice_id", "duration": 5, "mime_type": "audio/ogg"},
    }

    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json=_updates(msg))
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json={"ok": True, "result": []})

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    text = result["messages"][0]["text"]
    assert "[voice message," in text
    assert "file_id=voice_id" in text


# ── raw chat_id for linkable vs private chats ─────────────────────────────────

@rsps.activate
def test_supergroup_raw_chat_id_is_set():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json=_updates(_make_msg()))
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json={"ok": True, "result": []})

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    raw = result["messages"][0]["raw"]
    assert raw["chat_type"] == "supergroup"
    assert raw["chat_id"] != ""  # stripped -100 prefix, non-empty


@rsps.activate
def test_business_private_raw_chat_id_is_empty():
    """Private business messages get empty chat_id so no link is generated."""
    biz_msg = _make_msg(chat_id=9999, message_id=1, chat_type="private")

    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": [
                 {"update_id": 3000, "business_message": biz_msg},
             ]})
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json={"ok": True, "result": []})

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    raw = result["messages"][0]["raw"]
    assert raw["chat_id"] == ""  # no link for private chats


# ── fetch_new (live read-only) ────────────────────────────────────────────────

@rsps.activate
def test_fetch_new_filters_to_requested_chat():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json={"ok": True, "result": [
        {"update_id": 1000, "message": _make_msg(chat_id=-100111, message_id=1, text="in target")},
        {"update_id": 1001, "message": _make_msg(chat_id=-100222, message_id=2, text="other chat")},
    ]})

    conn = _make_connector()
    conn.connect()
    msgs = conn.fetch_new("-100111", limit=50)
    assert [m["text"] for m in msgs] == ["in target"]
    assert msgs[0]["channel"] == "Test Group"  # chat title attached


@rsps.activate
def test_fetch_new_does_not_save_offset_or_upsert():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json={"ok": True, "result": [
        {"update_id": 1000, "message": _make_msg(chat_id=-100111, message_id=1)},
    ]})

    mock_db = MagicMock()
    db_callback = MagicMock(return_value=500)  # saved offset
    conn = _make_connector(db=mock_db, db_callback=db_callback)
    conn.connect()
    conn.fetch_new("-100111")

    db_callback.assert_called_with("test_tg", "__offset__")  # offset read
    mock_db.upsert_channel.assert_not_called()                # nothing written


@rsps.activate
def test_fetch_new_makes_single_getupdates_call():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    # 100 updates would make fetch_messages loop again; fetch_new must NOT.
    big = {"ok": True, "result": [
        {"update_id": 1000 + i, "message": _make_msg(chat_id=-100111, message_id=i)}
        for i in range(100)
    ]}
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json=big)

    conn = _make_connector()
    conn.connect()
    conn.fetch_new("-100111", limit=50)

    getupdates_calls = [c for c in rsps.calls if "getUpdates" in c.request.url]
    assert len(getupdates_calls) == 1


@rsps.activate
def test_fetch_new_skips_dm_to_bot():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json={"ok": True, "result": [
        {"update_id": 1000, "message": _make_private_msg(message_id=1)},
    ]})

    conn = _make_connector()
    conn.connect()
    # filter by the private chat id; DM to bot must still be skipped
    assert conn.fetch_new("8575109205") == []


# ── send_message ──────────────────────────────────────────────────────────────

@rsps.activate
def test_send_message_returns_message_id():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.POST, f"{API_BASE}/sendMessage",
             json={"ok": True, "result": {"message_id": 555}})

    conn = _make_connector()
    conn.connect()
    msg_id = conn.send_message("-100111", "hello")

    assert msg_id == 555
    assert "hello" in rsps.calls[-1].request.body


@rsps.activate
def test_send_message_includes_reply_to():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.POST, f"{API_BASE}/sendMessage",
             json={"ok": True, "result": {"message_id": 556}})

    conn = _make_connector()
    conn.connect()
    conn.send_message("-100111", "reply text", reply_to=42)

    assert "reply_to_message_id=42" in rsps.calls[-1].request.body


@rsps.activate
def test_send_message_includes_business_connection_id():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.POST, f"{API_BASE}/sendMessage",
             json={"ok": True, "result": {"message_id": 557}})

    conn = _make_connector()
    conn.connect()
    conn.send_message("12345", "secretary reply", business_connection_id="bconn1")

    assert "business_connection_id=bconn1" in rsps.calls[-1].request.body


@rsps.activate
def test_send_message_auto_looks_up_business_connection_id_from_db():
    """When no explicit bcid is passed, the connector reads it off the channel row.

    Lets the MCP dispatcher stay generic: every connector's send_message takes
    just (channel, text, reply_to). Telegram-specific glue lives in the
    connector itself.
    """
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.POST, f"{API_BASE}/sendMessage",
             json={"ok": True, "result": {"message_id": 558}})

    db = MagicMock()
    db.get_channel_row.return_value = {"extra": {"business_connection_id": "bconn-stored"}}
    conn = _make_connector(db=db)
    conn.connect()
    conn.send_message("9153987", "hi")

    db.get_channel_row.assert_called_once_with("test_tg", "9153987")
    assert "business_connection_id=bconn-stored" in rsps.calls[-1].request.body


@rsps.activate
def test_send_message_skips_business_connection_id_when_unknown():
    """No row in DB -> no bcid kwarg sent. Telegram will reject if it was needed."""
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.POST, f"{API_BASE}/sendMessage",
             json={"ok": True, "result": {"message_id": 559}})

    db = MagicMock()
    db.get_channel_row.return_value = None
    conn = _make_connector(db=db)
    conn.connect()
    conn.send_message("-100999", "hi")

    body = rsps.calls[-1].request.body
    assert "business_connection_id" not in body


@rsps.activate
def test_send_message_business_peer_invalid_gives_actionable_error():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.POST, f"{API_BASE}/sendMessage",
             json={"ok": False, "error_code": 400, "description": "Bad Request: BUSINESS_PEER_INVALID"},
             status=400)

    conn = _make_connector()
    conn.connect()
    with pytest.raises(RuntimeError, match="reply rights"):
        conn.send_message("7548150", "hi", business_connection_id="bconn-xyz")


@rsps.activate
def test_send_message_surfaces_telegram_description_on_400():
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.POST, f"{API_BASE}/sendMessage",
             json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
             status=400)

    conn = _make_connector()
    conn.connect()
    with pytest.raises(Exception, match="chat not found"):
        conn.send_message("ghostchat", "hi")


# ── message_url ───────────────────────────────────────────────────────────────

def test_message_url_supergroup_strips_minus_100():
    conn = _make_connector()
    assert conn.message_url("-1001234567890", 42) == "https://t.me/c/1234567890/42"


def test_message_url_private_chat_returns_none():
    """Telegram private chats have no public link surface."""
    conn = _make_connector()
    assert conn.message_url("9153987", 42) is None


def test_message_url_no_message_id_returns_none():
    conn = _make_connector()
    assert conn.message_url("-1001234567890", None) is None
    assert conn.message_url("-1001234567890", "") is None


# ── chat description (getChat) ────────────────────────────────────────────────

@rsps.activate
def test_chat_description_fetched_and_stored_for_supergroup():
    msg = _make_msg(chat_id=-1001234567890, message_id=1, chat_type="supergroup")
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json=_updates(msg))
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json={"ok": True, "result": []})
    rsps.add(rsps.GET, f"{API_BASE}/getChat",
             json={"ok": True, "result": {"id": -1001234567890, "type": "supergroup",
                                          "title": "Test Group",
                                          "description": "deploy notifications channel"}})

    mock_db = MagicMock()
    conn = _make_connector(db=mock_db)
    conn.connect()
    conn.fetch_messages()

    upserted = next(c[0][0] for c in mock_db.upsert_channel.call_args_list
                    if c[0][0]["id"] == "-1001234567890")
    assert upserted["description"] == "deploy notifications channel"


@rsps.activate
def test_chat_description_skipped_for_private_chats():
    biz_msg = _make_msg(chat_id=9153987, message_id=1, chat_type="private")
    biz_msg["business_connection_id"] = "bconn-xyz"
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": [{"update_id": 3000, "business_message": biz_msg}]})
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json={"ok": True, "result": []})

    mock_db = MagicMock()
    conn = _make_connector(db=mock_db)
    conn.connect()
    conn.fetch_messages()

    upserted = next(c[0][0] for c in mock_db.upsert_channel.call_args_list
                    if c[0][0]["id"] == "9153987")
    assert upserted["description"] is None
    # getChat must not have been called for the private chat
    getchat_calls = [c for c in rsps.calls if "/getChat" in c.request.url]
    assert getchat_calls == []


@rsps.activate
def test_chat_description_cached_across_calls():
    msg1 = _make_msg(chat_id=-1001234567890, message_id=1, chat_type="supergroup")
    msg2 = _make_msg(chat_id=-1001234567890, message_id=2, chat_type="supergroup")
    rsps.add(rsps.GET, f"{API_BASE}/getMe", json=_me_response())
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates",
             json={"ok": True, "result": [
                 {"update_id": 1, "message": msg1},
                 {"update_id": 2, "message": msg2},
             ]})
    rsps.add(rsps.GET, f"{API_BASE}/getUpdates", json={"ok": True, "result": []})
    rsps.add(rsps.GET, f"{API_BASE}/getChat",
             json={"ok": True, "result": {"id": -1001234567890, "type": "supergroup",
                                          "title": "T", "description": "d"}})

    mock_db = MagicMock()
    conn = _make_connector(db=mock_db)
    conn.connect()
    conn.fetch_messages()

    getchat_calls = [c for c in rsps.calls if "/getChat" in c.request.url]
    assert len(getchat_calls) == 1
