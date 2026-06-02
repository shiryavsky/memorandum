"""CLI entry point for ``memorandum reindex-chroma``.

Wipes the configured chroma directory and rebuilds the vector store from
SQLite (the canonical message store). Acquires ``/tmp/memorandum-sync.lock``
— the same lock ``bin/memorandum-sync`` uses — so a sync run can't race the
rebuild and overwrite a half-empty collection.

Use cases:
  - Recover after a chroma corruption / dimension mismatch.
  - Backfill metadata after a schema fix (e.g. the channel-key fallback).
  - Switch to a new embedding model (combined with a fresh ``collection_name``
    is the safer route, but a hard rebuild works too).

Exit codes:
  0  ok
  2  another sync/reindex is already running, or config missing
  1  partial — some rows failed to embed
"""
import fcntl
import json
import shutil
import sys
import time
from pathlib import Path

from config import load_config


_LOCK_FILE = "/tmp/memorandum-sync.lock"


def run(config_path: str = "config.yaml", as_json: bool = False) -> None:
    """Acquire the sync lock, wipe chroma, and reinsert every SQLite row."""
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"config not found: {config_path}", file=sys.stderr)
        sys.exit(2)

    lock_fp = _acquire_lock(_LOCK_FILE)
    if lock_fp is None:
        print(
            f"Another memorandum sync/reindex is running (lock held: {_LOCK_FILE}). Aborting.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        report = _reindex(config)
    finally:
        lock_fp.close()  # releases flock

    if as_json:
        print(json.dumps(report, default=str, indent=2))
    else:
        print(_format_report(report))
    sys.exit(1 if report.get("failed", 0) else 0)


def _acquire_lock(path: str):
    """Try to take a non-blocking exclusive flock on `path`. Return the open
    file handle on success, or None if another process holds it."""
    fp = open(path, "w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.close()
        return None
    return fp


def _reindex(config: dict) -> dict:
    from storage.db import Database
    from storage.vector_store import VectorStore

    chroma_path = config["chroma_path"]
    sqlite_path = config["sqlite_path"]
    started = time.time()

    print(f"Wiping chroma directory: {chroma_path}", file=sys.stderr)
    if Path(chroma_path).exists():
        shutil.rmtree(chroma_path)

    db = Database(sqlite_path, read_only=True)
    try:
        total = db.count()
        print(
            f"Rebuilding {total} messages → {chroma_path} "
            f"(model: {(config.get('embedding') or {}).get('model', 'default BGE-M3')})",
            file=sys.stderr,
        )

        vs = VectorStore(chroma_path, embedding=config.get("embedding"))

        inserted = 0
        skipped = 0
        failed = 0
        progress_every = max(50, total // 100) if total else 100

        # Stream rows directly (no JOIN) so we feed channel_id — not the
        # friendlier COALESCE form db.search would synthesize — into the
        # vector_store's channel metadata.
        cur = db.conn.execute(
            "SELECT id, source, channel_id, sender, timestamp, text "
            "FROM messages ORDER BY timestamp ASC"
        )
        for row in cur:
            msg = {
                "id": row["id"],
                "source": row["source"],
                "channel_id": row["channel_id"],
                "sender": row["sender"],
                "timestamp": row["timestamp"],
                "text": row["text"],
            }
            try:
                if vs.insert(msg):
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                failed += 1
                print(f"  failed {msg['id']}: {e}", file=sys.stderr)

            done = inserted + skipped + failed
            if done % progress_every == 0:
                elapsed = time.time() - started
                rate = done / elapsed if elapsed else 0
                eta = (total - done) / rate if rate else 0
                print(
                    f"  {done}/{total}  inserted={inserted} skipped={skipped} "
                    f"failed={failed}  {rate:.1f} msg/s  eta={eta:.0f}s",
                    file=sys.stderr,
                )
    finally:
        db.close()

    elapsed = time.time() - started
    return {
        "chroma_path": chroma_path,
        "total": total,
        "inserted": inserted,
        "skipped": skipped,
        "failed": failed,
        "elapsed_seconds": round(elapsed, 1),
    }


def _format_report(r: dict) -> str:
    lines = [
        "# REINDEX COMPLETE",
        f"  chroma path:        {r['chroma_path']}",
        f"  messages scanned:   {r['total']}",
        f"  inserted:           {r['inserted']}",
        f"  skipped (no text):  {r['skipped']}",
        f"  failed:             {r['failed']}",
        f"  elapsed:            {r['elapsed_seconds']}s",
    ]
    return "\n".join(lines)
