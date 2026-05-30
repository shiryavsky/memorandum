"""Tests for connectors/mattermost_connector.py using the responses library."""
from unittest.mock import MagicMock
import pytest
import responses as rsps

from connectors.mattermost_connector import MattermostConnector

BASE = "https://mm.example.com"
API = f"{BASE}/api/v4"


def _make_connector(source_name="test_mm", **kwargs):
    return MattermostConnector(
        base_url=BASE,
        token="tok123",
        source_name=source_name,
        **kwargs,
    )


def _register_me(username="testuser"):
    rsps.add(rsps.GET, f"{API}/users/me", json={"id": "u1", "username": username}, status=200)


def _register_teams():
    rsps.add(rsps.GET, f"{API}/users/me/teams", json=[{"id": "t1", "name": "myteam"}])


def _register_channels(channels=None):
    if channels is None:
        channels = [{"id": "ch1", "name": "general", "display_name": "General", "type": "O"}]
    rsps.add(rsps.GET, f"{API}/users/me/teams/t1/channels", json=channels)


def _register_posts(channel_id="ch1", posts=None):
    if posts is None:
        posts = {
            "post1": {
                "id": "post1", "user_id": "u1", "message": "hello",
                "create_at": 1705312200000, "update_at": 1705312200001,
            }
        }
    rsps.add(rsps.GET, f"{API}/channels/{channel_id}/posts", json={"posts": posts})


def _register_user(user_id="u1"):
    rsps.add(rsps.GET, f"{API}/users/{user_id}", json={"id": user_id, "username": "testuser"})


# ── connect() ─────────────────────────────────────────────────────────────────

@rsps.activate
def test_connect_sets_connected_true_on_200():
    _register_me()
    conn = _make_connector()
    conn.connect()
    assert conn._connected is True


@rsps.activate
def test_connect_raises_on_4xx():
    rsps.add(rsps.GET, f"{API}/users/me", status=401)
    conn = _make_connector()
    with pytest.raises(ConnectionError):
        conn.connect()


# ── fetch_messages: no saved state ───────────────────────────────────────────

@rsps.activate
def test_fetch_messages_no_saved_state_uses_default_since_ms():
    _register_me()
    _register_teams()
    _register_channels()
    _register_posts()
    _register_user()

    conn = _make_connector()
    conn.connect()
    result = conn.fetch_messages(default_since_ms=0)
    assert result["messages_count"] >= 1


# ── fetch_messages: with saved state ─────────────────────────────────────────

@rsps.activate
def test_fetch_messages_with_saved_state_calls_db_callback():
    _register_me()
    _register_teams()
    _register_channels()
    _register_posts()
    _register_user()

    saved_ts = 1705312100000
    db_callback = MagicMock(return_value=saved_ts)

    conn = _make_connector(db_callback=db_callback)
    conn.connect()
    conn.fetch_messages(default_since_ms=0)

    db_callback.assert_called()


# ── message dict fields ───────────────────────────────────────────────────────

@rsps.activate
def test_message_id_format_includes_source_name():
    _register_me()
    _register_teams()
    _register_channels()
    _register_posts()
    _register_user()

    conn = _make_connector(source_name="my_mm")
    conn.connect()
    result = conn.fetch_messages(default_since_ms=0)

    msg = result["messages"][0]
    assert msg["id"] == "my_mm:post1"


@rsps.activate
def test_message_source_is_source_name_not_hardcoded():
    _register_me()
    _register_teams()
    _register_channels()
    _register_posts()
    _register_user()

    conn = _make_connector(source_name="custom_src")
    conn.connect()
    result = conn.fetch_messages(default_since_ms=0)

    assert result["messages"][0]["source"] == "custom_src"


# ── channel filters ───────────────────────────────────────────────────────────

@rsps.activate
def test_skip_channels_skips_matching_before_fetching_posts():
    _register_me()
    _register_teams()
    _register_channels(channels=[
        {"id": "ch1", "name": "general", "display_name": "General", "type": "O"},
    ])
    # No posts endpoint registered — if it were called, responses would raise

    conn = _make_connector(skip_channels=["General"])
    conn.connect()
    result = conn.fetch_messages(default_since_ms=0)
    assert result["messages_count"] == 0


@rsps.activate
def test_only_channels_skips_non_matching():
    _register_me()
    _register_teams()
    _register_channels(channels=[
        {"id": "ch1", "name": "general", "display_name": "General", "type": "O"},
        {"id": "ch2", "name": "dev", "display_name": "Dev", "type": "O"},
    ])
    _register_posts(channel_id="ch2")
    _register_user()

    conn = _make_connector(only_channels=["Dev"])
    conn.connect()
    result = conn.fetch_messages(default_since_ms=0)

    assert all(m["channel_id"] == "ch2" for m in result["messages"])


# ── upsert_channel ────────────────────────────────────────────────────────────

@rsps.activate
def test_upsert_channel_called_for_each_channel_with_correct_source():
    _register_me()
    _register_teams()
    _register_channels()
    _register_posts()
    _register_user()

    mock_db = MagicMock()
    conn = _make_connector(db=mock_db)
    conn.connect()
    conn.fetch_messages(default_since_ms=0)

    calls = mock_db.upsert_channel.call_args_list
    assert len(calls) >= 1
    first_call_arg = calls[0][0][0]
    assert first_call_arg["source"] == "test_mm"


# ── fetch_new ─────────────────────────────────────────────────────────────────

def _posts_listing(order, posts):
    return {"order": order, "posts": posts}


@rsps.activate
def test_fetch_new_no_state_returns_newest_oldest_first():
    _register_me()
    _register_teams()
    _register_channels()
    _register_user()
    posts = {
        "p1": {"id": "p1", "user_id": "u1", "message": "first", "create_at": 1000, "update_at": 1000},
        "p2": {"id": "p2", "user_id": "u1", "message": "second", "create_at": 2000, "update_at": 2000},
        "p3": {"id": "p3", "user_id": "u1", "message": "third", "create_at": 3000, "update_at": 3000},
    }
    rsps.add(rsps.GET, f"{API}/channels/ch1/posts",
             json=_posts_listing(["p3", "p2", "p1"], posts))  # API order: newest-first

    conn = _make_connector()  # no db_callback → listing fallback
    conn.connect()
    msgs = conn.fetch_new("general", limit=20)
    assert [m["raw"]["post_id"] for m in msgs] == ["p1", "p2", "p3"]
    assert all(m["channel"] == "General" for m in msgs)  # channel name attached


@rsps.activate
def test_fetch_new_no_state_resolves_by_id_and_respects_limit():
    _register_me()
    _register_teams()
    _register_channels()
    _register_user()
    posts = {f"p{i}": {"id": f"p{i}", "user_id": "u1", "message": f"m{i}",
                       "create_at": i * 1000, "update_at": i * 1000} for i in range(1, 6)}
    order = [f"p{i}" for i in range(5, 0, -1)]
    rsps.add(rsps.GET, f"{API}/channels/ch1/posts", json=_posts_listing(order, posts))

    conn = _make_connector()
    conn.connect()
    msgs = conn.fetch_new("ch1", limit=3)  # resolve by channel id
    assert len(msgs) == 3


@rsps.activate
def test_fetch_new_since_saved_state_does_not_write():
    _register_me()
    _register_teams()
    _register_channels()
    _register_user()
    posts = {"p1": {"id": "p1", "user_id": "u1", "message": "new", "create_at": 2000, "update_at": 2000}}
    rsps.add(rsps.GET, f"{API}/channels/ch1/posts", json={"posts": posts})

    mock_db = MagicMock()
    conn = _make_connector(db=mock_db, db_callback=MagicMock(return_value=1000))
    conn.connect()
    msgs = conn.fetch_new("general", limit=50)
    assert len(msgs) >= 1
    mock_db.upsert_channel.assert_not_called()


@rsps.activate
def test_fetch_new_unknown_channel_raises_value_error():
    _register_me()
    _register_teams()
    _register_channels()

    conn = _make_connector()
    conn.connect()
    with pytest.raises(ValueError):
        conn.fetch_new("nonexistent")


# ── get_sender_info ───────────────────────────────────────────────────────────

@rsps.activate
def test_get_sender_info_returns_source_name():
    _register_me()
    _register_user(user_id="u42")

    conn = _make_connector(source_name="my_mm")
    conn.connect()
    info = conn.get_sender_info("u42")

    assert info["source"] == "my_mm"
    assert info["sender_id"] == "u42"


# ── send_message ──────────────────────────────────────────────────────────────

@rsps.activate
def test_send_message_posts_and_returns_id():
    _register_me()
    rsps.add(rsps.POST, f"{API}/posts", json={"id": "newpost1"}, status=201)

    conn = _make_connector()
    conn.connect()
    post_id = conn.send_message("ch1", "hello world")

    assert post_id == "newpost1"
    body = rsps.calls[-1].request.body
    assert b"hello world" in body
    assert b"ch1" in body


@rsps.activate
def test_send_message_includes_root_id_for_reply():
    _register_me()
    rsps.add(rsps.POST, f"{API}/posts", json={"id": "reply1"}, status=201)

    conn = _make_connector()
    conn.connect()
    conn.send_message("ch1", "in thread", root_id="root99")

    assert b"root99" in rsps.calls[-1].request.body


@rsps.activate
def test_send_message_raises_clear_error_on_429():
    _register_me()
    rsps.add(rsps.POST, f"{API}/posts", status=429, json={"message": "rate limited"})

    conn = _make_connector()
    conn.connect()
    with pytest.raises(RuntimeError, match="rate limit"):
        conn.send_message("ch1", "too fast")


# ── channel description (purpose + header) ────────────────────────────────────

@rsps.activate
def test_channel_description_combines_purpose_and_header():
    _register_me()
    _register_teams()
    _register_channels(channels=[{
        "id": "ch1", "name": "dev", "display_name": "Dev", "type": "O",
        "purpose": "team announcements", "header": "ship date 2026-06-01",
    }])
    _register_posts()
    _register_user()

    mock_db = MagicMock()
    conn = _make_connector(db=mock_db)
    conn.connect()
    conn.fetch_messages(default_since_ms=0)

    upserted = mock_db.upsert_channel.call_args[0][0]
    assert upserted["description"] == "team announcements — ship date 2026-06-01"


@rsps.activate
def test_channel_description_none_when_purpose_and_header_blank():
    _register_me()
    _register_teams()
    _register_channels(channels=[{
        "id": "ch1", "name": "dev", "display_name": "Dev", "type": "O",
        "purpose": "", "header": "",
    }])
    _register_posts()
    _register_user()

    mock_db = MagicMock()
    conn = _make_connector(db=mock_db)
    conn.connect()
    conn.fetch_messages(default_since_ms=0)

    upserted = mock_db.upsert_channel.call_args[0][0]
    assert upserted["description"] is None


# ── youtrack url + issue-id extraction ────────────────────────────────────────

_YT = {"base_url": "https://youtrack.jetbrains.com", "project_prefixes": ["PL"]}


@rsps.activate
def test_message_raw_includes_extracted_urls():
    _register_me()
    _register_teams()
    _register_channels()
    _register_posts(posts={
        "post1": {"id": "post1", "user_id": "u1",
                  "message": "fix in https://youtrack.jetbrains.com/issue/PL-21765",
                  "create_at": 1705312200000, "update_at": 1705312200001}})
    _register_user()

    conn = _make_connector(youtrack_cfg=_YT)
    conn.connect()
    msgs = conn.fetch_messages(default_since_ms=0)["messages"]
    urls = msgs[0]["raw"]["urls"]
    assert urls == [{"type": "youtrack", "issue_id": "PL-21765",
                     "url": "https://youtrack.jetbrains.com/issue/PL-21765"}]


@rsps.activate
def test_channel_extra_carries_issue_ids_from_name():
    _register_me()
    _register_teams()
    _register_channels(channels=[{
        "id": "ch1", "name": "dev-pl-15491", "display_name": "Dev / PL-15491 mDK",
        "type": "O",
    }])
    _register_posts()
    _register_user()

    mock_db = MagicMock()
    conn = _make_connector(db=mock_db, youtrack_cfg=_YT)
    conn.connect()
    conn.fetch_messages(default_since_ms=0)

    upserted = mock_db.upsert_channel.call_args[0][0]
    assert upserted["extra"] == {"issue_ids": ["PL-15491"]}
