"""Tests for pipeline/health.py and related DB methods."""
from unittest.mock import patch

import pytest

from pipeline.health import build_health_report, format_health_text, _exit_code
from storage.db import Database


# ── DB helpers ────────────────────────────────────────────────────────────────

def _run(status="ok", sources_checked=2, messages_new=5, messages_fetched=10, errors=None):
    return {
        "started_at": "2024-01-15T10:00:00+00:00",
        "finished_at": "2024-01-15T10:00:30+00:00",
        "status": status,
        "sources_checked": sources_checked,
        "messages_new": messages_new,
        "messages_fetched": messages_fetched,
        "errors": errors or [],
    }


def _msg(db, id, source, timestamp):
    db.insert({
        "id": id, "source": source, "channel_id": "ch1",
        "sender": "alice", "sender_id": "u1",
        "timestamp": timestamp, "text": "hi",
        "thread_id": None, "reply_to_id": None, "tags": [], "raw": {},
    })


# ── record_ingest_run / get_last_ingest_run ───────────────────────────────────

def test_record_and_get_last_ingest_run(tmp_db):
    tmp_db.record_ingest_run(_run())
    row = tmp_db.get_last_ingest_run()
    assert row is not None
    assert row["status"] == "ok"
    assert row["messages_new"] == 5
    assert isinstance(row["errors"], list)


def test_get_last_ingest_run_returns_none_when_empty(tmp_db):
    assert tmp_db.get_last_ingest_run() is None


def test_get_last_ingest_run_returns_most_recent(tmp_db):
    tmp_db.record_ingest_run(_run(status="ok"))
    tmp_db.record_ingest_run(_run(status="error", messages_new=0))
    row = tmp_db.get_last_ingest_run()
    assert row["status"] == "error"


def test_record_ingest_run_stores_errors_as_json(tmp_db):
    errors = [{"source": "mm", "error": "connection refused"}]
    tmp_db.record_ingest_run(_run(status="partial", errors=errors))
    row = tmp_db.get_last_ingest_run()
    assert row["errors"] == errors


# ── get_source_health ─────────────────────────────────────────────────────────

def test_get_source_health_returns_empty_when_no_messages(tmp_db):
    assert tmp_db.get_source_health() == []


def test_get_source_health_returns_per_source_stats(tmp_db):
    _msg(tmp_db, "mm:1", "mm", "2024-01-10T08:00:00+00:00")
    _msg(tmp_db, "mm:2", "mm", "2024-01-15T12:00:00+00:00")
    _msg(tmp_db, "tg:1", "tg", "2024-01-12T06:00:00+00:00")

    health = {s["source"]: s for s in tmp_db.get_source_health()}
    assert health["mm"]["count"] == 2
    assert health["mm"]["oldest_message"] == "2024-01-10T08:00:00+00:00"
    assert health["mm"]["last_message"] == "2024-01-15T12:00:00+00:00"
    assert health["tg"]["count"] == 1


# ── build_health_report ───────────────────────────────────────────────────────

def test_build_health_report_structure(tmp_db):
    config = {"sources": {"mm": {"type": "mattermost"}, "tg": {"type": "telegram"}}}
    report = build_health_report(tmp_db, config)
    assert "last_run" in report
    assert "source_health" in report
    assert "configured_sources" in report
    assert report["configured_sources"] == ["mm", "tg"]


def test_build_health_report_no_run(tmp_db):
    report = build_health_report(tmp_db, {"sources": {}})
    assert report["last_run"] is None


def test_build_health_report_reflects_stored_data(tmp_db):
    tmp_db.record_ingest_run(_run(status="ok"))
    _msg(tmp_db, "mm:1", "mm", "2024-01-15T10:00:00+00:00")
    config = {"sources": {"mm": {"type": "mattermost"}}}
    report = build_health_report(tmp_db, config)
    assert report["last_run"]["status"] == "ok"
    assert report["source_health"]["mm"]["count"] == 1


# ── format_health_text ────────────────────────────────────────────────────────

def _sample_report(status="ok", errors=None):
    return {
        "last_run": {
            "status": status,
            "started_at": "2024-01-15T10:00:00+00:00",
            "finished_at": "2024-01-15T10:00:30+00:00",
            "sources_checked": 2,
            "messages_fetched": 10,
            "messages_new": 5,
            "errors": errors or [],
        },
        "source_health": {
            "mm": {
                "source": "mm",
                "oldest_message": "2024-01-01T00:00:00+00:00",
                "last_message": "2024-01-15T10:00:00+00:00",
                "count": 42,
            }
        },
        "configured_sources": ["mm"],
    }


def test_format_health_text_shows_status():
    text = format_health_text(_sample_report("ok"))
    assert "[OK]" in text


def test_format_health_text_shows_partial_status():
    text = format_health_text(_sample_report("partial"))
    assert "[PARTIAL]" in text


def test_format_health_text_shows_error_status():
    text = format_health_text(_sample_report("error"))
    assert "[ERROR]" in text


def test_format_health_text_shows_source_stats():
    text = format_health_text(_sample_report())
    assert "mm" in text
    assert "42" in text


def test_format_health_text_shows_errors_list():
    errors = [{"source": "mm", "error": "401 Unauthorized"}]
    text = format_health_text(_sample_report("partial", errors=errors))
    assert "401 Unauthorized" in text
    assert "mm" in text


def test_format_health_text_no_run_recorded():
    report = {"last_run": None, "source_health": {}, "configured_sources": []}
    text = format_health_text(report)
    assert "No ingest run" in text


def test_format_health_text_missing_source_shows_no_messages():
    report = {
        "last_run": None,
        "source_health": {},
        "configured_sources": ["missing_src"],
    }
    text = format_health_text(report)
    assert "no messages stored" in text


def test_format_health_text_respects_timezone():
    report = _sample_report()
    config = {"display_timezone": "Europe/Moscow"}  # UTC+3
    text = format_health_text(report, config)
    assert "2024-01-15 13:00" in text  # 10:00 UTC → 13:00 MSK


# ── _exit_code ────────────────────────────────────────────────────────────────

def test_exit_code_ok():
    assert _exit_code({"status": "ok"}) == 0


def test_exit_code_partial():
    assert _exit_code({"status": "partial"}) == 1


def test_exit_code_error():
    assert _exit_code({"status": "error"}) == 1


def test_exit_code_no_run():
    assert _exit_code(None) == 2


# ── ingest run recording integration ─────────────────────────────────────────

@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_records_ok_run(MockMM, MockVS, tmp_path):
    import yaml
    from pipeline.ingest import run_ingest

    cfg = {
        "sqlite_path": str(tmp_path / "db.sqlite"),
        "chroma_path": str(tmp_path / "chroma"),
        "sources": {"mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True}},
    }
    config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg))

    connector = pytest.importorskip("unittest.mock").MagicMock()
    connector.fetch_messages.return_value = {
        "messages": [], "messages_count": 0, "channels_scanned": 1, "channels_skipped": 0
    }
    MockMM.return_value = connector

    run_ingest(config_path=config_path)

    db = Database(str(tmp_path / "db.sqlite"))
    row = db.get_last_ingest_run()
    assert row is not None
    assert row["status"] == "ok"
    assert row["errors"] == []


@patch("pipeline.ingest.VectorStore")
@patch("connectors.factory.MattermostConnector")
def test_run_ingest_records_partial_on_fetch_error(MockMM, MockVS, tmp_path):
    import yaml
    from pipeline.ingest import run_ingest

    cfg = {
        "sqlite_path": str(tmp_path / "db.sqlite"),
        "chroma_path": str(tmp_path / "chroma"),
        "sources": {
            "mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True},
        },
    }
    config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg))

    connector = pytest.importorskip("unittest.mock").MagicMock()
    connector.fetch_messages.side_effect = RuntimeError("API timeout")
    MockMM.return_value = connector

    run_ingest(config_path=config_path)

    db = Database(str(tmp_path / "db.sqlite"))
    row = db.get_last_ingest_run()
    assert row["status"] == "partial"
    assert any("API timeout" in e["error"] for e in row["errors"])


@patch("pipeline.ingest.VectorStore")
def test_run_ingest_records_error_when_all_sources_fail(MockVS, tmp_path):
    import yaml
    from pipeline.ingest import run_ingest

    cfg = {
        "sqlite_path": str(tmp_path / "db.sqlite"),
        "chroma_path": str(tmp_path / "chroma"),
        "sources": {
            "mm": {"type": "mattermost", "url": "http://x", "token": "t", "enabled": True},
        },
    }
    config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg))

    with patch("connectors.factory.MattermostConnector") as MockMM:
        MockMM.side_effect = RuntimeError("Bad token")
        run_ingest(config_path=config_path)

    db = Database(str(tmp_path / "db.sqlite"))
    row = db.get_last_ingest_run()
    assert row["status"] == "error"
    assert row["sources_checked"] == 0
