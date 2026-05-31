"""CLI entry point for ``memorandum prune``.

Thin wrapper around ``pipeline.housekeeping.run_housekeeping`` — defaults to
dry-run so the operator can review counts before committing. ``--commit`` is
the explicit "actually delete" flag.

Exit codes:
  0  ok / dry_run / disabled / throttled — nothing went wrong
  1  partial — SQL committed but vector store or file cache had degraded
     failures (next prune retries; not fatal)
  2  hard error — SQL prune failed; nothing was changed
"""
import json
import sys

from config import load_config, get_retention_settings


def run(
    config_path: str = "config.yaml",
    dry_run: bool = True,
    days_override: int = None,
    as_json: bool = False,
) -> None:
    """Run housekeeping and print the report; sys.exit with the right code."""
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"config not found: {config_path}", file=sys.stderr)
        sys.exit(2)

    ret = get_retention_settings(config)
    if days_override is not None:
        ret["retention_days"] = max(1, int(days_override))

    # Lazy imports so `memorandum --help` doesn't pay for the heavy deps.
    from storage.db import Database
    from storage.vector_store import VectorStore
    from pipeline.housekeeping import run_housekeeping

    db = Database(config["sqlite_path"])
    vs = None
    try:
        # Vector store can be None — housekeeping handles that gracefully — but
        # in real runs we want chroma deletions to happen too.
        try:
            vs = VectorStore(config["chroma_path"], embedding=config.get("embedding"))
        except Exception as e:
            print(f"vector store unavailable, skipping vector cleanup: {e}", file=sys.stderr)
            vs = None

        report = run_housekeeping(
            db, vs,
            attachments_path=config.get("attachments_path", "data/attachments"),
            retention_days=ret["retention_days"],
            prune_interval_hours=ret["prune_interval_hours"],
            file_cache_grace_seconds=int(ret["file_cache_grace_minutes"]) * 60,
            dry_run=dry_run,
        )
    finally:
        db.close()

    if as_json:
        print(json.dumps(report, default=str, indent=2))
    else:
        print(_format_report(report, dry_run))
    sys.exit(_exit_code(report))


def _format_report(r: dict, dry_run: bool) -> str:
    title = "DRY RUN — no changes made" if dry_run else "PRUNE COMPLETE"
    status = r.get("status", "?")
    lines = [
        f"# {title}  (status={status})",
    ]
    if status == "disabled":
        lines.append("Retention is disabled (retention_days=0/null). Set a positive "
                     "value in `retention:` to enable.")
        return "\n".join(lines)
    if status == "throttled":
        lines.append(f"Skipped — last prune was at {r.get('last_prune_at')!r} "
                     f"(within prune_interval_hours).")
        return "\n".join(lines)

    cutoff = r.get("cutoff_ts") or "?"
    lines.append(f"  cutoff (UTC):        {cutoff}")
    lines.append(f"  messages deleted:    {r.get('messages_deleted', 0)}")
    lines.append(f"  mentions deleted:    {r.get('mentions_deleted', 0)}")
    lines.append(f"  vectors deleted:     {r.get('vectors_deleted', 0)}")
    lines.append(f"  sent_messages:       {r.get('sent_deleted', 0)}")
    lines.append(f"  ingest_runs deleted: {r.get('runs_deleted', 0)}")
    files = r.get("files_deleted", 0)
    freed = r.get("files_freed_bytes")
    retention_files = r.get("files_with_deleted_messages", 0)
    orphan_files = r.get("files_orphans_swept", 0)
    if freed is None:
        lines.append(f"  cache files cleared: {files} "
                     f"(retention:{retention_files} + orphans:{orphan_files})")
    else:
        lines.append(f"  cache files cleared: {files} "
                     f"(retention:{retention_files} + orphans:{orphan_files}; "
                     f"{_human(freed)} freed)")
    if r.get("error"):
        lines.append("")
        lines.append(f"  errors: {r['error']}")
    if dry_run and status == "dry_run":
        lines.append("")
        lines.append("Re-run with --commit to actually delete.")
    return "\n".join(lines)


def _human(n: int) -> str:
    if n is None:
        return "?"
    n = int(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _exit_code(report: dict) -> int:
    status = report.get("status")
    if status == "partial":
        return 1
    if status == "error":
        return 2
    return 0
