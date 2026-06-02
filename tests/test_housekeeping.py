"""Tests for pipeline/housekeeping.run_housekeeping.

Exercises the orchestrator against a real SQLite DB (so the prune transaction
is the real thing) and a mock VectorStore + real tmp filesystem.
"""
import yaml
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from pipeline.housekeeping import run_housekeeping
from storage.db import Database


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()


@pytest.fixture
def vs():
    """Mock vector store that records its delete_many calls."""
    m = MagicMock()
    m.delete_many.side_effect = lambda ids, **kw: len(ids)
    return m


@pytest.fixture
def cache_dir(tmp_path):
    d = tmp_path / "file_cache"
    d.mkdir()
    return d


def _msg(id="src:m1", source="src", channel_id="c1",
         timestamp="2024-01-01T00:00:00+00:00", text="hi", **kw):
    base = {"id": id, "source": source, "channel_id": channel_id, "sender": "alice",
            "sender_id": "u1", "timestamp": timestamp, "text": text,
            "thread_id": None, "reply_to_id": None, "tags": [], "raw": {}}
    base.update(kw)
    return base


NOW = datetime(2026, 5, 30, tzinfo=timezone.utc)


# ── early-out paths ──────────────────────────────────────────────────────────

def test_disabled_when_retention_days_is_falsy(db, vs, cache_dir):
    r = run_housekeeping(db, vs, str(cache_dir), retention_days=None, file_cache_grace_seconds=0, now=NOW)
    assert r["status"] == "disabled"
    vs.delete_many.assert_not_called()


def test_disabled_when_retention_days_is_zero(db, vs, cache_dir):
    r = run_housekeeping(db, vs, str(cache_dir), retention_days=0, file_cache_grace_seconds=0, now=NOW)
    assert r["status"] == "disabled"


def test_throttled_when_last_prune_is_fresh(db, vs, cache_dir):
    """A successful prune 30 minutes ago should cause the next call to skip."""
    fresh = (NOW - timedelta(minutes=30)).isoformat()
    db.record_prune_run({"started_at": fresh, "finished_at": fresh})
    r = run_housekeeping(db, vs, str(cache_dir),
                         retention_days=365, prune_interval_hours=24, now=NOW)
    assert r["status"] == "throttled"
    vs.delete_many.assert_not_called()


def test_not_throttled_when_last_prune_is_old_enough(db, vs, cache_dir):
    old = (NOW - timedelta(hours=48)).isoformat()
    db.record_prune_run({"started_at": old, "finished_at": old})
    r = run_housekeeping(db, vs, str(cache_dir),
                         retention_days=365, prune_interval_hours=24, now=NOW)
    assert r["status"] == "ok"


def test_dry_run_bypasses_throttle(db, vs, cache_dir):
    """Operator wants to preview anytime; dry-run shouldn't be blocked by throttle."""
    fresh = (NOW - timedelta(minutes=5)).isoformat()
    db.record_prune_run({"started_at": fresh, "finished_at": fresh})
    r = run_housekeeping(db, vs, str(cache_dir),
                         retention_days=365, prune_interval_hours=24,
                         dry_run=True, now=NOW)
    assert r["status"] == "dry_run"


# ── real-run behavior ────────────────────────────────────────────────────────

def test_prunes_old_messages_and_propagates_to_vector_store(db, vs, cache_dir):
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00"))
    db.insert(_msg(id="src:new", timestamp="2026-01-01T00:00:00+00:00"))
    r = run_housekeeping(db, vs, str(cache_dir), retention_days=365, file_cache_grace_seconds=0, now=NOW)
    assert r["status"] == "ok"
    assert r["messages_deleted"] == 1
    assert r["vectors_deleted"] == 1
    vs.delete_many.assert_called_once()
    assert vs.delete_many.call_args[0][0] == ["src:old"]
    remaining = [m["id"] for m in db.search()]
    assert remaining == ["src:new"]


def test_writes_prune_runs_audit_row(db, vs, cache_dir):
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00"))
    run_housekeeping(db, vs, str(cache_dir), retention_days=365, file_cache_grace_seconds=0, now=NOW)
    assert db.last_prune_at() is not None


def test_dry_run_does_not_write_or_delete(db, vs, cache_dir):
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00"))
    r = run_housekeeping(db, vs, str(cache_dir),
                         retention_days=365, dry_run=True, now=NOW)
    assert r["status"] == "dry_run"
    assert r["messages_deleted"] == 1     # would-delete count
    # Real DB unchanged.
    assert len(db.search()) == 1
    # No vector calls in dry run.
    vs.delete_many.assert_not_called()
    # No audit row written.
    assert db.last_prune_at() is None


def test_vector_store_failure_does_not_abort_sql_prune(db, vs, cache_dir):
    """Chroma falling over must not roll back the SQL prune — degraded, not fatal."""
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00"))
    vs.delete_many.side_effect = RuntimeError("chroma is having a day")
    r = run_housekeeping(db, vs, str(cache_dir), retention_days=365, file_cache_grace_seconds=0, now=NOW)
    assert r["status"] == "partial"
    assert r["messages_deleted"] == 1
    assert "vectors" in (r.get("error") or "")
    assert len(db.search()) == 0   # SQL prune still committed
    # Audit row written even on partial; last_prune_at() intentionally ignores
    # errored rows (a degraded run shouldn't double as a throttle marker).
    assert db.last_prune_at() is None
    row = db.conn.execute(
        "SELECT error FROM prune_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["error"] and "chroma" in row["error"]


def test_vs_none_skips_vector_fan_out(db, cache_dir):
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00"))
    r = run_housekeeping(db, vs=None, attachments_path=str(cache_dir),
                         retention_days=365, file_cache_grace_seconds=0, now=NOW)
    assert r["status"] == "ok"
    assert r["vectors_deleted"] == 0


# ── file cache mark-and-sweep ────────────────────────────────────────────────

def test_file_cache_sweep_removes_orphans_only(db, vs, cache_dir):
    # Two cache files exist. Only one is referenced by a surviving message.
    (cache_dir / "keep_me_abc123.pdf").write_bytes(b"x" * 100)
    (cache_dir / "orphan_zzz999.jpg").write_bytes(b"x" * 200)
    db.insert(_msg(id="src:1", timestamp="2026-01-01T00:00:00+00:00",
                   text="hi [attachment: file.pdf, file_id=keep_me_abc123]"))
    r = run_housekeeping(db, vs, str(cache_dir), retention_days=365, file_cache_grace_seconds=0, now=NOW)
    assert (cache_dir / "keep_me_abc123.pdf").exists()
    assert not (cache_dir / "orphan_zzz999.jpg").exists()
    assert r["files_deleted"] == 1
    assert r["files_freed_bytes"] == 200


def test_file_cache_sweep_keeps_shared_file_when_one_referencing_msg_survives(db, vs, cache_dir):
    """Content-addressed safety: a file_id mentioned by ANY surviving message stays,
    even if the message that originally added it is being pruned."""
    (cache_dir / "shared_id.pdf").write_bytes(b"y" * 50)
    # Both messages reference the same file_id. The old one will be pruned.
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00",
                   text="[attachment: f.pdf, file_id=shared_id]"))
    db.insert(_msg(id="src:new", timestamp="2026-01-01T00:00:00+00:00",
                   text="[attachment: f.pdf, file_id=shared_id]"))
    r = run_housekeeping(db, vs, str(cache_dir), retention_days=365, file_cache_grace_seconds=0, now=NOW)
    assert r["messages_deleted"] == 1
    # File kept because the new message still references it.
    assert (cache_dir / "shared_id.pdf").exists()
    assert r["files_deleted"] == 0


def test_file_cache_sweep_handles_nonexistent_dir(db, vs, tmp_path):
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00"))
    r = run_housekeeping(db, vs, str(tmp_path / "does-not-exist"),
                         retention_days=365, file_cache_grace_seconds=0, now=NOW)
    assert r["status"] == "ok"
    assert r["files_deleted"] == 0


# ── recipient-aware (preexisting tables) untouched ───────────────────────────

def test_does_not_touch_channels_or_senders(db, vs, cache_dir):
    db.upsert_channel({"id": "ch1", "source": "src", "name": "dev",
                       "display_name": "Dev", "last_update_at": 1})
    db.upsert_sender({"sender_id": "u1", "source": "src", "username": "alice",
                      "full_name": "Alice"})
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00"))
    run_housekeeping(db, vs, str(cache_dir), retention_days=365, file_cache_grace_seconds=0, now=NOW)
    assert db.list_channels()
    assert db.get_sender("src", "u1") is not None


# ── ingest-end hook (TASK-028 wiring inside pipeline.ingest) ─────────────────

def test_ingest_end_hook_catches_housekeeping_exception(tmp_path):
    """If housekeeping blows up, the ingest must still return its stats cleanly."""
    cfg = {
        "sqlite_path": str(tmp_path / "db.sqlite"),
        "chroma_path": str(tmp_path / "chroma"),
        "sources": {"mm": {"type": "mattermost", "url": "u", "token": "t", "enabled": True}},
        "retention": {"retention_days": 365},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(cfg))

    from unittest.mock import MagicMock as MM
    with patch("pipeline.ingest.VectorStore"), \
        patch("connectors.factory.MattermostConnector") as MockMM, \
        patch("pipeline.housekeeping.run_housekeeping",
              side_effect=RuntimeError("housekeeping kaboom")):
        connector = MM()
        connector.fetch_messages.return_value = {
            "messages": [], "messages_count": 0,
            "channels_scanned": 0, "channels_skipped": 0,
        }
        MockMM.return_value = connector
        from pipeline.ingest import run_ingest
        stats = run_ingest(config_path=str(config_path))
        # Ingest completed normally even though housekeeping raised.
        assert stats["sources_checked"] == 1


def test_ingest_end_hook_runs_housekeeping_when_status_ok(tmp_path):
    cfg = {
        "sqlite_path": str(tmp_path / "db.sqlite"),
        "chroma_path": str(tmp_path / "chroma"),
        "sources": {"mm": {"type": "mattermost", "url": "u", "token": "t", "enabled": True}},
        "retention": {"retention_days": 365},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(cfg))

    from unittest.mock import MagicMock as MM
    with patch("pipeline.ingest.VectorStore"), \
        patch("connectors.factory.MattermostConnector") as MockMM, \
        patch("pipeline.housekeeping.run_housekeeping",
              return_value={"status": "ok", "messages_deleted": 0,
                            "mentions_deleted": 0, "vectors_deleted": 0,
                            "files_deleted": 0, "sent_deleted": 0,
                            "runs_deleted": 0}) as mock_hk:
        connector = MM()
        connector.fetch_messages.return_value = {
            "messages": [], "messages_count": 0,
            "channels_scanned": 0, "channels_skipped": 0,
        }
        MockMM.return_value = connector
        from pipeline.ingest import run_ingest
        run_ingest(config_path=str(config_path))
        mock_hk.assert_called_once()


def test_ingest_end_hook_skips_housekeeping_when_retention_block_absent(tmp_path):
    """Opt-in default: no retention block → housekeeping doesn't fire at all.
    (Actually it fires but reports `disabled`; tested via run_housekeeping above —
    here we just verify ingest doesn't crash.)"""
    cfg = {
        "sqlite_path": str(tmp_path / "db.sqlite"),
        "chroma_path": str(tmp_path / "chroma"),
        "sources": {"mm": {"type": "mattermost", "url": "u", "token": "t", "enabled": True}},
        # no retention block
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(cfg))

    from unittest.mock import MagicMock as MM
    with patch("pipeline.ingest.VectorStore"), \
            patch("connectors.factory.MattermostConnector") as MockMM:
        connector = MM()
        connector.fetch_messages.return_value = {
            "messages": [], "messages_count": 0,
            "channels_scanned": 0, "channels_skipped": 0,
        }
        MockMM.return_value = connector
        from pipeline.ingest import run_ingest
        stats = run_ingest(config_path=str(config_path))
        assert stats["sources_checked"] == 1


# ── orphan-vs-retention split (TASK-028 follow-up) ──────────────────────────

def test_split_pure_orphan_vs_retention_driven_sweep(db, vs, cache_dir):
    """The sweep should distinguish files orphaned by a previous filter/dupe
    cycle from files whose last reference was just deleted by retention."""
    # File A — referenced only by an OLD message (will be deleted by retention).
    (cache_dir / "ret_file_aaa.pdf").write_bytes(b"x" * 100)
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00",
                   text="[attachment: a.pdf, file_id=ret_file_aaa]"))
    # File B — pure orphan, no message references it at all.
    (cache_dir / "orphan_bbb.jpg").write_bytes(b"y" * 200)
    # File C — referenced by a SURVIVING message, must stay.
    (cache_dir / "keep_ccc.png").write_bytes(b"z" * 50)
    db.insert(_msg(id="src:new", timestamp="2026-01-01T00:00:00+00:00",
                   text="[attachment: c.png, file_id=keep_ccc]"))

    r = run_housekeeping(db, vs, str(cache_dir),
                         retention_days=365, file_cache_grace_seconds=0, now=NOW)
    assert r["status"] == "ok"
    # Surviving file untouched.
    assert (cache_dir / "keep_ccc.png").exists()
    # Both unreferenced files removed.
    assert not (cache_dir / "ret_file_aaa.pdf").exists()
    assert not (cache_dir / "orphan_bbb.jpg").exists()
    # Split is what we care about.
    assert r["files_with_deleted_messages"] == 1   # ret_file_aaa
    assert r["files_orphans_swept"] == 1           # orphan_bbb
    assert r["files_deleted"] == 2                  # sum kept for audit-row compat
    assert r["files_freed_bytes"] == 300


def test_dry_run_split_treats_everything_as_orphan(db, vs, cache_dir):
    """Dry-run can't tell retention-driven from orphan without actually pruning.
    It defaults to reporting both totals as orphans + 0 retention."""
    (cache_dir / "ret_file_aaa.pdf").write_bytes(b"x" * 100)
    db.insert(_msg(id="src:old", timestamp="2024-01-01T00:00:00+00:00",
                   text="[attachment: a.pdf, file_id=ret_file_aaa]"))
    r = run_housekeeping(db, vs, str(cache_dir), retention_days=365,
                         file_cache_grace_seconds=0, dry_run=True, now=NOW)
    assert r["status"] == "dry_run"
    # Dry-run uses the LIVE referenced set — the file still has its reference,
    # so it's not in would_unlink.
    assert r["files_deleted"] == 0
    # Add a pure orphan and re-run dry to confirm the orphan path counts it:
    (cache_dir / "orphan_bbb.jpg").write_bytes(b"y" * 200)
    r2 = run_housekeeping(db, vs, str(cache_dir), retention_days=365,
                          file_cache_grace_seconds=0, dry_run=True, now=NOW)
    assert r2["files_deleted"] == 1
    assert r2["files_orphans_swept"] == 1
    assert r2["files_with_deleted_messages"] == 0


# ── grace period (TASK-028 follow-up) ───────────────────────────────────────

def test_grace_period_skips_recently_written_files(db, vs, cache_dir):
    """A file written < grace ago must NOT be swept even when unreferenced."""
    (cache_dir / "fresh_orphan.pdf").write_bytes(b"x" * 100)
    db.insert(_msg(id="src:new", timestamp="2026-01-01T00:00:00+00:00"))
    # Grace = 1 hour: file is brand new, must stay.
    r = run_housekeeping(db, vs, str(cache_dir), retention_days=365,
                         file_cache_grace_seconds=3600, now=NOW)
    assert (cache_dir / "fresh_orphan.pdf").exists()
    assert r["files_deleted"] == 0


def test_grace_period_zero_sweeps_even_fresh_files(db, vs, cache_dir):
    """The existing test contract: grace=0 still sweeps as before."""
    (cache_dir / "orphan.pdf").write_bytes(b"x" * 100)
    db.insert(_msg(id="src:new", timestamp="2026-01-01T00:00:00+00:00"))
    r = run_housekeeping(db, vs, str(cache_dir), retention_days=365,
                         file_cache_grace_seconds=0, now=NOW)
    assert not (cache_dir / "orphan.pdf").exists()
    assert r["files_deleted"] == 1


def test_grace_period_applies_to_dry_run_projection(db, vs, cache_dir):
    """Dry-run must use the same grace cutoff so the preview matches reality."""
    (cache_dir / "fresh_orphan.pdf").write_bytes(b"x" * 100)
    db.insert(_msg(id="src:new", timestamp="2026-01-01T00:00:00+00:00"))
    r = run_housekeeping(db, vs, str(cache_dir), retention_days=365,
                         file_cache_grace_seconds=3600, dry_run=True, now=NOW)
    assert r["files_deleted"] == 0


def test_grace_period_threshold_uses_mtime(db, vs, cache_dir):
    """Files older than the cutoff still get swept; files newer than it don't."""
    import os
    (cache_dir / "old_orphan.pdf").write_bytes(b"x" * 100)
    # Backdate the file's mtime to two hours ago.
    p = cache_dir / "old_orphan.pdf"
    old_mtime = p.stat().st_mtime - 7200
    os.utime(p, (old_mtime, old_mtime))
    (cache_dir / "fresh_orphan.pdf").write_bytes(b"y" * 50)
    db.insert(_msg(id="src:new", timestamp="2026-01-01T00:00:00+00:00"))
    # Grace = 1h: old file goes, fresh stays.
    r = run_housekeeping(db, vs, str(cache_dir), retention_days=365,
                         file_cache_grace_seconds=3600, now=NOW)
    assert not (cache_dir / "old_orphan.pdf").exists()
    assert (cache_dir / "fresh_orphan.pdf").exists()
    assert r["files_deleted"] == 1
    assert r["files_orphans_swept"] == 1
