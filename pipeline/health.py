"""Health/status reporting for the ingest pipeline.

Shared by the CLI (python -m pipeline health) and the get_health MCP tool.
No MCP or connector imports allowed here.
"""
import json
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config import load_config
from storage.db import Database


def build_health_report(db: Database, config: dict) -> dict:
    last_run = db.get_last_ingest_run()
    source_health = {s["source"]: s for s in db.get_source_health()}
    configured_sources = list(config.get("sources", {}).keys())
    return {
        "last_run": last_run,
        "source_health": source_health,
        "configured_sources": configured_sources,
    }


def format_health_text(report: dict, config: dict = None) -> str:
    tz = _get_tz(config)
    parts = ["# Memorandum Health\n"]

    last_run = report.get("last_run")
    if last_run:
        status = last_run.get("status", "?")
        status_label = {"ok": "[OK]", "partial": "[PARTIAL]", "error": "[ERROR]"}.get(status, f"[{status}]")
        parts.append("## Last Ingest Run")
        parts.append(f"- Status:          {status_label}")
        parts.append(f"- Started:         {_fmt_ts(last_run.get('started_at', ''), tz)}")
        parts.append(f"- Finished:        {_fmt_ts(last_run.get('finished_at', ''), tz)}")
        parts.append(f"- Sources checked: {last_run.get('sources_checked', 0)}")
        parts.append(f"- Messages fetched:{last_run.get('messages_fetched', 0)}")
        parts.append(f"- Messages new:    {last_run.get('messages_new', 0)}")
        errors = last_run.get("errors", [])
        if errors:
            parts.append(f"- Errors ({len(errors)}):")
            for e in errors:
                parts.append(f"  - [{e.get('source', '?')}] {e.get('error', '?')}")
    else:
        parts.append("## Last Ingest Run\n- No ingest run recorded yet.")

    parts.append("")
    parts.append("## Source Health")
    source_health = report.get("source_health", {})
    configured = report.get("configured_sources", [])
    if not configured:
        parts.append("- No sources configured.")
    for src in configured:
        sh = source_health.get(src)
        if sh:
            oldest = _fmt_ts(sh.get("oldest_message", ""), tz)
            last = _fmt_ts(sh.get("last_message", ""), tz)
            count = sh.get("count", 0)
            parts.append(f"- {src}: {count} msgs | oldest: {oldest} | last: {last}")
        else:
            parts.append(f"- {src}: no messages stored")

    return "\n".join(parts)


def _get_tz(config: dict = None) -> ZoneInfo:
    if not config:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(config.get("display_timezone", "UTC"))
    except Exception:
        return ZoneInfo("UTC")


def _fmt_ts(ts: str, tz: ZoneInfo) -> str:
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ts[:16]


def _exit_code(last_run: dict | None) -> int:
    if last_run is None:
        return 2
    return 0 if last_run.get("status") == "ok" else 1


def main(args=None):
    """CLI: python -m pipeline health [--config PATH] [--json]

    Exit codes:
      0  last ingest run succeeded (status=ok)
      1  last run had errors (status=partial or error)
      2  no ingest run recorded yet
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Show Memorandum ingest health status",
        epilog="Exit codes: 0=ok, 1=partial/error, 2=no run recorded"
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--json", action="store_true", help="Output raw report as JSON")
    parsed = parser.parse_args(args)

    config = load_config(parsed.config)
    db = Database(config["sqlite_path"])
    report = build_health_report(db, config)
    db.close()

    if parsed.json:
        print(json.dumps(report, default=str, indent=2))
    else:
        print(format_health_text(report, config))

    sys.exit(_exit_code(report.get("last_run")))
