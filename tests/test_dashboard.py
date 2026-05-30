"""Tests for pipeline/dashboard.py — the dashboard data layer (TASK-026)."""
from cli.dashboard import _hour_label_row
from cli.dashboard import _multi_row_bars
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
import pytest

from storage.db import Database
from pipeline import dashboard as data


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "db.sqlite"))
    yield d
    d.close()


@pytest.fixture
def config(tmp_path):
    return {
        "sqlite_path": str(tmp_path / "db.sqlite"),
        "chroma_path": str(tmp_path / "chroma"),
        "display_timezone": "UTC",
        "sources": {
            "mm": {"type": "mattermost", "url": "u", "token": "t", "enabled": True},
        },
    }


def _msg(id="src:1", source="mm", channel_id="ch1", text="hi",
         timestamp=None, **kw):
    base = {
        "id": id, "source": source, "channel_id": channel_id,
        "sender": "alice", "sender_id": "u1",
        "timestamp": timestamp or "2026-01-15T10:00:00+00:00",
        "text": text, "thread_id": None, "reply_to_id": None,
        "tags": [], "raw": {},
    }
    base.update(kw)
    return base


# ── storage_status ───────────────────────────────────────────────────────────

def test_storage_status_with_no_vector_store(db, config):
    db.insert(_msg(id="mm:1"))
    s = data.storage_status(db, vs=None, config=config)
    assert s["sqlite_count"] == 1
    assert s["chroma_count"] is None
    assert s["drift"] is None
    assert s["sqlite_bytes"] > 0
    assert "B" in s["sqlite_size"] or "KB" in s["sqlite_size"]


def test_storage_status_includes_drift_when_vs_present(db, config):
    db.insert(_msg(id="mm:1"))
    db.insert(_msg(id="mm:2"))
    vs = MagicMock()
    vs.count.return_value = 3  # one extra vector — should surface as drift=+1
    s = data.storage_status(db, vs=vs, config=config)
    assert s["sqlite_count"] == 2
    assert s["chroma_count"] == 3
    assert s["drift"] == 1


def test_storage_status_swallows_vs_failure(db, config):
    vs = MagicMock()
    vs.count.side_effect = RuntimeError("chroma down")
    s = data.storage_status(db, vs=vs, config=config)
    assert s["chroma_count"] is None
    assert "chroma down" in s["chroma_error"]
    assert s["drift"] is None


# ── ingest_status ────────────────────────────────────────────────────────────

def test_ingest_status_never_run(db, config):
    s = data.ingest_status(db, config)
    assert s["status_badge"] == "NEVER_RUN"
    assert s["last_run"] == {}
    assert s["cadence_minutes"] is None


def test_ingest_status_computes_cadence_median(db, config):
    """Three runs at 5, 10, 15 minutes apart → median gap = 5min."""
    now = datetime.now(timezone.utc)
    for i in range(4):
        started = (now - timedelta(minutes=15 * (3 - i))).isoformat()
        finished = (now - timedelta(minutes=15 * (3 - i) - 1)).isoformat()
        db.record_ingest_run({"started_at": started, "finished_at": finished,
                              "status": "ok"})
    s = data.ingest_status(db, config)
    assert s["status_badge"] == "OK"
    assert s["cadence_minutes"] == 15  # all gaps are 15 min → median 15


def test_ingest_status_flags_stale_when_last_run_too_old(db, config):
    """If finished_at is way past 2.5x cadence, badge becomes STALE."""
    now = datetime.now(timezone.utc)
    # Plant several runs at 5-min intervals to establish cadence...
    for i in range(5):
        started = (now - timedelta(minutes=5 * (10 + i))).isoformat()
        finished = (now - timedelta(minutes=5 * (10 + i) - 1)).isoformat()
        db.record_ingest_run({"started_at": started, "finished_at": finished,
                              "status": "ok"})
    # ...but the LAST one is way back.
    s = data.ingest_status(db, config)
    # last finished was at minute -10*5+1=-49; cadence ~5min; >> 2.5*5=12.5 min → STALE
    assert s["status_badge"] == "STALE"


# ── source_health ────────────────────────────────────────────────────────────

def test_source_health_one_row_per_configured_source(db, config):
    db.insert(_msg(id="mm:1", source="mm", internal=1))
    db.insert(_msg(id="mm:2", source="mm", internal=0))
    rows = data.source_health(db, config)
    assert len(rows) == 1
    r = rows[0]
    assert r["source"] == "mm"
    assert r["count"] == 2
    assert r["total"] == 2
    assert r["internal"] == 1


def test_source_health_marks_stale_when_last_message_old(db, config):
    db.insert(_msg(id="mm:1", source="mm",
                   timestamp="2020-01-01T00:00:00+00:00"))
    r = data.source_health(db, config)[0]
    assert r["stale"] is True
    assert "ago" in r["last_age"]


# ── top_channels / top_senders ───────────────────────────────────────────────

def test_top_channels_returns_db_row_shape(db):
    db.upsert_channel({"id": "ch1", "source": "mm", "name": "dev",
                       "display_name": "Dev", "last_update_at": 1})
    db.insert(_msg(id="mm:1", channel_id="ch1",
                   timestamp="2026-01-15T10:00:00+00:00"))
    rows = data.top_channels(db, "2020-01-01T00:00:00+00:00")
    assert rows[0]["channel"] == "Dev"
    assert rows[0]["count"] == 1


def test_top_senders_returns_db_row_shape(db):
    db.insert(_msg(id="mm:1", sender="alice", canonical_sender="Alice",
                   timestamp="2026-01-15T10:00:00+00:00"))
    rows = data.top_senders(db, "2020-01-01T00:00:00+00:00")
    assert rows[0]["sender"] == "Alice"


# ── messages_per_day zero-fill ───────────────────────────────────────────────

def test_messages_per_day_zero_fills_gaps_and_returns_oldest_to_newest(db):
    today = datetime.now(timezone.utc)
    # Plant one message yesterday.
    yesterday = (today - timedelta(days=1)).isoformat()
    db.insert(_msg(id="mm:1", timestamp=yesterday))
    days = data.messages_per_day(db, days=5)
    # 5 rows, oldest → newest.
    assert len(days) == 5
    dates = [d["date"] for d in days]
    assert dates == sorted(dates)
    # Yesterday has the message; other days zero.
    counts = {d["date"]: d["count"] for d in days}
    yesterday_key = (today - timedelta(days=1)).date().isoformat()
    assert counts[yesterday_key] == 1
    # All others are 0.
    assert sum(1 for v in counts.values() if v == 0) == 4
    # Weekday tags are present.
    assert all(0 <= d["weekday"] <= 6 for d in days)


# ── hour_of_day_histogram ────────────────────────────────────────────────────

def test_hour_of_day_histogram_returns_24_buckets(db):
    db.insert(_msg(id="mm:1", timestamp="2026-01-15T10:00:00+00:00"))
    buckets = data.hour_of_day_histogram(db, days=10_000, tz_name="UTC")
    assert len(buckets) == 24
    assert buckets[10] == 1


# ── send_stats / tool_call_stats wrappers ────────────────────────────────────

def test_send_stats_returns_d1_and_d7_buckets(db):
    db.record_sent_message({"source": "mm", "channel": "c", "text": "ok",
                            "success": True,
                            "sent_at": datetime.now(timezone.utc).isoformat()})
    s = data.send_stats(db)
    assert s["d1"]["total"] == 1
    assert s["d7"]["total"] == 1


def test_tool_call_stats_returns_d1_and_d7_buckets(db):
    db.log_tool_call(tool_name="search_messages", duration_ms=10, success=True)
    s = data.tool_call_stats(db)
    by_tool = {r["tool_name"]: r for r in s["d1"]}
    assert by_tool["search_messages"]["total"] == 1


# ── latest_messages / mentions_me wrappers ───────────────────────────────────

def test_latest_messages_passthrough(db):
    db.insert(_msg(id="mm:1"))
    assert data.latest_messages(db, limit=5)[0]["id"] == "mm:1"


def test_mentions_me_returns_counts_and_recent(db):
    now = datetime.now(timezone.utc)
    db.insert(_msg(id="mm:1", mentions_me=1,
                   timestamp=(now - timedelta(minutes=10)).isoformat()))
    m = data.mentions_me(db, recent_limit=3)
    assert m["counts"]["h1"] == 1
    assert m["counts"]["d1"] == 1
    assert m["counts"]["d7"] == 1
    assert m["recent"][0]["id"] == "mm:1"


# ── one-call snapshot ────────────────────────────────────────────────────────

def test_build_dashboard_snapshot_includes_every_panel(db, config):
    snap = data.build_dashboard_snapshot(db, vs=None, config=config)
    expected_keys = {
        "generated_at", "tz",
        "storage", "ingest", "source_health",
        "top_channels_d1", "top_channels_d7",
        "top_senders_d1",  "top_senders_d7",
        "messages_per_day", "hour_of_day_histogram",
        "send_stats", "tool_call_stats",
        "latest_messages", "mentions_me",
    }
    assert expected_keys <= set(snap.keys())
    assert snap["tz"] == "UTC"


def test_build_dashboard_snapshot_messages_per_day_is_90d():
    """Regression: bumped 30 → 90 days for the wider chart."""
    from pipeline import dashboard as _data
    cfg = {"sqlite_path": ":memory:", "chroma_path": "/tmp/x",
           "display_timezone": "UTC", "sources": {}}
    db = Database(":memory:")
    try:
        snap = _data.build_dashboard_snapshot(db, vs=None, config=cfg)
        assert len(snap["messages_per_day"]) == 90
    finally:
        db.close()


# ── _human_bytes ─────────────────────────────────────────────────────────────

def test_human_bytes_scales_units():
    assert data._human_bytes(0) == "0 B"
    assert data._human_bytes(500) == "500 B"
    assert data._human_bytes(2048) == "2.0 KB"
    assert data._human_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_human_age_returns_dash_for_none():
    assert data._human_age(None) == "-"


def test_human_age_minutes():
    now = datetime.now(timezone.utc)
    assert "m ago" in data._human_age(now - timedelta(minutes=5), now=now)


# ── _multi_row_bars (TASK-026 follow-up: taller histograms) ──────────────────

def test_multi_row_bars_empty_values_returns_blank_rows():
    rows = _multi_row_bars([], peak=10, height=5)
    assert len(rows) == 5
    assert all(r == "" for r in rows)


def test_multi_row_bars_zero_peak_returns_blank_rows():
    rows = _multi_row_bars([0, 0, 0], peak=0, height=5)
    assert len(rows) == 5
    assert all(r == "   " for r in rows)  # 3 columns of blanks


def test_multi_row_bars_peak_value_fills_top_row():
    rows = _multi_row_bars([10], peak=10, height=5)
    # Top row should be the full block, rest also full (it's at peak).
    assert len(rows) == 5
    assert all(r == "█" for r in rows)


def test_multi_row_bars_half_peak_fills_bottom_half():
    rows = _multi_row_bars([5], peak=10, height=4)
    # 4 rows; half-peak means ~2 rows of fill at the bottom.
    # Top → bottom; bottom-half = last two rows full.
    assert rows[0] == " "
    assert rows[1] == " "
    assert rows[2] == "█"
    assert rows[3] == "█"


def test_multi_row_bars_zero_value_is_blank_column_in_every_row():
    rows = _multi_row_bars([10, 0, 10], peak=10, height=3)
    for r in rows:
        # Three columns: full / blank / full
        assert r[0] == "█"
        assert r[1] == " "
        assert r[2] == "█"


def test_multi_row_bars_partial_uses_partial_block_chars():
    """A value that fills 1.5 rows should show a full block at the bottom and a
    partial-block at the row above."""
    # height=4, eighths_per_chart = 32. value=12, peak=24 → 16 eighths total.
    # That fills row1 (8 eighths) + 8 of row2's eighths (a full block).
    rows = _multi_row_bars([12], peak=24, height=4)
    # Top-down: row 0, 1, 2, 3. Rows 0+1 are empty, row 2 + 3 full.
    assert rows[0] == " "
    assert rows[1] == " "
    assert rows[2] == "█"
    assert rows[3] == "█"
    # A finer-grained example: value at 3/8 of one row.
    # peak=8, height=1: value=3 → 3 eighths → "▃"
    one_row = _multi_row_bars([3], peak=8, height=1)
    assert one_row == ["▃"]


def test_multi_row_bars_height_one_is_compatible_with_legacy_single_line():
    """Sanity: height=1 mirrors what the old _bar produced, character-for-character."""
    from cli.dashboard import _bar
    values = [0, 2, 5, 8]
    peak = 8
    row, = _multi_row_bars(values, peak=peak, height=1)
    legacy = "".join(_bar(v, peak) for v in values)
    assert row == legacy


def test_multi_row_bars_width_stays_stable_across_values():
    """Same number of columns regardless of which are zero — keeps charts aligned."""
    rows = _multi_row_bars([0, 1, 0, 5, 0], peak=5, height=3)
    for r in rows:
        assert len(r) == 5


# ── double-width bars + caret helpers (hour histogram tweaks) ────────────────

def test_multi_row_bars_col_width_two_doubles_each_cell():
    """col_width=2 renders each cell character twice — bars look rectangular
    instead of thin vertical lines."""
    rows = _multi_row_bars([0, 8, 0], peak=8, height=1, col_width=2)
    assert rows == ["  ██  "]


def test_multi_row_bars_col_width_preserves_total_width_on_empty():
    rows = _multi_row_bars([], peak=0, height=3, col_width=2)
    assert rows == ["", "", ""]


def test_hour_label_row_places_labels_every_third_hour():
    row = _hour_label_row(col_width=2)
    # "00" at cols 0-1; "03" at cols 6-7; "21" at cols 42-43.
    assert row[0:2] == "00"
    assert row[6:8] == "03"
    assert row[42:44] == "21"
    # Cols between labels should be spaces.
    assert row[2:6] == "    "


# ── --once smoke test (end-to-end render without raising) ────────────────────

def test_dashboard_once_renders_without_raising(tmp_path):
    """End-to-end: cli.dashboard.run(once=True) builds + renders a frame with
    no terminal interaction. Catches stupid mistakes in the rich Layout glue."""
    import yaml
    from unittest.mock import patch
    cfg = {
        "sqlite_path": str(tmp_path / "db.sqlite"),
        "chroma_path": str(tmp_path / "chroma"),
        "display_timezone": "UTC",
        "sources": {"mm": {"type": "mattermost", "url": "u", "token": "t",
                           "enabled": True}},
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg))

    # Seed a couple of rows so the panels aren't all empty.
    seed = Database(str(tmp_path / "db.sqlite"))
    seed.insert(_msg(id="mm:1", text="ping @me", mentions_me=1))
    seed.insert(_msg(id="mm:2"))
    seed.upsert_channel({"id": "ch1", "source": "mm", "name": "dev",
                         "display_name": "Dev", "last_update_at": 1})
    seed.log_tool_call(tool_name="search_messages", duration_ms=5, success=True)
    seed.close()

    from cli.dashboard import run as run_dashboard
    # The dashboard tries to open a chroma client; mock it out to avoid the
    # filesystem dance. Returning None makes storage_status report n/a — fine.
    with patch("cli.dashboard._open_vector_store_read_only", return_value=None):
        # If anything in the render pipeline raises, this propagates.
        run_dashboard(config_path=str(cfg_path), refresh=5, once=True, no_color=True)
