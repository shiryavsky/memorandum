"""Tests for storage/db.py — Database operations on an in-memory SQLite DB."""
import pytest
from storage.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()


def _msg(id="src:001", source="src", channel_id="ch1", **kwargs):
    base = {
        "id": id,
        "source": source,
        "channel_id": channel_id,
        "sender": "alice",
        "sender_id": "u1",
        "timestamp": "2024-01-15T10:00:00+00:00",
        "text": "hello",
        "thread_id": None,
        "reply_to_id": None,
        "tags": [],
        "raw": {},
    }
    base.update(kwargs)
    return base


def _ch(id="ch1", source="src", **kwargs):
    base = {"id": id, "source": source, "name": "general", "display_name": "General", "last_update_at": 0}
    base.update(kwargs)
    return base


# ── insert / exists ────────────────────────────────────────────────────────────

def test_insert_stores_message_and_exists_returns_true(db):
    assert db.insert(_msg()) is True
    assert db.exists("src:001") is True


def test_insert_duplicate_returns_false(db):
    db.insert(_msg())
    assert db.insert(_msg()) is False


# ── search ─────────────────────────────────────────────────────────────────────

def test_search_filters_by_source(db):
    db.insert(_msg(id="a:1", source="a"))
    db.insert(_msg(id="b:1", source="b"))
    results = db.search(source="a")
    assert len(results) == 1
    assert results[0]["source"] == "a"


def test_search_filters_by_channel_display_name(db):
    db.upsert_channel(_ch(id="ch1", display_name="Dev"))
    db.insert(_msg(channel_id="ch1"))
    results = db.search(channel="Dev")
    assert len(results) == 1
    assert results[0]["channel"] == "Dev"


def test_search_returns_channel_name_from_channels_table(db):
    db.upsert_channel(_ch(id="ch1", name="dev-ch", display_name="Dev Channel"))
    db.insert(_msg(channel_id="ch1"))
    results = db.search()
    assert results[0]["channel_name"] == "dev-ch"


def test_search_filters_by_sender(db):
    db.insert(_msg(id="1", sender="alice"))
    db.insert(_msg(id="2", sender="bob"))
    results = db.search(sender="alice")
    assert len(results) == 1
    assert results[0]["sender"] == "alice"


def test_search_filters_by_since(db):
    db.insert(_msg(id="old", timestamp="2023-01-01T00:00:00+00:00"))
    db.insert(_msg(id="new", timestamp="2024-06-01T00:00:00+00:00"))
    results = db.search(since="2024-01-01")
    assert len(results) == 1
    assert results[0]["id"] == "new"


# ── upsert_channel / get_channel ───────────────────────────────────────────────

def test_upsert_channel_inserts_and_get_returns_last_update_at(db):
    db.upsert_channel(_ch(last_update_at=100))
    assert db.get_channel("src", "ch1") == 100


def test_upsert_channel_second_call_updates_last_update_at(db):
    db.upsert_channel(_ch(last_update_at=100))
    db.upsert_channel(_ch(last_update_at=200))
    assert db.get_channel("src", "ch1") == 200


def test_get_channel_returns_none_for_unknown(db):
    assert db.get_channel("src", "nonexistent") is None


# ── list_channels ──────────────────────────────────────────────────────────────

def test_list_channels_returns_id_and_name(db):
    db.upsert_channel(_ch(id="ch1", name="dev", display_name="Dev"))
    rows = db.list_channels()
    assert len(rows) == 1
    assert rows[0]["id"] == "ch1"
    assert rows[0]["display_name"] == "Dev"


def test_list_channels_excludes_offset_sentinel(db):
    db.upsert_channel(_ch(id="ch1", name="dev"))
    db.upsert_channel(_ch(id="__offset__", name="__offset__", source="src"))
    ids = [r["id"] for r in db.list_channels()]
    assert "ch1" in ids
    assert "__offset__" not in ids


def test_list_channels_filters_by_source(db):
    db.upsert_channel(_ch(id="ch1", source="a"))
    db.upsert_channel(_ch(id="ch2", source="b"))
    rows = db.list_channels(source="a")
    assert [r["id"] for r in rows] == ["ch1"]


# ── get_by_ids ─────────────────────────────────────────────────────────────────

def test_get_by_ids_returns_known_rows_skips_unknown(db):
    db.insert(_msg(id="a:1"))
    db.insert(_msg(id="a:2"))
    results = db.get_by_ids(["a:1", "a:99"])
    assert len(results) == 1
    assert results[0]["id"] == "a:1"


# ── get_thread ─────────────────────────────────────────────────────────────────

def test_get_thread_returns_root_and_replies_ordered(db):
    db.insert(_msg(id="src:root", timestamp="2024-01-15T10:00:00+00:00"))
    db.insert(_msg(id="src:r2", thread_id="root", reply_to_id="root",
                   timestamp="2024-01-15T10:02:00+00:00"))
    db.insert(_msg(id="src:r1", thread_id="root", reply_to_id="root",
                   timestamp="2024-01-15T10:01:00+00:00"))
    results = db.get_thread("root")
    assert [r["id"] for r in results] == ["src:root", "src:r1", "src:r2"]


def test_get_thread_excludes_other_threads(db):
    db.insert(_msg(id="src:root", timestamp="2024-01-15T10:00:00+00:00"))
    db.insert(_msg(id="src:reply", thread_id="root",
                   timestamp="2024-01-15T10:01:00+00:00"))
    db.insert(_msg(id="src:other", thread_id="elsewhere",
                   timestamp="2024-01-15T10:03:00+00:00"))
    results = db.get_thread("root")
    assert {r["id"] for r in results} == {"src:root", "src:reply"}


def test_get_thread_empty_for_unknown(db):
    db.insert(_msg(id="src:root"))
    assert db.get_thread("nonexistent") == []


def test_get_thread_channel_filter_narrows(db):
    db.upsert_channel(_ch(id="ch1", name="dev", display_name="Dev"))
    db.insert(_msg(id="src:root", channel_id="ch1"))
    db.insert(_msg(id="src:reply", channel_id="ch1", thread_id="root"))
    assert len(db.get_thread("root", channel="Dev")) == 2
    assert db.get_thread("root", channel="OtherChannel") == []


def test_get_thread_respects_limit(db):
    db.insert(_msg(id="src:root", timestamp="2024-01-15T10:00:00+00:00"))
    for i in range(5):
        db.insert(_msg(id=f"src:r{i}", thread_id="root",
                       timestamp=f"2024-01-15T10:0{i+1}:00+00:00"))
    assert len(db.get_thread("root", limit=3)) == 3


# ── count ─────────────────────────────────────────────────────────────────────

def test_count_returns_total(db):
    db.insert(_msg(id="1", source="a"))
    db.insert(_msg(id="2", source="b"))
    assert db.count() == 2


def test_count_filters_by_source(db):
    db.insert(_msg(id="1", source="a"))
    db.insert(_msg(id="2", source="b"))
    assert db.count(source="a") == 1


# ── upsert_sender ─────────────────────────────────────────────────────────────

def test_upsert_sender_inserts_and_updates(db):
    info = {
        "sender_id": "u1", "source": "src", "username": "alice",
        "full_name": "Alice", "email": None, "phone": None,
        "avatar_url": None, "extra": {},
    }
    db.upsert_sender(info)
    assert db.get_sender("src", "u1")["username"] == "alice"

    info["username"] = "alice_updated"
    db.upsert_sender(info)
    assert db.get_sender("src", "u1")["username"] == "alice_updated"


# ── canonical_sender / mentions_me ────────────────────────────────────────────

def test_insert_stores_canonical_sender(db):
    msg = _msg(id="1")
    msg["canonical_sender"] = "Jane Smith"
    db.insert(msg)
    results = db.search()
    assert results[0]["canonical_sender"] == "Jane Smith"


def test_insert_stores_mentions_me(db):
    msg = _msg(id="1")
    msg["mentions_me"] = 1
    db.insert(msg)
    results = db.search()
    assert results[0]["mentions_me"] == 1


def test_insert_stores_internal_flag(db):
    msg = _msg(id="1")
    msg["internal"] = 1
    db.insert(msg)
    assert db.search()[0]["internal"] == 1


def test_internal_defaults_to_zero(db):
    db.insert(_msg(id="1"))  # _msg has no 'internal' key
    assert db.search()[0]["internal"] == 0


def test_search_matches_canonical_sender(db):
    msg = _msg(id="1", sender="jsmith")
    msg["canonical_sender"] = "Jane Smith"
    db.insert(msg)
    results = db.search(sender="Jane Smith")
    assert len(results) == 1
    assert results[0]["canonical_sender"] == "Jane Smith"


def test_search_filters_mentions_me(db):
    msg_to_me = _msg(id="1")
    msg_to_me["mentions_me"] = 1
    msg_other = _msg(id="2")
    msg_other["mentions_me"] = 0
    db.insert(msg_to_me)
    db.insert(msg_other)
    results = db.search(mentions_me=True)
    assert len(results) == 1
    assert results[0]["id"] == "1"


# ── upsert_aliases / get_aliases ──────────────────────────────────────────────

def test_upsert_aliases_populates_table(db):
    aliases = [
        {"canonical_name": "Jane Smith", "aliases": ["jane", "jane.smith"]},
        {"canonical_name": "Bob Wilson", "aliases": ["bob"]},
    ]
    db.upsert_aliases(aliases)
    groups = db.get_aliases()
    canonicals = {g["canonical_name"] for g in groups}
    assert "Jane Smith" in canonicals
    assert "Bob Wilson" in canonicals


def test_upsert_aliases_replaces_on_second_call(db):
    db.upsert_aliases([{"canonical_name": "Old Name", "aliases": ["old"]}])
    db.upsert_aliases([{"canonical_name": "New Name", "aliases": ["new"]}])
    groups = db.get_aliases()
    canonicals = {g["canonical_name"] for g in groups}
    assert "New Name" in canonicals
    assert "Old Name" not in canonicals


def test_upsert_aliases_empty_clears_table(db):
    db.upsert_aliases([{"canonical_name": "Jane", "aliases": ["jane"]}])
    db.upsert_aliases([])
    assert db.get_aliases() == []


# ── sent_messages ─────────────────────────────────────────────────────────────

def test_record_sent_message_stores_success(db):
    row_id = db.record_sent_message({
        "source": "mm", "channel": "ch1", "reply_to": None,
        "text": "hello", "success": True, "message_id": "post99",
    })
    assert row_id > 0
    row = db.conn.execute("SELECT * FROM sent_messages WHERE id=?", (row_id,)).fetchone()
    assert row["source"] == "mm"
    assert row["channel"] == "ch1"
    assert row["text"] == "hello"
    assert row["success"] == 1
    assert row["message_id"] == "post99"
    assert row["sent_at"]  # auto-filled


def test_record_sent_message_stores_failure(db):
    db.record_sent_message({
        "source": "tg", "channel": "-100123", "reply_to": "5",
        "text": "oops", "success": False, "error": "rate limit hit",
    })
    row = db.conn.execute("SELECT * FROM sent_messages ORDER BY id DESC LIMIT 1").fetchone()
    assert row["success"] == 0
    assert row["message_id"] is None
    assert row["error"] == "rate limit hit"
    assert row["reply_to"] == "5"


# ── get_channel_row ───────────────────────────────────────────────────────────

def test_get_channel_row_returns_parsed_extra(db):
    db.upsert_channel({
        "id": "9153987", "source": "tg", "name": "9153987",
        "display_name": "Alice", "channel_type": "private",
        "extra": {"business_connection_id": "bconn-xyz"},
        "last_update_at": 123,
    })
    row = db.get_channel_row("tg", "9153987")
    assert row["display_name"] == "Alice"
    assert row["extra"] == {"business_connection_id": "bconn-xyz"}


def test_get_channel_row_returns_none_for_unknown(db):
    assert db.get_channel_row("tg", "nope") is None


# ── description ───────────────────────────────────────────────────────────────

def test_upsert_channel_persists_description(db):
    db.upsert_channel({
        "id": "ch1", "source": "mm", "name": "dev", "display_name": "Dev",
        "description": "team announcements — pinned: ship date 2026-06-01",
        "last_update_at": 1,
    })
    row = db.get_channel_row("mm", "ch1")
    assert row["description"].startswith("team announcements")


def test_upsert_channel_description_preserved_when_omitted(db):
    db.upsert_channel({"id": "ch1", "source": "mm", "name": "dev",
                       "description": "first", "last_update_at": 1})
    db.upsert_channel({"id": "ch1", "source": "mm", "name": "dev",
                       "last_update_at": 2})  # no description this time
    row = db.get_channel_row("mm", "ch1")
    assert row["description"] == "first"


def test_list_channels_includes_description(db):
    db.upsert_channel({"id": "ch1", "source": "mm", "name": "dev",
                       "display_name": "Dev", "description": "team",
                       "last_update_at": 1})
    rows = db.list_channels()
    assert rows[0]["description"] == "team"


# ── find_by_issue_id ──────────────────────────────────────────────────────────

def test_find_by_issue_id_matches_message_raw_urls(db):
    db.insert(_msg(id="src:1", text="see link",
                   raw={"urls": [{"type": "youtrack", "issue_id": "PL-1",
                                  "url": "https://track/PL-1"}]}))
    db.insert(_msg(id="src:2", text="other"))
    hits = db.find_by_issue_id("PL-1")
    assert [h["id"] for h in hits] == ["src:1"]


def test_find_by_issue_id_matches_channel_extra_issue_ids(db):
    db.upsert_channel({"id": "ch9", "source": "src", "name": "dev",
                       "display_name": "PL-15491 mDK",
                       "extra": {"issue_ids": ["PL-15491"]},
                       "last_update_at": 1})
    db.insert(_msg(id="src:9", channel_id="ch9", text="hi"))
    hits = db.find_by_issue_id("PL-15491")
    assert [h["id"] for h in hits] == ["src:9"]


def test_find_by_issue_id_no_match_returns_empty(db):
    db.insert(_msg(id="src:1", text="just text"))
    assert db.find_by_issue_id("PL-999") == []


# ── mentions ──────────────────────────────────────────────────────────────────

def _mention(message_id="src:1", source="src", **kwargs):
    base = {
        "message_id": message_id,
        "source": source,
        "sender_id": "u1",
        "sender_canonical": "Alice",
        "mentioned_token": "@bob",
        "mentioned_canonical": "Bob",
        "mentioned_sender_id": "u2",
    }
    base.update(kwargs)
    return base


def test_insert_mentions_round_trip(db):
    db.insert(_msg(id="src:1"))
    inserted = db.insert_mentions([_mention()])
    assert inserted == 1
    rows = db.get_mentions(mentioned_canonical="Bob")
    assert len(rows) == 1
    assert rows[0]["mentioned_token"] == "@bob"
    assert rows[0]["mentioned_sender_id"] == "u2"
    assert rows[0]["sender_canonical"] == "Alice"


def test_insert_mentions_bulk(db):
    db.insert(_msg(id="src:1"))
    inserted = db.insert_mentions([
        _mention(mentioned_token="@bob", mentioned_canonical="Bob"),
        _mention(mentioned_token="@carol", mentioned_canonical="Carol"),
    ])
    assert inserted == 2


def test_insert_mentions_empty_is_noop(db):
    assert db.insert_mentions([]) == 0


def test_get_mentions_filters_by_sender_canonical(db):
    db.insert(_msg(id="src:1", sender="alice"))
    db.insert(_msg(id="src:2", sender="dave"))
    db.insert_mentions([
        _mention(message_id="src:1", sender_canonical="Alice"),
        _mention(message_id="src:2", sender_canonical="Dave"),
    ])
    rows = db.get_mentions(sender_canonical="Dave")
    assert [r["id"] for r in rows] == ["src:2"]


def test_get_mentions_filters_by_source(db):
    db.insert(_msg(id="a:1", source="a"))
    db.insert(_msg(id="b:1", source="b"))
    db.insert_mentions([_mention(message_id="a:1", source="a"),
                        _mention(message_id="b:1", source="b")])
    rows = db.get_mentions(mentioned_canonical="Bob", source="a")
    assert [r["id"] for r in rows] == ["a:1"]


def test_get_mentions_filters_by_since_until(db):
    db.insert(_msg(id="src:1", timestamp="2024-01-10T10:00:00+00:00"))
    db.insert(_msg(id="src:2", timestamp="2024-01-20T10:00:00+00:00"))
    db.insert_mentions([_mention(message_id="src:1"), _mention(message_id="src:2")])
    rows = db.get_mentions(mentioned_canonical="Bob", since="2024-01-15T00:00:00+00:00")
    assert [r["id"] for r in rows] == ["src:2"]
    rows = db.get_mentions(mentioned_canonical="Bob", until="2024-01-15T00:00:00+00:00")
    assert [r["id"] for r in rows] == ["src:1"]


def test_get_mentions_joins_channel_display_name(db):
    db.upsert_channel(_ch(id="ch1", display_name="Dev"))
    db.insert(_msg(id="src:1", channel_id="ch1"))
    db.insert_mentions([_mention(message_id="src:1")])
    rows = db.get_mentions(mentioned_canonical="Bob")
    assert rows[0]["channel"] == "Dev"


def test_find_sender_id_by_username_case_insensitive(db):
    db.upsert_sender({"sender_id": "u42", "source": "mm",
                      "username": "Bob.Jones", "full_name": "Bob Jones"})
    assert db.find_sender_id_by_username("mm", "bob.jones") == "u42"
    assert db.find_sender_id_by_username("mm", "BOB.JONES") == "u42"


def test_find_sender_id_by_username_unknown_returns_none(db):
    assert db.find_sender_id_by_username("mm", "nobody") is None


# ── get_mentions_for_identity (OR across token / canonical / sender_id) ──────

def test_identity_match_by_token_alone(db):
    """Mention stored with canonical=NULL still matches via mentioned_token."""
    db.insert(_msg(id="src:1"))
    db.insert_mentions([_mention(
        message_id="src:1",
        mentioned_token="@john.doe",
        mentioned_canonical=None,
        mentioned_sender_id=None,
    )])
    rows = db.get_mentions_for_identity(tokens=["@john.doe"])
    assert len(rows) == 1


def test_identity_match_is_case_insensitive_on_token(db):
    db.insert(_msg(id="src:1"))
    db.insert_mentions([_mention(message_id="src:1", mentioned_token="@John.Doe",
                                 mentioned_canonical=None, mentioned_sender_id=None)])
    rows = db.get_mentions_for_identity(tokens=["@john.doe"])
    assert len(rows) == 1


def test_identity_match_by_sender_id_alone(db):
    db.insert(_msg(id="src:1"))
    db.insert_mentions([_mention(
        message_id="src:1",
        mentioned_token="@somebody",
        mentioned_canonical=None,
        mentioned_sender_id="user_42",
    )])
    rows = db.get_mentions_for_identity(sender_ids=["user_42"])
    assert len(rows) == 1


def test_identity_match_or_across_three_columns(db):
    """A query for canonicals OR tokens OR sender_ids should return any row
    matching ANY of the lists, not just rows where all three match."""
    db.insert(_msg(id="src:1"))
    db.insert(_msg(id="src:2"))
    db.insert(_msg(id="src:3"))
    db.insert_mentions([
        _mention(message_id="src:1", mentioned_canonical="Bob", mentioned_token="@bob", mentioned_sender_id=None),
        _mention(message_id="src:2", mentioned_canonical=None,  mentioned_token="@bob_alt", mentioned_sender_id=None),
        _mention(message_id="src:3", mentioned_canonical=None,  mentioned_token="@other", mentioned_sender_id="u42"),
    ])
    rows = db.get_mentions_for_identity(
        canonicals=["Bob"], tokens=["@bob_alt"], sender_ids=["u42"],
    )
    ids = sorted(r["id"] for r in rows)
    assert ids == ["src:1", "src:2", "src:3"]


def test_identity_empty_filters_return_empty(db):
    """No identity criteria → no rows (safer than 'return everything')."""
    db.insert(_msg(id="src:1"))
    db.insert_mentions([_mention(message_id="src:1")])
    assert db.get_mentions_for_identity() == []


# ── retention / prune (TASK-028) ─────────────────────────────────────────────

def test_referenced_file_ids_picks_up_all_four_marker_shapes(db):
    # Pachca/Email sha1-prefix (24 hex)
    db.insert(_msg(id="src:1", text="hi [attachment: file.pdf, file_id=abc123def456abc123def456]"))
    # Mattermost post-style id (long alphanumeric)
    db.insert(_msg(id="src:2", text="[file: notes.md, file_id=mmpostidalphanumericokayhere9999]"))
    # Telegram base64-ish id (uppercase + digits + underscores)
    db.insert(_msg(id="src:3", text="[photo, file_id=AgACAgIAAxkB_abcDEFghi-jklMNO]"))
    # Hyphenated id (shouldn't drop the hyphen segment)
    db.insert(_msg(id="src:4", text="[document: x.zip, file_id=some-hyphenated-id-here-42]"))
    refs = db.referenced_file_ids()
    assert "abc123def456abc123def456" in refs
    assert "mmpostidalphanumericokayhere9999" in refs
    assert "AgACAgIAAxkB_abcDEFghi-jklMNO" in refs
    assert "some-hyphenated-id-here-42" in refs


def test_referenced_file_ids_skips_messages_without_marker(db):
    db.insert(_msg(id="src:1", text="just text no attachment"))
    assert db.referenced_file_ids() == set()


def test_prune_deletes_messages_below_cutoff_only(db):
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00"))
    db.insert(_msg(id="src:new", timestamp="2026-01-01T00:00:00+00:00"))
    counts = db.prune("2025-01-01T00:00:00+00:00")
    assert counts["messages_deleted"] == 1
    assert counts["message_ids"] == ["src:old"]
    ids = [r["id"] for r in db.search()]
    assert "src:old" not in ids
    assert "src:new" in ids


def test_prune_deletes_mentions_of_old_messages(db):
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00"))
    db.insert(_msg(id="src:new", timestamp="2026-01-01T00:00:00+00:00"))
    db.insert_mentions([
        _mention(message_id="src:old", mentioned_token="@old"),
        _mention(message_id="src:new", mentioned_token="@new"),
    ])
    counts = db.prune("2025-01-01T00:00:00+00:00")
    assert counts["mentions_deleted"] == 1
    remaining = db.get_mentions()
    assert len(remaining) == 1
    assert remaining[0]["mentioned_token"] == "@new"


def test_prune_does_not_touch_channels_senders_aliases(db):
    db.upsert_channel(_ch(id="ch1", display_name="Dev"))
    db.upsert_sender({"sender_id": "u1", "source": "src", "username": "alice",
                      "full_name": "Alice"})
    db.upsert_aliases([{"canonical_name": "Alice", "aliases": ["alice"]}])
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00"))
    db.prune("2025-01-01T00:00:00+00:00")
    assert db.list_channels() != []                    # channels intact
    assert db.get_sender("src", "u1") is not None      # senders intact
    assert db.get_aliases() != []                      # sender_aliases intact


def test_prune_deletes_sent_messages_and_ingest_runs(db):
    db.record_sent_message({"source": "mm", "channel": "c", "text": "hi",
                            "success": True, "sent_at": "2024-01-01T00:00:00+00:00"})
    db.record_sent_message({"source": "mm", "channel": "c", "text": "hi",
                            "success": True, "sent_at": "2026-01-01T00:00:00+00:00"})
    db.record_ingest_run({"started_at": "2024-01-01T00:00:00+00:00",
                          "finished_at": "2024-01-01T00:01:00+00:00",
                          "status": "ok"})
    db.record_ingest_run({"started_at": "2026-01-01T00:00:00+00:00",
                          "finished_at": "2026-01-01T00:01:00+00:00",
                          "status": "ok"})
    counts = db.prune("2025-01-01T00:00:00+00:00")
    assert counts["sent_deleted"] == 1
    assert counts["runs_deleted"] == 1


def test_count_prune_candidates_mirrors_prune_without_writing(db):
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00"))
    db.insert(_msg(id="src:new", timestamp="2026-01-01T00:00:00+00:00"))
    db.insert_mentions([_mention(message_id="src:old", mentioned_token="@old")])
    plan = db.count_prune_candidates("2025-01-01T00:00:00+00:00")
    assert plan["messages_deleted"] == 1
    assert plan["mentions_deleted"] == 1
    assert plan["message_ids"] == ["src:old"]
    # Nothing was actually deleted.
    assert len(db.search()) == 2
    assert len(db.get_mentions()) == 1


def test_record_prune_run_and_last_prune_at(db):
    assert db.last_prune_at() is None
    db.record_prune_run({
        "started_at": "2026-05-30T00:00:00+00:00",
        "finished_at": "2026-05-30T00:00:30+00:00",
        "cutoff_ts": "2025-05-30T00:00:00+00:00",
        "messages_deleted": 5,
    })
    assert db.last_prune_at() == "2026-05-30T00:00:30+00:00"


def test_last_prune_at_ignores_failed_runs(db):
    """A row with `error` doesn't count as a successful throttle marker."""
    db.record_prune_run({"started_at": "2026-05-30T00:00:00+00:00",
                         "finished_at": "2026-05-30T00:00:30+00:00",
                         "error": "kaboom"})
    assert db.last_prune_at() is None


# ── tool_calls log (TASK-026) ────────────────────────────────────────────────

def test_log_tool_call_round_trip(db):
    db.log_tool_call(tool_name="search_messages",
                     args_summary='{"query": "deploy", "mode": "semantic"}',
                     duration_ms=42, success=True)
    rows = list(db.conn.execute("SELECT * FROM tool_calls").fetchall())
    assert len(rows) == 1
    r = dict(rows[0])
    assert r["tool_name"] == "search_messages"
    assert r["duration_ms"] == 42
    assert r["success"] == 1
    assert "deploy" in r["args_summary"]
    assert r["called_at"]  # populated


def test_log_tool_call_records_failure(db):
    db.log_tool_call(tool_name="send_message", success=False,
                     error="upstream 500", duration_ms=10)
    r = dict(db.conn.execute("SELECT * FROM tool_calls").fetchone())
    assert r["success"] == 0
    assert r["error"] == "upstream 500"


def test_log_tool_call_swallows_db_errors(db):
    """A broken connection must not propagate out of log_tool_call."""
    db.conn.close()  # subsequent execute raises ProgrammingError
    db.log_tool_call(tool_name="x")  # must not raise


def test_log_tool_call_no_op_on_read_only(tmp_path):
    # Seed a writable DB so we can reopen read-only.
    rw = Database(str(tmp_path / "db.sqlite"))
    rw.close()
    ro = Database(str(tmp_path / "db.sqlite"), read_only=True)
    ro.log_tool_call(tool_name="x")  # no-op, doesn't raise
    # Confirm nothing was written.
    n = ro.conn.execute("SELECT COUNT(*) c FROM tool_calls").fetchone()["c"]
    assert n == 0
    ro.close()


def test_log_tool_call_prunes_old_rows_after_threshold(db):
    """Once _TOOL_CALLS_PRUNE_EVERY writes have accumulated, the > 30-day rows go."""
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    # Plant an old row directly.
    db.conn.execute("""
        INSERT INTO tool_calls (called_at, tool_name, success)
        VALUES (?, 'ancient', 1)
    """, (old,))
    db.conn.commit()
    # Force the prune cadence: write _TOOL_CALLS_PRUNE_EVERY recent rows.
    for _ in range(Database._TOOL_CALLS_PRUNE_EVERY):
        db.log_tool_call(tool_name="recent", duration_ms=1, success=True)
    # The old row should be gone; the recent ones survive.
    ancient = db.conn.execute(
        "SELECT COUNT(*) c FROM tool_calls WHERE tool_name='ancient'"
    ).fetchone()["c"]
    assert ancient == 0
    recent = db.conn.execute(
        "SELECT COUNT(*) c FROM tool_calls WHERE tool_name='recent'"
    ).fetchone()["c"]
    assert recent == Database._TOOL_CALLS_PRUNE_EVERY


# ── read-only mode ────────────────────────────────────────────────────────────

def test_read_only_mode_refuses_writes(tmp_path):
    # Seed a writable DB so the file exists with our schema.
    rw = Database(str(tmp_path / "db.sqlite"))
    rw.insert(_msg(id="src:1"))
    rw.close()
    ro = Database(str(tmp_path / "db.sqlite"), read_only=True)
    import sqlite3 as _sqlite3
    with pytest.raises(_sqlite3.OperationalError, match="readonly"):
        ro.conn.execute("INSERT INTO messages (id, source) VALUES ('src:2', 'src')")
    # Reads still work.
    assert ro.search()[0]["id"] == "src:1"
    ro.close()


# ── dashboard read queries (TASK-026) ────────────────────────────────────────

def test_latest_messages_orders_newest_first(db):
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00"))
    db.insert(_msg(id="src:new", timestamp="2026-01-01T00:00:00+00:00"))
    rows = db.latest_messages(limit=10)
    assert [r["id"] for r in rows] == ["src:new", "src:old"]


def test_top_channels_since_orders_by_count_desc(db):
    db.upsert_channel(_ch(id="ch1", display_name="Dev"))
    db.upsert_channel(_ch(id="ch2", display_name="Random"))
    for i in range(5):
        db.insert(_msg(id=f"src:dev{i}", channel_id="ch1",
                       timestamp="2026-01-15T10:00:00+00:00"))
    for i in range(2):
        db.insert(_msg(id=f"src:rnd{i}", channel_id="ch2",
                       timestamp="2026-01-15T10:00:00+00:00"))
    rows = db.top_channels_since("2026-01-01T00:00:00+00:00")
    assert rows[0]["channel"] == "Dev"
    assert rows[0]["count"] == 5
    assert rows[1]["channel"] == "Random"


def test_top_senders_since_collapses_aliases_via_canonical(db):
    db.insert(_msg(id="src:1", sender="bob",     canonical_sender="Bob Wilson",
                   timestamp="2026-01-15T10:00:00+00:00"))
    db.insert(_msg(id="src:2", sender="bwilson", canonical_sender="Bob Wilson",
                   timestamp="2026-01-15T11:00:00+00:00"))
    rows = db.top_senders_since("2026-01-01T00:00:00+00:00")
    assert rows[0]["sender"] == "Bob Wilson"
    assert rows[0]["count"] == 2


def test_messages_per_day_groups_by_date(db):
    db.insert(_msg(id="src:1", timestamp="2026-01-15T10:00:00+00:00"))
    db.insert(_msg(id="src:2", timestamp="2026-01-15T11:30:00+00:00"))
    db.insert(_msg(id="src:3", timestamp="2026-01-16T08:00:00+00:00"))
    from datetime import datetime, timezone
    # Use a large window so all three land.
    now = datetime.now(timezone.utc)
    days = max(1, (now - datetime(2026, 1, 14, tzinfo=timezone.utc)).days + 5)
    rows = db.messages_per_day(days=days)
    counts = {r["date"]: r["count"] for r in rows}
    assert counts.get("2026-01-15") == 2
    assert counts.get("2026-01-16") == 1


def test_hour_of_day_histogram_returns_24_buckets(db):
    db.insert(_msg(id="src:1", timestamp="2026-01-15T10:00:00+00:00"))
    db.insert(_msg(id="src:2", timestamp="2026-01-15T14:30:00+00:00"))
    buckets = db.hour_of_day_histogram(days=10_000)  # huge window so they land
    assert len(buckets) == 24
    assert sum(buckets) >= 2


def test_send_stats_since_counts_success_and_failure(db):
    db.record_sent_message({"source": "mm", "channel": "c", "text": "ok",
                            "success": True, "sent_at": "2026-01-15T10:00:00+00:00"})
    db.record_sent_message({"source": "mm", "channel": "c", "text": "fail",
                            "success": False, "sent_at": "2026-01-15T11:00:00+00:00",
                            "error": "kaboom"})
    s = db.send_stats_since("2026-01-01T00:00:00+00:00")
    assert s["total"] == 2
    assert s["success"] == 1
    assert s["failure"] == 1
    assert s["per_source"][0]["source"] == "mm"


def test_tool_call_stats_since_aggregates_per_tool(db):
    db.log_tool_call(tool_name="search_messages", duration_ms=10, success=True)
    db.log_tool_call(tool_name="search_messages", duration_ms=30, success=True)
    db.log_tool_call(tool_name="search_messages", duration_ms=20, success=False, error="x")
    db.log_tool_call(tool_name="get_health", duration_ms=2, success=True)
    rows = db.tool_call_stats_since("2020-01-01T00:00:00+00:00")
    by_tool = {r["tool_name"]: r for r in rows}
    assert by_tool["search_messages"]["total"] == 3
    assert by_tool["search_messages"]["success"] == 2
    assert by_tool["search_messages"]["failure"] == 1
    assert by_tool["search_messages"]["avg_ms"] == 20.0
    assert by_tool["get_health"]["total"] == 1


def test_mentions_me_counts_window(db):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    db.insert(_msg(id="src:hour",
                   timestamp=(now - timedelta(minutes=30)).isoformat(),
                   mentions_me=1))
    db.insert(_msg(id="src:day",
                   timestamp=(now - timedelta(hours=12)).isoformat(),
                   mentions_me=1))
    db.insert(_msg(id="src:week",
                   timestamp=(now - timedelta(days=3)).isoformat(),
                   mentions_me=1))
    db.insert(_msg(id="src:old",
                   timestamp=(now - timedelta(days=14)).isoformat(),
                   mentions_me=1))
    c = db.mentions_me_counts()
    assert c["h1"] == 1
    assert c["d1"] == 2
    assert c["d7"] == 3


def test_identity_other_filters_are_ANDed(db):
    """sender_canonical / source / since / until still narrow the OR result."""
    db.insert(_msg(id="src:1", sender="alice", timestamp="2024-01-10T10:00:00+00:00"))
    db.insert(_msg(id="src:2", sender="dave",  timestamp="2024-01-20T10:00:00+00:00"))
    db.insert_mentions([
        _mention(message_id="src:1", sender_canonical="Alice", mentioned_token="@bob"),
        _mention(message_id="src:2", sender_canonical="Dave",  mentioned_token="@bob"),
    ])
    rows = db.get_mentions_for_identity(tokens=["@bob"], sender_canonical="Dave")
    assert [r["id"] for r in rows] == ["src:2"]
    rows = db.get_mentions_for_identity(tokens=["@bob"], since="2024-01-15T00:00:00+00:00")
    assert [r["id"] for r in rows] == ["src:2"]
