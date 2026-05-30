"""Argparse dispatcher for ``python -m cli`` / ``memorandum``.

Verbs:
  memorandum health [--config FILE] [--json]
  memorandum aliases refresh [--config FILE] [--in-place] [--json]
  memorandum prune [--dry-run | --commit] [--days N] [--config FILE] [--json]
  memorandum dashboard [--refresh N] [--config FILE] [--once] [--no-color]
"""
import argparse
import sys
from pathlib import Path

# Make the package importable when invoked as `python -m cli` from the repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="memorandum",
        description="Memorandum CLI utilities (read-only and curation tools).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    p_health = sub.add_parser("health", help="Show ingest health status")
    p_health.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p_health.add_argument("--json", action="store_true", help="Emit the report as JSON")
    p_health.set_defaults(func=_run_health)

    p_aliases = sub.add_parser("aliases", help="user_aliases curation")
    sub_aliases = p_aliases.add_subparsers(dest="aliases_cmd", required=True, metavar="ACTION")
    p_refresh = sub_aliases.add_parser(
        "refresh",
        help="Emit stub user_aliases entries for senders not yet covered (append-only).",
    )
    p_refresh.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p_refresh.add_argument("--in-place", action="store_true",
                           help="Append stubs to config.yaml (preserves comments via ruamel.yaml)")
    p_refresh.add_argument("--json", action="store_true",
                           help="Emit the candidate list as JSON instead of YAML stubs")
    p_refresh.set_defaults(func=_run_aliases_refresh)

    p_prune = sub.add_parser(
        "prune",
        help="Delete data older than the configured retention horizon "
             "(dry-run by default).",
    )
    p_prune.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    mode = p_prune.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="Report what would be deleted without touching anything (default).")
    mode.add_argument("--commit", action="store_true",
                      help="Actually delete. Without this, runs in dry-run mode.")
    p_prune.add_argument("--days", type=int, default=None,
                         help="Override retention_days for this run (does not modify config).")
    p_prune.add_argument("--json", action="store_true",
                         help="Emit the report as JSON instead of human text.")
    p_prune.set_defaults(func=_run_prune)

    p_dash = sub.add_parser(
        "dashboard",
        help="Live terminal dashboard — refreshing storage / ingest / mentions / "
             "send / tool-usage panels. Ctrl-C exits.",
    )
    p_dash.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p_dash.add_argument("--refresh", type=int, default=5,
                        help="Refresh interval in seconds (default: 5).")
    p_dash.add_argument("--once", action="store_true",
                        help="Render one frame to stdout and exit (useful for `watch` / snapshots).")
    p_dash.add_argument("--no-color", action="store_true",
                        help="Disable ANSI color (e.g. when piping to a file).")
    p_dash.add_argument("--mock", action="store_true",
                        help="Render with hard-coded demo data (no DB / chroma / "
                             "config needed). Use for screenshots and README assets.")
    p_dash.set_defaults(func=_run_dashboard)

    parsed = parser.parse_args(argv)
    parsed.func(parsed)


def _run_health(args):
    from cli.health import run
    run(config_path=args.config, as_json=args.json)


def _run_aliases_refresh(args):
    from cli.aliases import refresh
    refresh(config_path=args.config, in_place=args.in_place, as_json=args.json)


def _run_prune(args):
    from cli.prune import run
    # --commit overrides the default --dry-run flag.
    dry_run = not args.commit
    run(config_path=args.config, dry_run=dry_run,
        days_override=args.days, as_json=args.json)


def _run_dashboard(args):
    from cli.dashboard import run
    run(config_path=args.config, refresh=args.refresh,
        once=args.once, no_color=args.no_color, mock=args.mock)


if __name__ == "__main__":
    main()
