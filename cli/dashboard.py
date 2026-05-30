"""``memorandum dashboard`` — live terminal dashboard (TASK-026).

Reads everything from the local SQLite + Chroma + config; writes nothing.
The data layer lives in ``pipeline.dashboard`` (pure formatting, no rich
imports); this module is the rich Layout + Live refresh loop on top.

Run:
    memorandum dashboard                  # 5s refresh by default
    memorandum dashboard --refresh 10     # every 10s
    memorandum dashboard --once           # render one frame and exit
"""
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import load_config
from pipeline import dashboard as data
from storage.db import Database


# ── small utilities ──────────────────────────────────────────────────────────

_BAR_CHARS = " ▁▂▃▄▅▆▇█"
_BAR_HEIGHT = 6  # rows in messages_per_day + hour_of_day_histogram charts
_STATUS_STYLES = {
    "OK": "green",
    "PARTIAL": "yellow",
    "ERROR": "red bold",
    "STALE": "red bold",
    "NEVER_RUN": "dim",
}


def _bar(value: int, peak: int) -> str:
    """Map a value in [0, peak] onto one of the 8 Unicode block characters."""
    if peak <= 0 or value <= 0:
        return _BAR_CHARS[0]
    idx = min(len(_BAR_CHARS) - 1, max(1, round(value / peak * (len(_BAR_CHARS) - 1))))
    return _BAR_CHARS[idx]


def _multi_row_bars(
    values: list[int],
    peak: int,
    height: int = _BAR_HEIGHT,
    col_width: int = 1,
) -> list[str]:
    """Render `values` as a `height`-row bar chart using Unicode partial blocks.

    Each cell measures the value in eighths (0–8) of one row; the chart spans
    ``height * 8`` total eighths. Returns ``height`` strings, top row first
    (so callers can stack them line by line). With height=5 and 8 partial-block
    characters, max representable value gets 40 eighths.

    ``col_width`` repeats each cell character N times — pass 2 to render
    "rectangular" bars that look proportional rather than thin vertical lines
    (used by the 24-hour histogram so the chart fills its panel width).

    Edge cases: empty / zero peak returns `height` blank rows of the right
    width so the column count stays stable across snapshots.
    """
    height = max(1, height)
    col_width = max(1, col_width)
    if not values or peak <= 0:
        return [" " * (len(values) * col_width)] * height

    rows: list[str] = []
    for row in range(height, 0, -1):
        line = []
        for v in values:
            if v <= 0:
                line.append(" " * col_width)
                continue
            # Total eighths this value occupies across the full chart.
            total_eighths = round(v / peak * height * 8)
            # How many eighths land in THIS row (0-indexed from bottom).
            eighths_below = (row - 1) * 8
            cell_eighths = max(0, min(8, total_eighths - eighths_below))
            line.append(_BAR_CHARS[cell_eighths] * col_width)
        rows.append("".join(line))
    return rows


def _hour_label_row(col_width: int = 2) -> str:
    """Build the hours-of-day label row for the histogram.

    Labels every 3rd hour ("00 03 06 ... 21") placed at the LEFT char of
    that hour's column. Width matches `col_width * 24` so the labels stay
    aligned with the bars above. ``col_width=1`` reproduces the legacy
    spacing; ``col_width=2`` is what the wide layout uses.
    """
    total = 24 * col_width
    chars = [" "] * total
    for h in range(0, 24, 3):
        label = f"{h:02d}"
        pos = h * col_width
        for i, ch in enumerate(label):
            if pos + i < total:
                chars[pos + i] = ch
    return "".join(chars)


def _fmt_local(ts_iso: Optional[str], tz: ZoneInfo) -> str:
    if not ts_iso:
        return "-"
    try:
        dt = datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ts_iso[:16]


def _short_text(s: Optional[str], limit: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _uptime(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


# ── panel renderers (each returns a rich Renderable) ────────────────────────

def _panel_header(snap: dict, started: float, refresh: int, config_path: str, tz: ZoneInfo) -> Panel:
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    up = _uptime(time.monotonic() - started)
    line = Text()
    line.append("Memorandum dashboard", style="bold cyan")
    line.append(f"   {now}", style="white")
    line.append(f"   config={config_path}", style="dim")
    line.append(f"   refresh={refresh}s", style="dim")
    line.append(f"   uptime={up}", style="dim")
    return Panel(line, padding=(0, 1), border_style="cyan")


def _panel_storage(snap: dict) -> Panel:
    s = snap["storage"]
    t = Table.grid(padding=(0, 1))
    t.add_column(justify="right", style="dim")
    t.add_column()
    t.add_row("SQLite:", f"{s['sqlite_count']:,} msgs  ({s['sqlite_size']})")
    if s["chroma_count"] is None:
        t.add_row("Chroma:", Text(f"unavailable ({s['chroma_error'] or '-'})", style="red"))
    else:
        t.add_row("Chroma:", f"{s['chroma_count']:,} vecs  ({s['chroma_size']})")
    drift = s["drift"]
    if drift is None:
        drift_txt = Text("n/a", style="dim")
    elif drift == 0:
        drift_txt = Text("0 (aligned)", style="green")
    else:
        drift_txt = Text(f"{drift:+,}", style="red bold")
    t.add_row("Drift:", drift_txt)
    return Panel(t, title="Storage", border_style="cyan")


def _panel_ingest(snap: dict, tz: ZoneInfo) -> Panel:
    i = snap["ingest"]
    last = i.get("last_run") or {}
    badge = i.get("status_badge", "?")
    style = _STATUS_STYLES.get(badge, "white")
    t = Table.grid(padding=(0, 1))
    t.add_column(justify="right", style="dim")
    t.add_column()
    t.add_row("Status:", Text(badge, style=style))
    t.add_row("Started:",  _fmt_local(last.get("started_at"), tz))
    t.add_row("Finished:", _fmt_local(last.get("finished_at"), tz))
    t.add_row("Sources:",  str(last.get("sources_checked", 0)))
    t.add_row("Fetched:",  str(last.get("messages_fetched", 0)))
    t.add_row("New:",      str(last.get("messages_new", 0)))
    cadence = i.get("cadence_minutes")
    cadence_txt = f"~{cadence:.1f} min" if cadence else "-"
    t.add_row("Cadence:", cadence_txt)
    errors = last.get("errors") or []
    if errors:
        t.add_row("Errors:", Text(f"{len(errors)} (latest: {(errors[0].get('error') or '')[:60]})",
                                  style="red"))
    return Panel(t, title="Ingest", border_style="cyan")


def _panel_source_health(snap: dict, tz: ZoneInfo) -> Panel:
    t = Table(show_header=True, header_style="bold", expand=True, pad_edge=False)
    t.add_column("Source", overflow="ellipsis")
    t.add_column("Msgs", justify="right")
    t.add_column("Last", justify="right")
    # t.add_column("Oldest", justify="right")
    # t.add_column("Int/Tot", justify="right")
    for row in snap["source_health"]:
        age_style = "red" if row.get("stale") else "white"
        t.add_row(
            row["source"],
            f"{row['count']:,}" if row["count"] else Text("0", style="dim"),
            Text(row["last_age"], style=age_style),
            # (row.get("oldest_message") or "-")[:10],
            # f"{row.get('internal', 0)}/{row.get('total', 0)}",
        )
    return Panel(t, title="Source health", border_style="cyan")


def _table_top(rows: list[dict], key: str, count_key: str = "count") -> Table:
    t = Table(show_header=True, header_style="bold", expand=True, pad_edge=False)
    t.add_column("#", justify="right", style="dim")
    t.add_column(key.title(), overflow="ellipsis")
    t.add_column("Cnt", justify="right")
    for i, r in enumerate(rows, 1):
        t.add_row(str(i), str(r.get(key) or "-"), f"{r[count_key]:,}")
    if not rows:
        t.add_row("-", Text("(no activity)", style="dim"), "-")
    return t


def _panel_top_channels(snap: dict) -> Panel:
    return Panel(
        Group(
            Text("Last 24h", style="bold"),
            _table_top(snap["top_channels_d1"][:5], "channel"),
            Text(),
            Text("Last 7d", style="bold"),
            _table_top(snap["top_channels_d7"][:5], "channel"),
        ),
        title="Top channels", border_style="cyan",
    )


def _panel_top_senders(snap: dict) -> Panel:
    def _row_text(r):
        marker = "●" if r.get("internal") else "○"
        return f"{marker} {r.get('sender') or '-'}"
    t1 = Table(show_header=True, header_style="bold", expand=True, pad_edge=False)
    t1.add_column("#", justify="right", style="dim")
    t1.add_column("Sender")
    t1.add_column("Cnt", justify="right")
    for i, r in enumerate(snap["top_senders_d1"][:5], 1):
        t1.add_row(str(i), _row_text(r), f"{r['count']:,}")
    if not snap["top_senders_d1"]:
        t1.add_row("-", Text("(no activity)", style="dim"), "-")
    t7 = Table(show_header=True, header_style="bold", expand=True, pad_edge=False)
    t7.add_column("#", justify="right", style="dim")
    t7.add_column("Sender")
    t7.add_column("Cnt", justify="right")
    for i, r in enumerate(snap["top_senders_d7"][:5], 1):
        t7.add_row(str(i), _row_text(r), f"{r['count']:,}")
    if not snap["top_senders_d7"]:
        t7.add_row("-", Text("(no activity)", style="dim"), "-")
    return Panel(
        Group(Text("Last 24h", style="bold"), t1, Text(),
              Text("Last 7d", style="bold"), t7),
        title="Top senders", border_style="cyan",
    )


def _panel_messages_per_day(snap: dict) -> Panel:
    days = snap["messages_per_day"]
    peak = max((d["count"] for d in days), default=0)
    counts = [d["count"] for d in days]
    rows = _multi_row_bars(counts, peak, height=_BAR_HEIGHT)
    # Per-column weekend dimming: rebuild each row as a list of styled chars.
    rendered_rows = []
    for row_text in rows:
        segments = []
        for ch, d in zip(row_text, days):
            style = "dim" if d.get("weekday", 0) >= 5 else None
            segments.append(Text(ch, style=style) if style else Text(ch))
        rendered_rows.append(Text.assemble(*segments))

    label_lo = days[0]["date"][5:] if days else "-"
    label_hi = days[-1]["date"][5:] if days else "-"
    body = Group(
        *rendered_rows,
        Text(f"from {label_lo} to {label_hi} peak={peak:,} msgs/day", style="dim", justify="right"),
    )
    return Panel(body, title=f"Messages per day (last {len(days)})", border_style="cyan")


def _panel_hour_histogram(snap: dict, tz: ZoneInfo) -> Panel:
    """24-hour activity averaged across the last 14 days.

    Each hour is rendered as a 2-char-wide column so the chart fills the
    panel's width.
    """
    buckets = snap["hour_of_day_histogram"]
    peak = max(buckets) if buckets else 0
    rows = _multi_row_bars(buckets, peak, height=_BAR_HEIGHT, col_width=2)
    # One trailing row carries hour labels (dim) AND right-justifies the peak.
    label_row = Table.grid(expand=True, padding=(0, 0))
    label_row.add_column()
    label_row.add_column(justify="right")
    label_row.add_row(Text(_hour_label_row(col_width=2), style="dim"),
                      Text(f"peak={peak:,}", style="dim"))
    # Second trailing row: caret marking "now" + a brief legend on the right.
    caret_row = Table.grid(expand=True, padding=(0, 0))
    caret_row.add_column()
    caret_row.add_column(justify="right")
    body = Group(*[Text(r) for r in rows], label_row, caret_row)
    return Panel(body, title="24h activity (avg of last 14 days)", border_style="cyan")


def _panel_send(snap: dict) -> Panel:
    s = snap["send_stats"]
    t = Table.grid(padding=(0, 1))
    t.add_column(justify="right", style="dim")
    t.add_column()
    d1 = s["d1"]
    d7 = s["d7"]
    fail_24h = d1["failure"]
    fail_text = Text(str(fail_24h), style="red bold") if fail_24h else Text("0", style="green")
    t.add_row("24h:", f"{d1['total']} / {d1['success']} ok")
    t.add_row("err:",     fail_text)
    t.add_row("7d:",  f"{d7['total']} / {d7['success']} ok / {d7['failure']} errs")
    if d1.get("per_source"):
        t.add_row(Text("24h-src:", style="dim"))
        for ps in d1["per_source"][:4]:
            t.add_row("", f"{ps['source']}: {ps['total']} / {ps['failure']} errs")
    return Panel(t, title="Send activity", border_style="cyan")


def _panel_tool_usage(snap: dict) -> Panel:
    rows = snap["tool_call_stats"]["d1"]
    rows7 = {r["tool_name"]: r for r in snap["tool_call_stats"]["d7"]}
    t = Table(show_header=True, header_style="bold", expand=True, pad_edge=False)
    t.add_column("Tool")
    t.add_column("24h", justify="right")
    t.add_column("7d",  justify="right")
    t.add_column("avg ms", justify="right")
    t.add_column("err", justify="right")
    if not rows:
        t.add_row("(no calls)", "-", "-", "-", "-")
    for r in rows[:10]:
        err = r.get("failure") or 0
        err_txt = Text(str(err), style="red") if err else Text("0", style="dim")
        avg_ms = r.get("avg_ms")
        avg_txt = f"{int(avg_ms)}" if avg_ms else "-"
        d7 = rows7.get(r["tool_name"], {}).get("total", "-")
        t.add_row(r["tool_name"], str(r["total"]), str(d7), avg_txt, err_txt)
    return Panel(t, title="MCP tool usage (24h)", border_style="cyan")


def _panel_latest(snap: dict, tz: ZoneInfo) -> Panel:
    t = Table(show_header=True, header_style="bold", expand=True, pad_edge=False)
    t.add_column("Time",    width=5)
    t.add_column("Source",  overflow="ellipsis", width=12)
    t.add_column("Channel", overflow="ellipsis", width=24)
    t.add_column("Sender",  overflow="ellipsis", width=18)
    t.add_column("Text",    overflow="ellipsis")
    for r in snap["latest_messages"]:
        ts = _fmt_local(r.get("timestamp"), tz)[-5:]  # HH:MM
        style = "yellow bold" if r.get("mentions_me") else None
        sender = (r.get("canonical_sender") or r.get("sender") or "-")
        text = _short_text(r.get("text"), 200)
        row = [ts, r.get("source", "-")[:12], (r.get("channel") or "-")[:24],
               sender[:18], text]
        if style:
            t.add_row(*[Text(x, style=style) for x in row])
        else:
            t.add_row(*row)
    return Panel(t, title="Latest messages", border_style="cyan")


def _panel_mentions(snap: dict, tz: ZoneInfo) -> Panel:
    m = snap["mentions_me"]
    counts = m["counts"]
    head = Text.assemble(
        ("1h: ", "dim"), (f"{counts['h1']}  ", "bold"),
        ("24h: ", "dim"), (f"{counts['d1']}  ", "bold"),
        ("7d: ", "dim"), (f"{counts['d7']}", "bold"),
    )
    t = Table(show_header=True, header_style="bold", expand=True, pad_edge=False)
    t.add_column("Time", width=5)
    t.add_column("Channel", overflow="ellipsis", width=20)
    t.add_column("Sender", overflow="ellipsis", width=18)
    for r in m["recent"]:
        ts = _fmt_local(r.get("timestamp"), tz)[-5:]
        t.add_row(ts, (r.get("channel") or "-")[:20],
                  (r.get("sender") or "-")[:18])
    if not m["recent"]:
        t.add_row("-", "-", "-", Text("(no recent mentions)", style="dim"))
    return Panel(Group(head, Text(), t), title="Mentions of me", border_style="cyan")


# ── layout construction ─────────────────────────────────────────────────────

def _full_layout(snap: dict, started: float, refresh: int,
                 config_path: str, tz: ZoneInfo) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="row1",   size=11),
        Layout(name="row2",   size=12),
        Layout(name="row3",   size=9),
        Layout(name="latest"),
    )
    layout["header"].update(_panel_header(snap, started, refresh, config_path, tz))

    layout["row1"].split_row(
        Layout(_panel_storage(snap),     name="storage", ratio=1),
        Layout(_panel_ingest(snap, tz),  name="ingest",  ratio=1),
        Layout(_panel_send(snap),        name="send",    ratio=1),
        Layout(_panel_tool_usage(snap),  name="tools",   ratio=2),
    )
    layout["row2"].split_row(
        Layout(_panel_source_health(snap, tz), ratio=2),
        Layout(_panel_top_channels(snap),      ratio=2),
        Layout(_panel_top_senders(snap),       ratio=2),
        Layout(_panel_mentions(snap, tz),      ratio=3),
    )
    layout["row3"].split_row(
        # ratio 3:2 — messages_per_day gets ~108 cols (fits 90 days at
        # 1 char/day with slack); hour_histogram gets ~72 cols (fits 24
        # hours at 2 chars/hour with slack), roughly doubling its previous
        # width.
        Layout(_panel_messages_per_day(snap),  ratio=3),
        Layout(_panel_hour_histogram(snap, tz), ratio=2),
    )
    layout["latest"].update(_panel_latest(snap, tz))
    return layout


def _compact_layout(snap: dict, started: float, refresh: int,
                    config_path: str, tz: ZoneInfo) -> Group:
    """Vertical stack of the highest-signal panels for small terminals."""
    return Group(
        _panel_header(snap, started, refresh, config_path, tz),
        _panel_storage(snap),
        _panel_ingest(snap, tz),
        _panel_source_health(snap, tz),
        _panel_mentions(snap, tz),
        _panel_latest(snap, tz),
    )


def _render(snap: dict, started: float, refresh: int,
            config_path: str, tz: ZoneInfo, console: Console):
    """Pick full vs compact layout based on terminal size."""
    if console.size.width < 100 or console.size.height < 30:
        return _compact_layout(snap, started, refresh, config_path, tz)
    return _full_layout(snap, started, refresh, config_path, tz)


# ── entry point ─────────────────────────────────────────────────────────────

def run(
    config_path: str = "config.yaml",
    refresh: int = 5,
    once: bool = False,
    no_color: bool = False,
    mock: bool = False,
) -> None:
    """Launch the dashboard. Ctrl-C exits cleanly.

    ``mock=True`` skips the DB / Chroma / config dance entirely and renders a
    hard-coded demo snapshot — useful for screenshots and README assets when
    you don't want to share real account data."""
    if mock:
        tz = ZoneInfo("UTC")
        console = Console(no_color=no_color)
        snap = _build_mock_snapshot()
        started = time.monotonic()
        rendered = _render(snap, started, refresh, "(mock)", tz, console)
        if once:
            console.print(rendered)
            return
        # Live loop — refreshes the uptime/clock but the data stays static.
        with Live(rendered, console=console,
                  refresh_per_second=max(1, int(round(1 / refresh))) if refresh else 4,
                  screen=True, redirect_stderr=False, redirect_stdout=False) as live:
            try:
                while True:
                    time.sleep(refresh)
                    live.update(_render(_build_mock_snapshot(), started, refresh,
                                        "(mock)", tz, console))
            except KeyboardInterrupt:
                pass
        return

    config = load_config(config_path)
    tz_name = config.get("display_timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    db = Database(config["sqlite_path"], read_only=True)
    vs = _open_vector_store_read_only(config)
    console = Console(no_color=no_color)
    started = time.monotonic()

    def _snapshot():
        # Per-frame snapshot. Cheap (~ms scale on local SQLite).
        try:
            return data.build_dashboard_snapshot(db, vs, config)
        except Exception as e:
            # If the snapshot itself blows up, show a minimal error frame so
            # the operator sees a problem rather than a frozen screen.
            return _error_snapshot(e)

    try:
        if once:
            console.print(_render(_snapshot(), started, refresh, config_path, tz, console))
            return
        with Live(
            _render(_snapshot(), started, refresh, config_path, tz, console),
            console=console, refresh_per_second=max(1, int(round(1 / refresh))) if refresh else 4,
            screen=True, redirect_stderr=False, redirect_stdout=False,
        ) as live:
            while True:
                time.sleep(refresh)
                live.update(_render(_snapshot(), started, refresh, config_path, tz, console))
    except KeyboardInterrupt:
        # Quiet exit — no traceback.
        pass
    finally:
        try:
            db.close()
        except Exception:
            pass


def _open_vector_store_read_only(config: dict):
    """Try to attach to the existing chroma collection. None on failure — the
    dashboard renders the storage panel with `chroma_error` instead of crashing.
    """
    try:
        import chromadb
        path = Path(config["chroma_path"])
        if not path.exists():
            return None
        client = chromadb.PersistentClient(path=str(path))
        name = (config.get("embedding") or {}).get("collection_name", "messages")
        coll = client.get_or_create_collection(name=name, embedding_function=None)
        # Return a tiny adapter so dashboard.storage_status can call `.count()`.
        return _ChromaProbe(coll)
    except Exception:
        return None


class _ChromaProbe:
    """Minimal adapter exposing just the .count() the dashboard needs."""

    def __init__(self, collection):
        self._c = collection

    def count(self) -> int:
        return int(self._c.count())


def _error_snapshot(exc: Exception) -> dict:
    """Build a minimal snapshot that displays one big error panel — keeps the
    refresh loop alive when a panel-fetch fails."""
    msg = f"snapshot failed: {type(exc).__name__}: {exc}"
    empty_send = {"d1": {"total": 0, "success": 0, "failure": 0, "per_source": []},
                  "d7": {"total": 0, "success": 0, "failure": 0, "per_source": []}}
    return {
        "generated_at": "-",
        "tz": "UTC",
        "storage": {"sqlite_count": 0, "chroma_count": None, "chroma_error": msg,
                    "drift": None, "sqlite_bytes": 0, "sqlite_size": "-",
                    "chroma_bytes": 0, "chroma_size": "-"},
        "ingest": {"status_badge": "ERROR", "last_run": {"errors": [{"error": msg}]},
                   "cadence_minutes": None, "configured_sources": []},
        "source_health": [],
        "top_channels_d1": [], "top_channels_d7": [],
        "top_senders_d1":  [], "top_senders_d7":  [],
        "messages_per_day": [],
        "hour_of_day_histogram": [0] * 24,
        "send_stats": empty_send,
        "tool_call_stats": {"d1": [], "d7": []},
        "latest_messages": [],
        "mentions_me": {"counts": {"h1": 0, "d1": 0, "d7": 0}, "recent": []},
    }


# ── mock snapshot for screenshots ───────────────────────────────────────────

def _build_mock_snapshot() -> dict:
    """Hard-coded demo data — used by ``memorandum dashboard --mock``.

    Numbers are picked to show off every panel without being conspicuously
    fake: a realistic weekly rhythm on the 90-day chart (weekday peaks,
    quieter weekends), a workday-shaped 24h histogram, a healthy spread of
    sources, a few mentions, some send activity, busy MCP tool usage.
    """
    from datetime import datetime, timedelta, timezone

    # Fixed "now" so consecutive screenshot runs produce the same output.
    now = datetime(2026, 5, 30, 14, 23, tzinfo=timezone.utc)

    # 90 days of message counts: weekday baseline + weekend dip + recent spike.
    msgs_per_day = []
    last = 10
    for i in range(89, -1, -1):
        d = now.date() - timedelta(days=i)
        wd = d.weekday()
        base = 35 if wd < 5 else 12
        # A gentle recent uptick + Tuesday/Thursday peaks for visual variety.
        if i < 7:
            base = int(base * 1.6)
        if wd in (1, 3):
            base = int(base * 1.5)
        # Growth
        if wd < 5:
            if i <= 30:
                last -= 5
            if i >= 20:
                last += 5
            base += last
        msgs_per_day.append({"date": d.isoformat(), "count": base, "weekday": wd})

    # 24h activity averaged across "14 days" — workday peaks 09–11 and 14–17.
    hour_hist = [2, 1, 0, 0, 0, 0, 1, 4, 12, 38, 52, 47, 28, 35, 51, 49, 42, 31, 20, 14, 10, 7, 5, 3]

    return {
        "generated_at": now.isoformat(),
        "tz": "UTC",
        "storage": {
            "sqlite_count": 12_847,
            "chroma_count": 12_847,
            "chroma_error": None,
            "drift": 0,
            "sqlite_bytes": 41 * 1024 * 1024,
            "sqlite_size": "41.0 MB",
            "chroma_bytes": 187 * 1024 * 1024,
            "chroma_size": "187.0 MB",
        },
        "ingest": {
            "status_badge": "OK",
            "last_run": {
                "started_at":  (now - timedelta(minutes=3)).isoformat(),
                "finished_at": (now - timedelta(minutes=2, seconds=42)).isoformat(),
                "status": "ok",
                "sources_checked": 4,
                "messages_fetched": 47,
                "messages_new": 12,
                "errors": [],
            },
            "cadence_minutes": 15.0,
            "configured_sources": ["company_mattermost", "work_telegram",
                                   "work_pachca", "work_email"],
        },
        "source_health": [
            {"source": "company_mattermost", "count": 9_421,
             "last_message":   (now - timedelta(minutes=4)).isoformat(),
             "oldest_message": "2026-03-01T08:14:00+00:00",
             "last_age": "4m ago", "stale": False,
             "internal": 9_421, "total": 9_421},
            {"source": "work_telegram", "count": 1_206,
             "last_message":   (now - timedelta(minutes=18)).isoformat(),
             "oldest_message": "2026-03-04T11:02:00+00:00",
             "last_age": "18m ago", "stale": False,
             "internal": 408, "total": 1_206},
            {"source": "work_pachca", "count": 1_842,
             "last_message":   (now - timedelta(hours=2, minutes=10)).isoformat(),
             "oldest_message": "2026-03-02T07:55:00+00:00",
             "last_age": "2h ago", "stale": False,
             "internal": 1_402, "total": 1_842},
            {"source": "work_email", "count": 378,
             "last_message":   (now - timedelta(minutes=47)).isoformat(),
             "oldest_message": "2026-03-15T09:30:00+00:00",
             "last_age": "47m ago", "stale": False,
             "internal": 88, "total": 378},
        ],
        "top_channels_d1": [
            {"channel": "PM / Status",          "count": 38, "source": "company_mattermost"},
            {"channel": "Eng / Backend",        "count": 26, "source": "company_mattermost"},
            {"channel": "INBOX",                "count": 19, "source": "work_email"},
            {"channel": "Eng / Frontend",       "count": 17, "source": "company_mattermost"},
            {"channel": "Random",               "count": 11, "source": "work_pachca"},
        ],
        "top_channels_d7": [
            {"channel": "PM / Status",          "count": 264, "source": "company_mattermost"},
            {"channel": "Eng / Backend",        "count": 198, "source": "company_mattermost"},
            {"channel": "Eng / Frontend",       "count": 142, "source": "company_mattermost"},
            {"channel": "INBOX",                "count": 121, "source": "work_email"},
            {"channel": "Design / Reviews",     "count":  87, "source": "company_mattermost"},
        ],
        "top_senders_d1": [
            {"sender": "Jane Smith",   "source": "company_mattermost", "count": 24, "internal": 1},
            {"sender": "Bob Wilson",   "source": "work_email",         "count": 14, "internal": 0},
            {"sender": "John Doe",     "source": "company_mattermost", "count": 12, "internal": 1},
            {"sender": "Alex Petrov",  "source": "work_pachca",        "count":  9, "internal": 1},
            {"sender": "Carol Reyes",  "source": "company_mattermost", "count":  7, "internal": 1},
        ],
        "top_senders_d7": [
            {"sender": "Jane Smith",   "source": "company_mattermost", "count": 187, "internal": 1},
            {"sender": "John Doe",     "source": "company_mattermost", "count": 142, "internal": 1},
            {"sender": "Bob Wilson",   "source": "work_email",         "count":  98, "internal": 0},
            {"sender": "Alex Petrov",  "source": "work_pachca",        "count":  76, "internal": 1},
            {"sender": "Carol Reyes",  "source": "company_mattermost", "count":  54, "internal": 1},
        ],
        "messages_per_day": msgs_per_day,
        "hour_of_day_histogram": hour_hist,
        "send_stats": {
            "d1": {"total": 4, "success": 4, "failure": 0,
                   "per_source": [
                       {"source": "company_mattermost", "total": 3, "success": 3, "failure": 0},
                       {"source": "work_email",         "total": 1, "success": 1, "failure": 0},
                   ]},
            "d7": {"total": 31, "success": 30, "failure": 1,
                   "per_source": [
                       {"source": "company_mattermost", "total": 22, "success": 22, "failure": 0},
                       {"source": "work_email",         "total":  7, "success":  6, "failure": 1},
                       {"source": "work_pachca",        "total":  2, "success":  2, "failure": 0},
                   ]},
        },
        "tool_call_stats": {
            "d1": [
                {"tool_name": "search_messages",   "total": 42, "success": 42, "failure": 0, "avg_ms": 187},
                {"tool_name": "get_new_messages",  "total": 28, "success": 28, "failure": 0, "avg_ms":  94},
                {"tool_name": "get_thread",        "total": 15, "success": 15, "failure": 0, "avg_ms":  12},
                {"tool_name": "summarize_messages", "total":  9, "success":  9, "failure": 0, "avg_ms": 412},
                {"tool_name": "who_mentioned",     "total":  6, "success":  6, "failure": 0, "avg_ms":   8},
                {"tool_name": "send_message",      "total":  4, "success":  4, "failure": 0, "avg_ms": 220},
                {"tool_name": "list_channels",     "total":  3, "success":  3, "failure": 0, "avg_ms":  18},
                {"tool_name": "get_health",        "total":  2, "success":  2, "failure": 0, "avg_ms":   4},
            ],
            "d7": [
                {"tool_name": "search_messages",   "total": 318, "success": 316, "failure": 2, "avg_ms": 192},
                {"tool_name": "get_new_messages",  "total": 201, "success": 201, "failure": 0, "avg_ms":  88},
                {"tool_name": "get_thread",        "total":  94, "success":  94, "failure": 0, "avg_ms":  11},
                {"tool_name": "summarize_messages", "total":  47, "success":  46, "failure": 1, "avg_ms": 405},
                {"tool_name": "who_mentioned",     "total":  38, "success":  38, "failure": 0, "avg_ms":   9},
                {"tool_name": "send_message",      "total":  31, "success":  30, "failure": 1, "avg_ms": 234},
                {"tool_name": "list_channels",     "total":  18, "success":  18, "failure": 0, "avg_ms":  19},
                {"tool_name": "get_health",        "total":  12, "success":  12, "failure": 0, "avg_ms":   4},
            ],
        },
        "latest_messages": [
            {"id": "company_mattermost:1", "source": "company_mattermost",
             "channel": "PM / Status", "sender": "Jane Smith", "canonical_sender": "Jane Smith",
             "timestamp": (now - timedelta(minutes=4)).isoformat(),
             "text": "Backend deploy went out clean, monitoring looks good. "
                     "Closing PROJ-1248 — thanks all.",
             "mentions_me": 0, "internal": 1},
            {"id": "work_email:1", "source": "work_email",
             "channel": "INBOX", "sender": "Bob Wilson", "canonical_sender": "Bob Wilson",
             "timestamp": (now - timedelta(minutes=11)).isoformat(),
             "text": "Subject: Re: Q2 forecast review — looks good, scheduling Thursday 10am",
             "mentions_me": 1, "internal": 0},
            {"id": "company_mattermost:2", "source": "company_mattermost",
             "channel": "Eng / Backend", "sender": "John Doe", "canonical_sender": "John Doe",
             "timestamp": (now - timedelta(minutes=18)).isoformat(),
             "text": "Migration script needs another look — Alex spotted an edge case "
                     "with empty result sets",
             "mentions_me": 0, "internal": 1},
            {"id": "work_pachca:1", "source": "work_pachca",
             "channel": "Random", "sender": "Carol Reyes", "canonical_sender": "Carol Reyes",
             "timestamp": (now - timedelta(minutes=26)).isoformat(),
             "text": "anyone else seeing flaky CI on the integration job? happens about "
                     "1 in 5 runs",
             "mentions_me": 0, "internal": 1},
            {"id": "company_mattermost:3", "source": "company_mattermost",
             "channel": "Design / Reviews", "sender": "Alex Petrov", "canonical_sender": "Alex Petrov",
             "timestamp": (now - timedelta(minutes=35)).isoformat(),
             "text": "Updated mocks in Figma — addressed Jane's feedback on the empty state",
             "mentions_me": 1, "internal": 1},
            {"id": "company_mattermost:4", "source": "company_mattermost",
             "channel": "Eng / Frontend", "sender": "Jane Smith", "canonical_sender": "Jane Smith",
             "timestamp": (now - timedelta(minutes=42)).isoformat(),
             "text": "@John Doe could you take the on-call swap Friday? I'll cover yours next "
                     "week",
             "mentions_me": 0, "internal": 1},
            {"id": "work_email:2", "source": "work_email",
             "channel": "INBOX", "sender": "Diana Chen", "canonical_sender": "Diana Chen",
             "timestamp": (now - timedelta(minutes=58)).isoformat(),
             "text": "Subject: Contract amendment — please review the attached redlines",
             "mentions_me": 0, "internal": 0},
            {"id": "company_mattermost:5", "source": "company_mattermost",
             "channel": "PM / Status", "sender": "Bob Wilson", "canonical_sender": "Bob Wilson",
             "timestamp": (now - timedelta(hours=1, minutes=14)).isoformat(),
             "text": "Sprint review notes posted in the wiki — actions assigned, due dates "
                     "in the tracker",
             "mentions_me": 0, "internal": 0},
        ],
        "mentions_me": {
            "counts": {"h1": 2, "d1": 7, "d7": 34},
            "recent": [
                {"id": "work_email:1", "source": "work_email", "channel": "INBOX",
                 "sender": "Bob Wilson",
                 "timestamp": (now - timedelta(minutes=11)).isoformat(),
                 "text": "Subject: Re: Q2 forecast review — looks good, scheduling Thursday 10am"},
                {"id": "company_mattermost:3", "source": "company_mattermost",
                 "channel": "Design / Reviews", "sender": "Alex Petrov",
                 "timestamp": (now - timedelta(minutes=35)).isoformat(),
                 "text": "Updated mocks in Figma — addressed Jane's feedback on the empty state"},
                {"id": "company_mattermost:6", "source": "company_mattermost",
                 "channel": "PM / Status", "sender": "Jane Smith",
                 "timestamp": (now - timedelta(hours=2, minutes=8)).isoformat(),
                 "text": "Heads-up @me — the auth deprecation notice needs a sign-off by EOD"},
            ],
        },
    }
