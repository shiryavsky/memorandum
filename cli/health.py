"""CLI entry point for ``memorandum health``.

Thin wrapper — the report-building and formatting live in ``pipeline.health``
(shared with the MCP server's ``get_health`` tool). This module is just the
command-line shell.
"""
import json
import sys

from config import load_config
from storage.db import Database
from pipeline.health import build_health_report, format_health_text, _exit_code


def run(config_path: str = "config.yaml", as_json: bool = False) -> None:
    """Print the health report and exit (0=ok, 1=partial/error, 2=no run yet)."""
    config = load_config(config_path)
    db = Database(config["sqlite_path"])
    try:
        report = build_health_report(db, config)
    finally:
        db.close()

    if as_json:
        print(json.dumps(report, default=str, indent=2))
    else:
        print(format_health_text(report, config))

    sys.exit(_exit_code(report.get("last_run")))
