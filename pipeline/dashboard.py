"""Dashboard data layer (TASK-026).

Pure data-fetch + format. ZERO ``rich`` / terminal imports — so the same
functions can drive the TUI today and (if we ever want it) a ``dashboard_json``
MCP tool later, without duplication.

One function per panel. Each returns a shape-stable dict / list so the renderer
in ``cli/dashboard.py`` can be a thin map-to-rich-Table layer.

Same architectural pattern as ``pipeline.health`` and ``pipeline.housekeeping``:
no MCP / no connector imports.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Optional

from pipeline.health import build_health_report


# ── helpers ──────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_n_ago(**kwargs) -> str:
    return (_now_utc() - timedelta(**kwargs)).isoformat()


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _human_age(then: Optional[datetime], now: Optional[datetime] = None) -> str:
    """Render '3m ago', '2h ago', '5d ago'. None → '-'."""
    if then is None:
        return "-"
    now = now or _now_utc()
    delta = now - then
    sec = int(delta.total_seconds())
    if sec < 0:
        return "future"
    if sec < 60:
        return f"{sec}s ago"
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec // 3600}h ago"
    return f"{sec // 86400}d ago"


def _dir_size_bytes(path: Path) -> int:
    """Sum of all file sizes under `path`, recursively. 0 if path missing."""
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _human_bytes(n: int) -> str:
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ── panels ───────────────────────────────────────────────────────────────────

def storage_status(db, vs, config: dict) -> dict:
    """SQLite + Chroma counts, drift, on-disk sizes."""
    sql_path = Path(config.get("sqlite_path", "data/messages.db"))
    chroma_path = Path(config.get("chroma_path", "data/chroma"))
    sqlite_count = db.conn.execute(
        "SELECT COUNT(*) c FROM messages"
    ).fetchone()["c"]
    chroma_count: Optional[int] = None
    chroma_err: Optional[str] = None
    if vs is not None:
        try:
            chroma_count = vs.count()
        except Exception as e:
            chroma_err = str(e)[:200]
    sql_size = sql_path.stat().st_size if sql_path.exists() else 0
    chroma_size = _dir_size_bytes(chroma_path)
    drift = (chroma_count - sqlite_count) if chroma_count is not None else None
    return {
        "sqlite_count": sqlite_count,
        "chroma_count": chroma_count,
        "chroma_error": chroma_err,
        "drift": drift,
        "sqlite_bytes": sql_size,
        "sqlite_size": _human_bytes(sql_size),
        "chroma_bytes": chroma_size,
        "chroma_size": _human_bytes(chroma_size),
    }


def ingest_status(db, config: dict) -> dict:
    """Reuses pipeline.health.build_health_report and adds run cadence.

    Cadence = median minutes between the last 20 ingest_runs.started_at values.
    Used to compute STALE (ingest hasn't run in > 2× the typical interval)."""
    report = build_health_report(db, config)
    last_run = report.get("last_run") or {}
    runs = db.recent_ingest_runs(limit=20)
    cadence_minutes: Optional[float] = None
    if len(runs) >= 2:
        starts = []
        for r in runs:
            dt = _parse_iso(r.get("started_at") or "")
            if dt is not None:
                starts.append(dt)
        starts.sort()
        gaps = [
            (b - a).total_seconds() / 60.0
            for a, b in zip(starts, starts[1:])
            if (b - a).total_seconds() > 0
        ]
        if gaps:
            cadence_minutes = median(gaps)

    status_badge = "NEVER_RUN"
    if last_run:
        status_badge = (last_run.get("status") or "?").upper()
        finished = _parse_iso(last_run.get("finished_at") or "")
        if finished is not None and cadence_minutes:
            staleness_minutes = (_now_utc() - finished).total_seconds() / 60.0
            if staleness_minutes > max(cadence_minutes * 2.5, 30):
                status_badge = "STALE"

    return {
        "status_badge": status_badge,
        "last_run": last_run,
        "cadence_minutes": cadence_minutes,
        "configured_sources": report.get("configured_sources", []),
    }


def source_health(db, config: dict) -> list[dict]:
    """One row per configured source — count, last/oldest message ages,
    internal/external split."""
    report = build_health_report(db, config)
    sh = report.get("source_health", {})
    out: list[dict] = []
    now = _now_utc()
    # Per-source internal/external counts.
    int_split = {
        r["source"]: r for r in db.conn.execute("""
            SELECT source,
                   SUM(internal)         AS internal,
                   COUNT(*)              AS total
            FROM messages GROUP BY source
        """).fetchall()
    }
    for src in report.get("configured_sources", []):
        info = sh.get(src) or {}
        last_dt = _parse_iso(info.get("last_message") or "")
        out.append({
            "source":         src,
            "count":          info.get("count", 0),
            "last_message":   info.get("last_message"),
            "oldest_message": info.get("oldest_message"),
            "last_age":       _human_age(last_dt, now),
            "stale":          last_dt is not None and (now - last_dt) > timedelta(minutes=30),
            "internal":       (int_split.get(src) or {})["internal"] if src in int_split else 0,
            "total":          (int_split.get(src) or {})["total"] if src in int_split else 0,
        })
    return out


def top_channels(db, since_iso: str, limit: int = 10) -> list[dict]:
    """Most-active channels in the window."""
    return db.top_channels_since(since_iso, limit=limit)


def top_senders(db, since_iso: str, limit: int = 10) -> list[dict]:
    """Most-active senders in the window. Uses canonical_sender so aliases collapse."""
    return db.top_senders_since(since_iso, limit=limit)


def messages_per_day(db, days: int = 30) -> list[dict]:
    """Daily counts for the trailing N days, gaps zero-filled, oldest → newest."""
    raw = {r["date"]: r["count"] for r in db.messages_per_day(days=days)}
    now = _now_utc().date()
    out: list[dict] = []
    for i in range(days - 1, -1, -1):
        d = (now - timedelta(days=i))
        key = d.isoformat()
        out.append({
            "date":    key,
            "count":   raw.get(key, 0),
            "weekday": d.weekday(),   # 0=Mon, 6=Sun
        })
    return out


def hour_of_day_histogram(db, days: int = 14, tz_name: str = "UTC") -> list[int]:
    """24-entry list (hour → count) averaged over the trailing N days, local tz."""
    return db.hour_of_day_histogram(days=days, tz_name=tz_name)


def send_stats(db) -> dict:
    """Send activity for the last 24h + 7d windows."""
    return {
        "d1": db.send_stats_since(_iso_n_ago(days=1)),
        "d7": db.send_stats_since(_iso_n_ago(days=7)),
    }


def tool_call_stats(db) -> dict:
    """Per-tool aggregates for 24h + 7d windows."""
    return {
        "d1": db.tool_call_stats_since(_iso_n_ago(days=1)),
        "d7": db.tool_call_stats_since(_iso_n_ago(days=7)),
    }


def latest_messages(db, limit: int = 15) -> list[dict]:
    """Most recent messages across all sources."""
    return db.latest_messages(limit=limit)


def mentions_me(db, recent_limit: int = 3) -> dict:
    """1h / 24h / 7d counts + the most recent N mention rows."""
    return {
        "counts": db.mentions_me_counts(),
        "recent": db.recent_mentions_me(limit=recent_limit),
    }


# ── one-call entry point ─────────────────────────────────────────────────────

def build_dashboard_snapshot(db, vs, config: dict) -> dict:
    """Collect every panel into one dict — used by `--once` and (eventually) by
    a future `dashboard_json` MCP tool. The TUI renderer calls each panel
    helper individually so partial failure doesn't blank the whole screen."""
    tz_name = config.get("display_timezone", "UTC")
    return {
        "generated_at":  _now_utc().isoformat(),
        "tz":            tz_name,
        "storage":       storage_status(db, vs, config),
        "ingest":        ingest_status(db, config),
        "source_health": source_health(db, config),
        "top_channels_d1": top_channels(db, _iso_n_ago(days=1)),
        "top_channels_d7": top_channels(db, _iso_n_ago(days=7)),
        "top_senders_d1":  top_senders(db,  _iso_n_ago(days=1)),
        "top_senders_d7":  top_senders(db,  _iso_n_ago(days=7)),
        "messages_per_day":     messages_per_day(db, days=90),
        "hour_of_day_histogram": hour_of_day_histogram(db, days=14, tz_name=tz_name),
        "send_stats":      send_stats(db),
        "tool_call_stats": tool_call_stats(db),
        "latest_messages": latest_messages(db, limit=15),
        "mentions_me":     mentions_me(db),
    }
