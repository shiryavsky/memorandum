"""Tests for pipeline/ingest.py — run_ingest orchestrator.

VectorStore is mocked globally in conftest.py.
Connector classes are mocked per-test to avoid network calls.
"""
from unittest.mock import MagicMock, patch
import yaml

from pipeline.ingest import run_ingest


def _write_config(tmp_path, sources=None, user_aliases=None, my_aliases=None,
                  internal_domains=None):
    cfg = {
        "sqlite_path": str(tmp_path / "db.sqlite"),
        "chroma_path": str(tmp_path / "chroma"),
        "sources": sources or {},
    }
    if user_aliases is not None:
        cfg["user_aliases"] = user_aliases
    if my_aliases is not None:
        cfg["my_aliases"] = my_aliases
    if internal_domains is not None:
        cfg["internal_domains"] = internal_domains
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(cfg))
    return str(p), cfg


def _sample_msg(id="mm_src:post1", source="mm_src", channel_id="ch1"):
    return {
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


def _connector_result(messages=None):
    msgs = messages or []
    return {
        "messages": msgs,
        "messages_count": len(msgs),
        "channels_scanned": 1,
        "channels_skipped": 0,
    }


# ── no enabled sources ────────────────────────────────────────────────────────

@patch("pipeline.ingest.VectorStore")
def test_run_ingest_no_sources_returns_zeroed_stats(MockVS, tmp_path):
    config_path, _ = _write_config(tmp_path, sources={})
    stats = run_ingest(config_path=config_path)

    assert stats["sources_checked"] == 0
    assert stats["messages_new"] == 0
    assert stats["messages_duplicate"] == 0
    assert stats["messages_fetched"] == 0
    MockVS.return_value.insert.assert_not_called()


# ── connector called, stats aggregated ───────────────────────────────────────

@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_calls_connector_and_aggregates_stats(MockMM, MockVS, tmp_path):
    config_path, _ = _write_config(tmp_path, sources={
        "mm_src": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}
    })
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([_sample_msg()])
    MockMM.return_value = connector

    stats = run_ingest(config_path=config_path)

    assert stats["sources_checked"] == 1
    assert stats["messages_fetched"] == 1
    assert stats["messages_new"] == 1
    assert stats["channels_scanned"] == 1


# ── filter engine applied ─────────────────────────────────────────────────────

@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_filter_engine_reflected_in_stats(MockMM, MockVS, tmp_path):
    config_path, _ = _write_config(tmp_path, sources={
        "mm_src": {
            "type": "mattermost",
            "url": "http://x",
            "token": "t",
            "enabled": True,
            "filters": {"skip_senders": ["bot"]},
        }
    })
    msgs = [
        _sample_msg(id="mm_src:1", source="mm_src"),
        {**_sample_msg(id="mm_src:2", source="mm_src"), "sender": "bot"},
    ]
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result(msgs)
    MockMM.return_value = connector

    stats = run_ingest(config_path=config_path)

    assert stats["messages_fetched"] == 2
    assert stats["messages_filtered"] == 1
    assert stats["messages_new"] == 1


# ── duplicate detection ───────────────────────────────────────────────────────

@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_duplicate_increments_duplicate_stat(MockMM, MockVS, tmp_path):
    config_path, _ = _write_config(tmp_path, sources={
        "mm_src": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}
    })
    msg = _sample_msg()
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg])
    MockMM.return_value = connector

    # First ingest — inserts the message
    run_ingest(config_path=config_path)
    # Second ingest — same message should be a duplicate
    stats = run_ingest(config_path=config_path)

    assert stats["messages_duplicate"] == 1
    assert stats["messages_new"] == 0


# ── VectorStore insert called for new messages only ──────────────────────────

@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_vector_store_insert_called_for_new_not_for_duplicates(MockMM, MockVS, tmp_path):
    config_path, _ = _write_config(tmp_path, sources={
        "mm_src": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}
    })
    msg = _sample_msg()
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg])
    MockMM.return_value = connector

    vs_instance = MockVS.return_value
    run_ingest(config_path=config_path)
    assert vs_instance.insert.call_count == 1

    # Second run — duplicate, no insert
    vs_instance.insert.reset_mock()
    run_ingest(config_path=config_path)
    vs_instance.insert.assert_not_called()


# ── Pachca connector instantiated for type: pachca ────────────────────────────

@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.PachcaConnector")
def test_run_ingest_instantiates_pachca_connector(MockPA, MockVS, tmp_path):
    config_path, _ = _write_config(tmp_path, sources={
        "pa_src": {"type": "pachca", "token": "tok", "enabled": True}
    })
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([
        _sample_msg(id="pa_src:1", source="pa_src")
    ])
    MockPA.return_value = connector

    stats = run_ingest(config_path=config_path)

    MockPA.assert_called_once()
    call_kwargs = MockPA.call_args[1]
    assert call_kwargs["source_name"] == "pa_src"
    assert call_kwargs["access_token"] == "tok"
    assert stats["messages_new"] == 1


# ── alias resolution ──────────────────────────────────────────────────────────

@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_resolves_sender_to_canonical(MockMM, MockVS, tmp_path):
    user_aliases = [{"canonical_name": "Jane Smith", "aliases": ["jsmith", "jane"]}]
    config_path, _ = _write_config(
        tmp_path,
        sources={"mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}},
        user_aliases=user_aliases,
    )
    msg = _sample_msg(id="mm:1", source="mm")
    msg["sender"] = "jsmith"
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg])
    MockMM.return_value = connector

    run_ingest(config_path=config_path)

    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    rows = db.search()
    assert rows[0]["canonical_sender"] == "Jane Smith"


@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_sets_mentions_me_flag(MockMM, MockVS, tmp_path):
    config_path, _ = _write_config(
        tmp_path,
        sources={"mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}},
        my_aliases=["john"],
    )
    msg = _sample_msg(id="mm:1", source="mm")
    msg["text"] = "hey @john please review"
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg])
    MockMM.return_value = connector

    run_ingest(config_path=config_path)

    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    rows = db.search(mentions_me=True)
    assert len(rows) == 1


# ── internal / external classification ────────────────────────────────────────

@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_source_internal_flag_marks_messages(MockMM, MockVS, tmp_path):
    config_path, _ = _write_config(tmp_path, sources={
        "mm": {"type": "mattermost", "url": "http://x", "token": "t",
               "enabled": True, "internal": True}
    })
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([_sample_msg(id="mm:1", source="mm")])
    MockMM.return_value = connector

    run_ingest(config_path=config_path)

    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    assert db.search()[0]["internal"] == 1


@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_alias_internal_flag_marks_messages(MockMM, MockVS, tmp_path):
    config_path, _ = _write_config(
        tmp_path,
        sources={"mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}},
        user_aliases=[{"canonical_name": "Jane Smith", "internal": True, "aliases": ["jsmith"]}],
    )
    msg = _sample_msg(id="mm:1", source="mm")
    msg["sender"] = "jsmith"
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg])
    MockMM.return_value = connector

    run_ingest(config_path=config_path)

    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    assert db.search()[0]["internal"] == 1


@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_unflagged_sender_is_external(MockMM, MockVS, tmp_path):
    config_path, _ = _write_config(
        tmp_path,
        sources={"mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}},
        user_aliases=[{"canonical_name": "Bob Wilson", "aliases": ["bob"]}],  # no internal flag
    )
    msg = _sample_msg(id="mm:1", source="mm")
    msg["sender"] = "bob"
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg])
    MockMM.return_value = connector

    run_ingest(config_path=config_path)

    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    assert db.search()[0]["internal"] == 0


# ── mention extraction wiring ────────────────────────────────────────────────

@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_writes_mention_rows_for_new_messages(MockMM, MockVS, tmp_path):
    user_aliases = [{"canonical_name": "Bob Wilson", "aliases": ["bob"]}]
    config_path, _ = _write_config(
        tmp_path,
        sources={"mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}},
        user_aliases=user_aliases,
    )
    msg = _sample_msg(id="mm:1", source="mm")
    msg["text"] = "ping @bob and @carol please"
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg])
    MockMM.return_value = connector

    run_ingest(config_path=config_path)

    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    rows = db.get_mentions(mentioned_canonical="Bob Wilson")
    assert len(rows) == 1
    assert rows[0]["mentioned_token"] == "@bob"
    # @carol has no alias group — canonical stays unresolved (None).
    raw = db.get_mentions()
    tokens = sorted(r["mentioned_token"] for r in raw)
    assert tokens == ["@bob", "@carol"]


@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_no_duplicate_mention_rows_on_re_ingest(MockMM, MockVS, tmp_path):
    config_path, _ = _write_config(
        tmp_path,
        sources={"mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}},
    )
    msg = _sample_msg(id="mm:1", source="mm")
    msg["text"] = "hello @alice"
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg])
    MockMM.return_value = connector

    run_ingest(config_path=config_path)
    run_ingest(config_path=config_path)  # duplicate — must not re-write mentions

    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    assert len(db.get_mentions()) == 1


@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_resolves_mentioned_sender_id_from_senders_table(MockMM, MockVS, tmp_path):
    """A pre-cached sender's @username mention should fill mentioned_sender_id."""
    config_path, _ = _write_config(
        tmp_path,
        sources={"mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}},
    )
    msg = _sample_msg(id="mm:1", source="mm")
    msg["text"] = "thanks @charlie"
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg])
    MockMM.return_value = connector

    # Pre-populate the senders table so the username lookup succeeds.
    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    db.upsert_sender({"sender_id": "user_charlie", "source": "mm",
                      "username": "charlie", "full_name": "Charlie"})
    db.close()

    run_ingest(config_path=config_path)

    db = Database(str(tmp_path / "db.sqlite"))
    rows = db.get_mentions()
    assert len(rows) == 1
    assert rows[0]["mentioned_sender_id"] == "user_charlie"


@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_caches_senders_before_mention_insertion(MockMM, MockVS, tmp_path):
    """Regression: senders must be cached BEFORE messages are inserted, so that
    _insert_mention_rows → find_sender_id_by_username has data to match against.
    On a fresh DB, a mention of @alice from a message Alice ALSO sent in the same
    batch must resolve mentioned_sender_id, not leave it NULL."""
    config_path, _ = _write_config(
        tmp_path,
        sources={"mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}},
    )
    msg_from_alice = _sample_msg(id="mm:1", source="mm")
    msg_from_alice["sender"] = "alice"
    msg_from_alice["sender_id"] = "u_alice"
    msg_from_alice["text"] = "first message"

    msg_mentioning_alice = _sample_msg(id="mm:2", source="mm")
    msg_mentioning_alice["sender"] = "bob"
    msg_mentioning_alice["sender_id"] = "u_bob"
    msg_mentioning_alice["text"] = "hey @alice please look"

    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result(
        [msg_from_alice, msg_mentioning_alice]
    )
    connector.get_sender_info.side_effect = lambda sid: {
        "u_alice": {"sender_id": "u_alice", "source": "mm",
                    "username": "alice", "full_name": "Alice"},
        "u_bob":   {"sender_id": "u_bob",   "source": "mm",
                    "username": "bob",   "full_name": "Bob"},
    }[sid]
    MockMM.return_value = connector

    run_ingest(config_path=config_path)

    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    rows = db.get_mentions()
    assert len(rows) == 1
    # The bug being regressed against: this was NULL before the ordering fix.
    assert rows[0]["mentioned_sender_id"] == "u_alice"


@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_message_with_no_mentions_writes_no_rows(MockMM, MockVS, tmp_path):
    config_path, _ = _write_config(
        tmp_path,
        sources={"mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}},
    )
    msg = _sample_msg(id="mm:1", source="mm")
    msg["text"] = "just a plain message"
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg])
    MockMM.return_value = connector

    run_ingest(config_path=config_path)

    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    assert db.get_mentions() == []


# ── internal_domains rule ─────────────────────────────────────────

@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_classifies_internal_from_cached_sender_email(MockMM, MockVS, tmp_path):
    """A sender on an internal domain is classified internal even without alias / source flag."""
    config_path, _ = _write_config(
        tmp_path,
        sources={"mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}},
        internal_domains=["mycompany.com"],
    )
    # Seed the senders cache so the enrichment lookup finds an email.
    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    db.upsert_sender({"sender_id": "u1", "source": "mm",
                      "username": "alice", "full_name": "Alice",
                      "email": "alice@mycompany.com"})
    db.close()

    msg = _sample_msg(id="mm:1", source="mm")  # sender="alice", sender_id="u1"
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg])
    MockMM.return_value = connector

    run_ingest(config_path=config_path)

    db = Database(str(tmp_path / "db.sqlite"))
    assert db.search()[0]["internal"] == 1


@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_external_domain_stays_external(MockMM, MockVS, tmp_path):
    config_path, _ = _write_config(
        tmp_path,
        sources={"mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}},
        internal_domains=["mycompany.com"],
    )
    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    db.upsert_sender({"sender_id": "u1", "source": "mm",
                      "username": "alice", "full_name": "Alice",
                      "email": "alice@client.com"})
    db.close()

    msg = _sample_msg(id="mm:1", source="mm")
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg])
    MockMM.return_value = connector

    run_ingest(config_path=config_path)

    db = Database(str(tmp_path / "db.sqlite"))
    assert db.search()[0]["internal"] == 0


@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.EmailConnector")
def test_run_ingest_email_internal_only_when_all_recipients_internal(MockE, MockVS, tmp_path):
    """Recipient-aware: sender + every recipient must be on the internal domain."""
    config_path, _ = _write_config(
        tmp_path,
        sources={"em": {"type": "email", "enabled": True, "host": "h", "port": 993,
                        "username": "me@mycompany.com", "password": "p"}},
        internal_domains=["mycompany.com"],
    )

    msg_internal = {
        **_sample_msg(id="em:<int@x>", source="em", channel_id="folder:INBOX"),
        "sender": "alice", "sender_id": "alice@mycompany.com",
        "sender_email": "alice@mycompany.com",
        "_recipient_emails": ["bob@mycompany.com", "carol@mycompany.com"],
    }
    msg_mixed = {
        **_sample_msg(id="em:<mix@x>", source="em", channel_id="folder:INBOX"),
        "sender": "alice", "sender_id": "alice@mycompany.com",
        "sender_email": "alice@mycompany.com",
        "_recipient_emails": ["bob@mycompany.com", "outsider@client.com"],
    }
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg_internal, msg_mixed])
    MockE.return_value = connector

    run_ingest(config_path=config_path)

    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    rows = {r["id"]: r for r in db.search()}
    assert rows["em:<int@x>"]["internal"] == 1
    assert rows["em:<mix@x>"]["internal"] == 0


@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_alias_false_demotes_internal_domain_sender(MockMM, MockVS, tmp_path):
    """Contractor pattern: alias says `internal: false` even though domain matches."""
    config_path, _ = _write_config(
        tmp_path,
        sources={"mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}},
        internal_domains=["mycompany.com"],
        user_aliases=[{"canonical_name": "Contractor", "aliases": ["alice"],
                       "internal": False}],
    )
    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    db.upsert_sender({"sender_id": "u1", "source": "mm",
                      "username": "alice", "full_name": "Alice",
                      "email": "alice@mycompany.com"})
    db.close()

    msg = _sample_msg(id="mm:1", source="mm")
    connector = MagicMock()
    connector.fetch_messages.return_value = _connector_result([msg])
    MockMM.return_value = connector

    run_ingest(config_path=config_path)

    db = Database(str(tmp_path / "db.sqlite"))
    assert db.search()[0]["internal"] == 0


# ── parallel fetch ────────────────────────────────────────────────

@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.PachcaConnector")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_aggregates_messages_from_multiple_sources_in_parallel(
    MockMM, MockPA, MockVS, tmp_path,
):
    """End-to-end: two enabled sources → both result sets land in the DB."""
    config_path, _ = _write_config(tmp_path, sources={
        "mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True},
        "pa": {"type": "pachca", "token": "t", "enabled": True},
    })
    mm_conn = MagicMock()
    mm_conn.fetch_messages.return_value = _connector_result([
        _sample_msg(id="mm:1", source="mm"),
        _sample_msg(id="mm:2", source="mm"),
    ])
    MockMM.return_value = mm_conn
    pa_conn = MagicMock()
    pa_conn.fetch_messages.return_value = _connector_result([
        _sample_msg(id="pa:1", source="pa"),
    ])
    MockPA.return_value = pa_conn

    stats = run_ingest(config_path=config_path)

    assert stats["sources_checked"] == 2
    assert stats["messages_fetched"] == 3
    assert stats["messages_new"] == 3

    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    ids = {r["id"] for r in db.search()}
    assert ids == {"mm:1", "mm:2", "pa:1"}


@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.PachcaConnector")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_one_source_raising_lets_other_land_and_records_partial(
    MockMM, MockPA, MockVS, tmp_path,
):
    """Isolation: a connector failure surfaces in source_errors and `partial`,
    while the other source's messages still get stored."""
    config_path, _ = _write_config(tmp_path, sources={
        "mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True},
        "pa": {"type": "pachca", "token": "t", "enabled": True},
    })
    mm_conn = MagicMock()
    mm_conn.fetch_messages.side_effect = RuntimeError("mm 500")
    MockMM.return_value = mm_conn
    pa_conn = MagicMock()
    pa_conn.fetch_messages.return_value = _connector_result([
        _sample_msg(id="pa:1", source="pa"),
    ])
    MockPA.return_value = pa_conn

    run_ingest(config_path=config_path)

    from storage.db import Database
    db = Database(str(tmp_path / "db.sqlite"))
    assert [r["id"] for r in db.search()] == ["pa:1"]
    last_run = db.get_last_ingest_run()
    assert last_run["status"] == "partial"
    assert any("mm 500" in e["error"] for e in last_run["errors"])


@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_fetch_workers_one_takes_sequential_path(
    MockMM, MockVS, tmp_path,
):
    """fetch_workers=1 must skip the executor entirely (the legacy path)."""
    cfg = {
        "sqlite_path": str(tmp_path / "db.sqlite"),
        "chroma_path": str(tmp_path / "chroma"),
        "sources": {"mm": {"type": "mattermost", "url": "u", "token": "t", "enabled": True}},
        "ingest": {"fetch_workers": 1},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(cfg))
    mm_conn = MagicMock()
    mm_conn.fetch_messages.return_value = _connector_result([_sample_msg(id="mm:1", source="mm")])
    MockMM.return_value = mm_conn

    with patch("pipeline.ingest.ThreadPoolExecutor") as Pool:
        run_ingest(config_path=str(config_path))
    Pool.assert_not_called()
