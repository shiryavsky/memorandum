"""Tests for connectors/pachca_connector.py using the responses library."""
from unittest.mock import MagicMock
import pytest
import responses as rsps

from connectors.pachca_connector import PachcaConnector

BASE = "https://api.pachca.com/api/shared/v1"


def _make_connector(source_name="test_pa", **kwargs):
    return PachcaConnector(access_token="tok123", source_name=source_name, **kwargs)


def _me_resp(first_name="Alice", last_name="Smith"):
    return {"data": {"id": 1, "first_name": first_name, "last_name": last_name,
                     "nickname": "alice", "email": "alice@example.com"}}


def _chats_resp(chats, has_next=False, next_page=None):
    return {
        "data": chats,
        "meta": {"paginate": {"has_next": has_next, "next_page": next_page}},
    }


def _msgs_resp(msgs, has_next=False, next_page=None):
    return {
        "data": msgs,
        "meta": {"paginate": {"has_next": has_next, "next_page": next_page}},
    }


def _chat(id=10, name="General", channel=True, personal=False):
    return {"id": id, "name": name, "channel": channel, "personal": personal}


def _msg(id=100, content="hello", user_id=1, created_at="2024-01-15T10:00:00Z",
         chat_id=10, entity_type="discussion", thread=None, parent_message_id=None,
         display_name=None, files=None):
    return {
        "id": id, "content": content, "user_id": user_id,
        "created_at": created_at, "chat_id": chat_id,
        "entity_type": entity_type, "entity_id": chat_id,
        "thread": thread, "parent_message_id": parent_message_id,
        "display_name": display_name, "files": files or [],
    }


def _user_resp(user_id=1, first_name="Alice", last_name="Smith"):
    return {"data": {"id": user_id, "first_name": first_name, "last_name": last_name,
                     "nickname": "alice", "email": "alice@example.com"}}


# ── connect() ─────────────────────────────────────────────────────────────────

@rsps.activate
def test_connect_success():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp(), status=200)
    conn = _make_connector()
    conn.connect()  # should not raise


@rsps.activate
def test_connect_raises_on_4xx():
    rsps.add(rsps.GET, f"{BASE}/profile", status=401)
    conn = _make_connector()
    with pytest.raises(Exception):
        conn.connect()


# ── fetch_messages: chat pagination ──────────────────────────────────────────

@rsps.activate
def test_fetch_messages_paginates_chats():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    # Page 1: has_next=True
    rsps.add(rsps.GET, f"{BASE}/chats",
             json=_chats_resp([_chat(10, "Dev")], has_next=True, next_page="cur2"))
    # Page 2: last page
    rsps.add(rsps.GET, f"{BASE}/chats",
             json=_chats_resp([_chat(11, "General")], has_next=False))
    # Messages for each chat
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=100, chat_id=10)]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=200, chat_id=11)]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages_count"] == 2
    assert result["channels_scanned"] == 2


# ── incremental sync ──────────────────────────────────────────────────────────

@rsps.activate
def test_fetch_messages_incremental_stops_at_last_seen_id():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10)]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=105), _msg(id=102), _msg(id=99)]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    db_callback = MagicMock(return_value=100)  # last seen = 100
    conn = _make_connector(db_callback=db_callback)
    conn.connect()
    result = conn.fetch_messages()

    ids = [m["id"] for m in result["messages"]]
    assert "test_pa:105" in ids
    assert "test_pa:102" in ids
    assert "test_pa:99" not in ids  # 99 <= 100, stopped here


@rsps.activate
def test_fetch_messages_first_run_respects_default_limit():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10)]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=i) for i in range(100, 95, -1)]))  # 5 messages
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    conn = _make_connector(default_limit=2)
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages_count"] == 2


@rsps.activate
def test_fetch_messages_force_ignores_saved_state_and_limit():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10)]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=i) for i in range(100, 95, -1)]))  # 5 messages
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    db_callback = MagicMock(return_value=99)  # would stop early without force
    conn = _make_connector(db_callback=db_callback, default_limit=2)
    conn.connect()
    result = conn.fetch_messages(force=True)

    # force=True → all 5 messages fetched (no limit, no ID stop)
    assert result["messages_count"] == 5


# ── channel filters ───────────────────────────────────────────────────────────

@rsps.activate
def test_skip_channels_filters_by_chat_name():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats",
             json=_chats_resp([_chat(10, "General"), _chat(11, "Dev")]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=200, chat_id=11)]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    conn = _make_connector(skip_channels=["General"])
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages_count"] == 1
    assert result["messages"][0]["channel_id"] == "11"


@rsps.activate
def test_only_channels_passes_matching_chats():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats",
             json=_chats_resp([_chat(10, "General"), _chat(11, "Dev")]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=200, chat_id=11)]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    conn = _make_connector(only_channels=["Dev"])
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages_count"] == 1
    assert result["messages"][0]["channel_id"] == "11"


# ── message dict format ───────────────────────────────────────────────────────

@rsps.activate
def test_message_id_format():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10)]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=42, chat_id=10)]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    conn = _make_connector(source_name="my_pa")
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages"][0]["id"] == "my_pa:42"


@rsps.activate
def test_message_source_is_source_name():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10)]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=42)]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    conn = _make_connector(source_name="custom_pa")
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages"][0]["source"] == "custom_pa"


@rsps.activate
def test_message_thread_id_set_from_thread_object():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10)]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=42, thread={"id": 77, "chat_id": 10})]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages"][0]["thread_id"] == "77"


@rsps.activate
def test_message_reply_to_id_set_from_parent_message_id():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10)]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=42, parent_message_id=41)]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages"][0]["reply_to_id"] == "41"


# ── upsert_channel ────────────────────────────────────────────────────────────

@rsps.activate
def test_upsert_channel_called_with_newest_message_id():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10, "General")]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=999), _msg(id=500)]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    mock_db = MagicMock()
    conn = _make_connector(db=mock_db)
    conn.connect()
    conn.fetch_messages()

    call_arg = mock_db.upsert_channel.call_args[0][0]
    assert call_arg["last_update_at"] == 999  # newest ID
    assert call_arg["source"] == "test_pa"
    assert call_arg["id"] == "10"


# ── fetch_new ─────────────────────────────────────────────────────────────────

@rsps.activate
def test_fetch_new_no_state_returns_newest_oldest_first():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10, "General")]))
    rsps.add(rsps.GET, f"{BASE}/messages", json=_msgs_resp([
        _msg(id=300, chat_id=10, created_at="2024-01-15T10:02:00Z"),
        _msg(id=200, chat_id=10, created_at="2024-01-15T10:01:00Z"),
        _msg(id=100, chat_id=10, created_at="2024-01-15T10:00:00Z"),
    ]))  # API order: newest-first
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    conn = _make_connector()  # no db_callback → fallback to newest `limit`
    conn.connect()
    msgs = conn.fetch_new("General", limit=20)
    assert [m["id"] for m in msgs] == ["test_pa:100", "test_pa:200", "test_pa:300"]
    assert all(m["channel"] == "General" for m in msgs)  # chat name attached


@rsps.activate
def test_fetch_new_no_state_resolves_by_id_and_respects_limit():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10, "General")]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=i, chat_id=10) for i in range(105, 100, -1)]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    conn = _make_connector()
    conn.connect()
    msgs = conn.fetch_new("10", limit=2)  # resolve by chat id
    assert len(msgs) == 2


@rsps.activate
def test_fetch_new_since_saved_state_stops_at_last_seen_and_no_writes():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10, "General")]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=105, chat_id=10, created_at="2024-01-15T10:05:00Z"),
                              _msg(id=102, chat_id=10, created_at="2024-01-15T10:02:00Z"),
                              _msg(id=99, chat_id=10, created_at="2024-01-15T09:59:00Z")]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    mock_db = MagicMock()
    conn = _make_connector(db=mock_db, db_callback=MagicMock(return_value=100))
    conn.connect()
    msgs = conn.fetch_new("General", limit=50)
    ids = [m["id"] for m in msgs]
    assert ids == ["test_pa:102", "test_pa:105"]  # >100, oldest→newest; 99 excluded
    mock_db.upsert_channel.assert_not_called()


@rsps.activate
def test_fetch_new_unknown_chat_raises_value_error():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10, "General")]))

    conn = _make_connector()
    conn.connect()
    with pytest.raises(ValueError):
        conn.fetch_new("Nonexistent")


# ── unfetchable (system) user ─────────────────────────────────────────────────

@rsps.activate
def test_unfetchable_user_cached_and_fetched_once():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10)]))
    rsps.add(rsps.GET, f"{BASE}/messages", json=_msgs_resp([
        _msg(id=1, user_id=999, chat_id=10), _msg(id=2, user_id=999, chat_id=10),
    ]))
    rsps.add(rsps.GET, f"{BASE}/users/999", status=404)

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages()

    assert result["messages_count"] == 2  # 404 user does not abort parsing
    user_calls = [c for c in rsps.calls if "/users/999" in c.request.url]
    assert len(user_calls) == 1  # negative result cached, not re-fetched


# ── chat display name ─────────────────────────────────────────────────────────

def test_group_chat_uses_name():
    conn = _make_connector()
    assert conn._chat_display_name(
        {"id": 10, "name": "General", "channel": True, "personal": False}
    ) == "General"


@rsps.activate
def test_personal_chat_upsert_name_falls_back_to_id():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())  # me id=1
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([
        {"id": 37768303, "name": "", "personal": True, "channel": False, "member_ids": [1, 42]},
    ]))
    rsps.add(rsps.GET, f"{BASE}/messages", json=_msgs_resp([_msg(id=5, chat_id=37768303)]))
    rsps.add(rsps.GET, f"{BASE}/users/42",
             json=_user_resp(user_id=42, first_name="Alt", last_name="Craft"))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())

    mock_db = MagicMock()
    conn = _make_connector(db=mock_db)
    conn.connect()
    conn.fetch_messages()

    call_arg = mock_db.upsert_channel.call_args[0][0]
    assert call_arg["name"] == "37768303"          # fallback to id, never empty
    assert call_arg["display_name"] == "Alt Craft"  # partner name


@rsps.activate
def test_personal_chat_uses_partner_name():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())  # me id=1
    rsps.add(rsps.GET, f"{BASE}/users/42",
             json=_user_resp(user_id=42, first_name="Alt", last_name="Craft"))

    conn = _make_connector()
    conn.connect()
    chat = {"id": 37768303, "name": "", "personal": True, "channel": False,
            "member_ids": [1, 42]}
    assert conn._chat_display_name(chat) == "Alt Craft"


# ── attachments ───────────────────────────────────────────────────────────────

_FILE_URL = "https://uploads.example/attaches/congrat.png?sig=abc"
_FILE = {"id": 3560, "name": "congrat.png", "file_type": "image", "url": _FILE_URL}


@rsps.activate
def test_message_with_attachment_embeds_file_id_not_url(tmp_path):
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10)]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=50, content="see this", files=[_FILE])]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())
    rsps.add(rsps.GET, _FILE_URL, body=b"\xff\xd8\xff")  # image bytes

    conn = _make_connector(attachments_path=str(tmp_path))
    conn.connect()
    text = conn.fetch_messages()["messages"][0]["text"]
    assert "see this" in text
    assert "file_id=3560" in text
    assert "congrat.png" in text
    assert "uploads.example" not in text  # no long signed URL inline


@rsps.activate
def test_attachment_downloaded_to_cache_by_file_id(tmp_path):
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10)]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=50, content="x", files=[_FILE])]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())
    rsps.add(rsps.GET, _FILE_URL, body=b"\xff\xd8\xff")

    conn = _make_connector(attachments_path=str(tmp_path))
    conn.connect()
    conn.fetch_messages()
    assert (tmp_path / "3560.png").exists()


@rsps.activate
def test_attachment_only_message_is_not_empty(tmp_path):
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10)]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=51, content="", files=[_FILE])]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())
    rsps.add(rsps.GET, _FILE_URL, body=b"\xff\xd8\xff")

    conn = _make_connector(attachments_path=str(tmp_path))
    conn.connect()
    msg = conn.fetch_messages()["messages"][0]
    assert msg["text"].strip() != ""
    assert "file_id=3560" in msg["text"]
    assert msg["raw"]["files"][0]["id"] == 3560


@rsps.activate
def test_text_attachment_inlined(tmp_path):
    txt_file = {"id": 77, "name": "notes.txt", "file_type": "file",
                "url": "https://uploads.example/notes.txt?sig=z"}
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/chats", json=_chats_resp([_chat(10)]))
    rsps.add(rsps.GET, f"{BASE}/messages",
             json=_msgs_resp([_msg(id=52, content="doc", files=[txt_file])]))
    rsps.add(rsps.GET, f"{BASE}/users/1", json=_user_resp())
    rsps.add(rsps.GET, "https://uploads.example/notes.txt?sig=z", body=b"hello from file")

    conn = _make_connector(attachments_path=str(tmp_path), text_extensions={".txt"})
    conn.connect()
    text = conn.fetch_messages()["messages"][0]["text"]
    assert "hello from file" in text


# ── get_sender_info ───────────────────────────────────────────────────────────

@rsps.activate
def test_get_sender_info_returns_source_name():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.GET, f"{BASE}/users/42",
             json=_user_resp(user_id=42, first_name="Bob", last_name="Jones"))

    conn = _make_connector(source_name="my_pa")
    conn.connect()
    info = conn.get_sender_info("42")

    assert info["source"] == "my_pa"
    assert info["sender_id"] == "42"
    assert info["full_name"] == "Bob Jones"


# ── send_message ──────────────────────────────────────────────────────────────

@rsps.activate
def test_send_message_posts_and_returns_id():
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.POST, f"{BASE}/messages", json={"data": {"id": 9001}}, status=201)

    conn = _make_connector()
    conn.connect()
    msg_id = conn.send_message(10, "hello there")

    assert msg_id == 9001
    body = rsps.calls[-1].request.body
    assert b"hello there" in body
    assert b"discussion" in body


@rsps.activate
def test_send_message_includes_parent_message_id():
    """`reply_to` is the public kwarg; on the wire it lands as `parent_message_id`."""
    rsps.add(rsps.GET, f"{BASE}/profile", json=_me_resp())
    rsps.add(rsps.POST, f"{BASE}/messages", json={"data": {"id": 9002}}, status=201)

    conn = _make_connector()
    conn.connect()
    # Accepts an int or a coerce-able string; the connector emits int.
    conn.send_message(10, "in thread", reply_to="8000")

    body = rsps.calls[-1].request.body
    assert b"parent_message_id" in body
    assert b"8000" in body


def test_message_url_builds_pachca_permalink():
    conn = _make_connector()
    assert conn.message_url(55, 9001) == "https://app.pachca.com/chats/55?message=9001"


def test_message_url_missing_message_id_returns_none():
    conn = _make_connector()
    assert conn.message_url(55, None) is None
    assert conn.message_url(55, "") is None
