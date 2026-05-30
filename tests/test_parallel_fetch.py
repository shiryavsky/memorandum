"""Tests for pipeline.ingest parallel-fetch helpers (TASK-027).

Covers the small pure helpers (_resolve_worker_count, _fetch_one, _fetch_all)
in isolation. End-to-end coverage of the run_ingest integration lives in
test_ingest.py.
"""
import time
import threading
from unittest.mock import MagicMock, patch

from pipeline.ingest import (
    _resolve_worker_count, _fetch_one, _fetch_all, _log_fetch_summary,
)


# ── _resolve_worker_count ────────────────────────────────────────────────────

def test_resolve_workers_auto_uses_num_sources():
    n = _resolve_worker_count(3, {"fetch_workers": None, "max_fetch_workers": 8})
    assert n == 3


def test_resolve_workers_auto_capped_by_max():
    n = _resolve_worker_count(30, {"fetch_workers": None, "max_fetch_workers": 8})
    assert n == 8


def test_resolve_workers_explicit_used_as_is_when_below_num_sources():
    n = _resolve_worker_count(10, {"fetch_workers": 4, "max_fetch_workers": 8})
    assert n == 4


def test_resolve_workers_explicit_clamped_to_num_sources():
    """4 workers for 2 sources would idle; clamp to 2."""
    n = _resolve_worker_count(2, {"fetch_workers": 4, "max_fetch_workers": 8})
    assert n == 2


def test_resolve_workers_explicit_one_keeps_one():
    n = _resolve_worker_count(5, {"fetch_workers": 1, "max_fetch_workers": 8})
    assert n == 1


def test_resolve_workers_zero_sources_returns_one():
    """No sources → no work, but the sequential path expects worker_count >= 1."""
    n = _resolve_worker_count(0, {"fetch_workers": None, "max_fetch_workers": 8})
    assert n == 1


# ── _fetch_one ────────────────────────────────────────────────────────────────

def _fake_fe(kept):
    fe = MagicMock()
    fe.filter_messages.return_value = list(kept)
    return fe


def test_fetch_one_ok_aggregates_filter_counts():
    conn = MagicMock()
    conn.fetch_messages.return_value = {
        "messages": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "messages_count": 3,
        "channels_scanned": 1,
        "channels_skipped": 0,
    }
    fe = _fake_fe(kept=[{"id": "a"}, {"id": "b"}])  # filter dropped one
    out = _fetch_one("src", conn, fe, since=None, force=False, since_ms=0)
    assert out["status"] == "ok"
    assert out["source"] == "src"
    assert out["messages_count"] == 3
    assert out["channels_scanned"] == 1
    assert out["channels_skipped"] == 0
    assert out["dropped"] == 1
    assert [m["id"] for m in out["kept"]] == ["a", "b"]


def test_fetch_one_connector_exception_becomes_error_status():
    """A connector raising must NEVER raise out of _fetch_one — it returns an
    error dict the dispatcher then folds into source_errors."""
    conn = MagicMock()
    conn.fetch_messages.side_effect = RuntimeError("boom")
    out = _fetch_one("src", conn, _fake_fe([]), since=None, force=False, since_ms=0)
    assert out["status"] == "error"
    assert out["source"] == "src"
    assert "boom" in out["error"]


def test_fetch_one_error_message_truncated_to_300_chars():
    conn = MagicMock()
    conn.fetch_messages.side_effect = RuntimeError("x" * 1000)
    out = _fetch_one("src", conn, _fake_fe([]), since=None, force=False, since_ms=0)
    assert len(out["error"]) == 300


def test_fetch_one_ok_includes_fetch_ms_in_result():
    conn = MagicMock()
    conn.fetch_messages.return_value = {
        "messages": [], "messages_count": 0,
        "channels_scanned": 0, "channels_skipped": 0,
    }
    out = _fetch_one("src", conn, _fake_fe([]), since=None, force=False, since_ms=0)
    assert "fetch_ms" in out
    assert isinstance(out["fetch_ms"], int)
    assert out["fetch_ms"] >= 0


def test_fetch_one_error_also_includes_fetch_ms():
    """Knowing how long a failing source idled before erroring is useful too."""
    conn = MagicMock()
    conn.fetch_messages.side_effect = RuntimeError("boom")
    out = _fetch_one("src", conn, _fake_fe([]), since=None, force=False, since_ms=0)
    assert "fetch_ms" in out


def test_fetch_one_logs_timing_in_info_line(caplog):
    """The per-source INFO line must end with the elapsed time so operators can
    see which source is slow when they scan the log."""
    import logging as _logging
    caplog.set_level(_logging.INFO, logger="pipeline.ingest")
    conn = MagicMock()
    conn.fetch_messages.return_value = {
        "messages": [{"id": "a"}], "messages_count": 1,
        "channels_scanned": 1, "channels_skipped": 0,
    }
    _fetch_one("payments", conn, _fake_fe([{"id": "a"}]),
               since=None, force=False, since_ms=0)
    msgs = [r.getMessage() for r in caplog.records]
    timing_lines = [m for m in msgs if m.startswith("[payments]") and "ms)" in m]
    assert timing_lines, f"expected an INFO line with timing; got {msgs!r}"


# ── _fetch_all ────────────────────────────────────────────────────────────────

def _result_for(count: int):
    return {
        "messages": [{"id": f"m{i}"} for i in range(count)],
        "messages_count": count,
        "channels_scanned": 1,
        "channels_skipped": 0,
    }


def test_fetch_all_one_worker_path_does_not_use_executor():
    """fetch_workers=1 → strictly sequential. Patch ThreadPoolExecutor; if it
    gets called we know the legacy path isn't actually being taken."""
    conn = MagicMock()
    conn.fetch_messages.return_value = _result_for(2)
    connectors = [("s", conn, _fake_fe([{"id": "m0"}, {"id": "m1"}]))]
    with patch("pipeline.ingest.ThreadPoolExecutor") as Pool:
        out = _fetch_all(connectors, worker_count=1,
                         since=None, force=False, since_ms=0)
    Pool.assert_not_called()
    assert len(out) == 1
    assert out[0]["status"] == "ok"


def test_fetch_all_runs_sources_in_parallel():
    """Two sources, each sleeping 200ms. Wall-clock with 2 workers must be
    well below 400ms (the serial bound)."""
    barrier = threading.Barrier(2, timeout=2.0)

    def slow_fetch(*_a, **_kw):
        barrier.wait()           # prove both threads are alive at once
        time.sleep(0.05)
        return _result_for(1)

    conn1 = MagicMock()
    conn1.fetch_messages.side_effect = slow_fetch
    conn2 = MagicMock()
    conn2.fetch_messages.side_effect = slow_fetch
    connectors = [
        ("s1", conn1, _fake_fe([{"id": "x"}])),
        ("s2", conn2, _fake_fe([{"id": "y"}])),
    ]

    t0 = time.monotonic()
    out = _fetch_all(connectors, worker_count=2,
                     since=None, force=False, since_ms=0)
    elapsed = time.monotonic() - t0

    assert {r["source"] for r in out} == {"s1", "s2"}
    assert all(r["status"] == "ok" for r in out)
    # Serial would be ~100ms+barrier-timeout-on-deadlock. Parallel finishes
    # roughly at the per-source 50ms mark plus thread overhead.
    assert elapsed < 0.4


def test_fetch_all_isolation_one_source_raising_does_not_kill_others():
    bad = MagicMock()
    bad.fetch_messages.side_effect = RuntimeError("upstream 500")
    good = MagicMock()
    good.fetch_messages.return_value = _result_for(3)
    connectors = [
        ("bad", bad, _fake_fe([])),
        ("good", good, _fake_fe([{"id": "a"}, {"id": "b"}, {"id": "c"}])),
    ]
    out = _fetch_all(connectors, worker_count=2,
                     since=None, force=False, since_ms=0)
    by_source = {r["source"]: r for r in out}
    assert by_source["bad"]["status"] == "error"
    assert "upstream 500" in by_source["bad"]["error"]
    assert by_source["good"]["status"] == "ok"
    assert by_source["good"]["messages_count"] == 3


def test_fetch_all_preserves_input_order_across_results():
    """We aggregate stats by walking the returned list; consistent ordering keeps
    log output predictable. Futures resolve in completion order if you use
    as_completed, but our implementation walks futures in submission order."""
    conn_a = MagicMock()
    conn_a.fetch_messages.return_value = _result_for(1)
    conn_b = MagicMock()
    conn_b.fetch_messages.return_value = _result_for(1)
    conn_c = MagicMock()
    conn_c.fetch_messages.return_value = _result_for(1)
    connectors = [
        ("alpha", conn_a, _fake_fe([{"id": "1"}])),
        ("beta",  conn_b, _fake_fe([{"id": "2"}])),
        ("gamma", conn_c, _fake_fe([{"id": "3"}])),
    ]
    out = _fetch_all(connectors, worker_count=3,
                     since=None, force=False, since_ms=0)
    assert [r["source"] for r in out] == ["alpha", "beta", "gamma"]


def test_fetch_all_passes_since_and_force_through():
    """The since/force/since_ms args must reach the connector verbatim."""
    from datetime import datetime, timezone
    conn = MagicMock()
    conn.fetch_messages.return_value = _result_for(0)
    connectors = [("s", conn, _fake_fe([]))]
    since = datetime(2026, 5, 1, tzinfo=timezone.utc)
    _fetch_all(connectors, worker_count=1, since=since, force=True, since_ms=42)
    conn.fetch_messages.assert_called_once_with(since, force=True, default_since_ms=42)


# ── _log_fetch_summary ───────────────────────────────────────────────────────

def test_log_fetch_summary_orders_slowest_first(caplog):
    import logging as _logging
    caplog.set_level(_logging.INFO, logger="pipeline.ingest")
    results = [
        {"source": "fast",    "fetch_ms": 50,   "status": "ok"},
        {"source": "slowest", "fetch_ms": 8000, "status": "ok"},
        {"source": "medium",  "fetch_ms": 1200, "status": "ok"},
    ]
    _log_fetch_summary(results, wall_ms=8050)
    msgs = [r.getMessage() for r in caplog.records]
    line = next((m for m in msgs if m.startswith("Fetch summary:")), None)
    assert line is not None
    # Slowest source named first.
    assert line.index("slowest=") < line.index("medium=") < line.index("fast=")
    assert "wall=8050 ms" in line


def test_log_fetch_summary_handles_empty_results(caplog):
    import logging as _logging
    caplog.set_level(_logging.INFO, logger="pipeline.ingest")
    _log_fetch_summary([], wall_ms=0)
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("Fetch summary" in m for m in msgs)


def test_log_fetch_summary_includes_errored_sources(caplog):
    """A source that errored before completing still contributed wall time;
    show it in the summary so operators can spot the timeout outlier."""
    import logging as _logging
    caplog.set_level(_logging.INFO, logger="pipeline.ingest")
    results = [
        {"source": "ok_src", "fetch_ms": 100, "status": "ok"},
        {"source": "broken", "fetch_ms": 5000, "status": "error", "error": "boom"},
    ]
    _log_fetch_summary(results, wall_ms=5100)
    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith("Fetch summary:"))
    assert "broken=5000ms" in line
    assert "ok_src=100ms" in line
