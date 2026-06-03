"""Retention / housekeeping.

Orchestrates the cross-store fan-out for "delete everything older than the
configured horizon" — one SQLite transaction for the relational rows, plus a
chroma bulk-delete and a filesystem mark-and-sweep for the cache.

Same architectural shape as ``pipeline/health.py``: no MCP / no connector
imports. Both the ingest end-of-run hook and the ``memorandum prune`` CLI
verb route through ``run_housekeeping``.

Invariants:
- Returns early if retention is disabled (``retention_days`` falsy) or the
  throttle says we already ran within ``prune_interval_hours``.
- External-store failures (chroma, filesystem) are LOGGED, not raised. The
  SQLite prune is the source of truth; the vector store and cache will catch
  up on a later prune cycle.
- ``dry_run=True`` returns the same dict shape as a real run but performs
  ZERO writes (no SQL DELETE, no chroma call, no file unlink, no audit row).
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_FILE_CACHE_GRACE_SECONDS = 3600  # 1h


def run_housekeeping(
    db,
    vs,
    attachments_path: str = "data/attachments",
    retention_days: Optional[int] = None,
    prune_interval_hours: int = 24,
    file_cache_grace_seconds: int = DEFAULT_FILE_CACHE_GRACE_SECONDS,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> dict:
    """Prune SQLite + Chroma + file cache for one retention horizon.

    Args:
        db: ``storage.db.Database``.
        vs: ``storage.vector_store.VectorStore`` or anything exposing
            ``delete_many(ids)`` (mock in tests; ``None`` to skip vector fan-out).
        attachments_path: where message attachments live; non-existent path is OK.
        retention_days: keep messages younger than this. Falsy = disabled.
        prune_interval_hours: skip if a prune ran less than this many hours ago.
        file_cache_grace_seconds: don't sweep cache files younger than this
            (default 1h). Protects against sweeping a just-downloaded file in
            the rare case of an interrupted ingest cycle.
        dry_run: count-only; perform no writes.
        now: injectable clock for tests. Defaults to ``datetime.now(timezone.utc)``.

    Returns:
        Dict with ``status`` (``"ok"``, ``"disabled"``, ``"throttled"``,
        ``"dry_run"``, ``"error"``), ``cutoff_ts``, per-kind delete counts,
        and ``last_prune_at``. The file deletion count is split into
        ``files_orphans_swept`` (cache files no message referenced — typically
        from filter / duplicate drops in earlier ingests) and
        ``files_with_deleted_messages`` (files whose last surviving reference
        was just pruned by retention). ``files_deleted`` is the sum for
        backward-compat with the audit table. Keys are stable across statuses.
    """
    now = now or datetime.now(timezone.utc)
    started = now.isoformat()
    last_prune = db.last_prune_at()
    base = {
        "status": "ok",
        "cutoff_ts": None,
        "messages_deleted": 0,
        "mentions_deleted": 0,
        "vectors_deleted": 0,
        "files_deleted": 0,                # sum of the two below — audit-table compat
        "files_orphans_swept": 0,          # cache files no message referenced
        "files_with_deleted_messages": 0,  # files whose last ref was just retention-deleted
        "sent_deleted": 0,
        "runs_deleted": 0,
        "last_prune_at": last_prune,
        "dry_run": bool(dry_run),
        "started_at": started,
        "finished_at": None,
    }

    if not retention_days:
        base["status"] = "disabled"
        return base

    # Throttle: skip if a previous prune is younger than the configured interval.
    # `--dry-run` (CLI) bypasses the throttle since it doesn't actually consume
    # the next-prune budget.
    if last_prune and not dry_run and prune_interval_hours > 0:
        try:
            last_dt = datetime.fromisoformat(last_prune)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (now - last_dt) < timedelta(hours=prune_interval_hours):
                base["status"] = "throttled"
                base["finished_at"] = datetime.now(timezone.utc).isoformat()
                return base
        except (ValueError, TypeError):
            # Unparseable last_prune_at shouldn't block a real run; warn and continue.
            logger.warning(f"Unparseable last_prune_at={last_prune!r}; ignoring throttle")

    cutoff = now - timedelta(days=int(retention_days))
    cutoff_iso = cutoff.isoformat()
    base["cutoff_ts"] = cutoff_iso

    if dry_run:
        try:
            plan = db.count_prune_candidates(cutoff_iso)
        except Exception as e:
            logger.error(f"housekeeping dry-run query failed: {e}")
            base["status"] = "error"
            base["finished_at"] = datetime.now(timezone.utc).isoformat()
            base["error"] = str(e)[:300]
            return base
        base["messages_deleted"] = plan["messages_deleted"]
        base["mentions_deleted"] = plan["mentions_deleted"]
        base["sent_deleted"] = plan["sent_deleted"]
        base["runs_deleted"] = plan["runs_deleted"]
        # Vectors: assume 1-1 with messages (the only path that populates them).
        base["vectors_deleted"] = plan["messages_deleted"]
        # Files: project what mark-and-sweep would drop without actually
        # unlinking. The grace period applies to dry-run too so the projection
        # matches what a real run would touch right now.
        _, would_unlink = _file_cache_projection(
            attachments_path, db, min_age_seconds=file_cache_grace_seconds,
        )
        base["files_deleted"] = len(would_unlink)
        base["files_orphans_swept"] = len(would_unlink)  # dry-run can't distinguish; treat as orphans
        base["files_with_deleted_messages"] = 0
        base["files_freed_bytes"] = sum(p.stat().st_size for p in would_unlink if p.exists())
        base["status"] = "dry_run"
        base["finished_at"] = datetime.now(timezone.utc).isoformat()
        return base

    # Real run.
    error_text: Optional[str] = None

    # Snapshot the referenced-file-ids set BEFORE the SQL prune. Diffing this
    # against the post-prune set tells us which file_ids lost their last
    # surviving reference to retention (vs. which were already orphans from
    # earlier filter/duplicate drops). Cheap — one indexed SELECT scan.
    try:
        refs_before = db.referenced_file_ids()
    except Exception:
        refs_before = set()

    try:
        sql_counts = db.prune(cutoff_iso)
    except Exception as e:
        logger.error(f"housekeeping SQL prune failed: {e}")
        base["status"] = "error"
        base["error"] = str(e)[:300]
        base["finished_at"] = datetime.now(timezone.utc).isoformat()
        # No SQL was committed; don't write a prune_runs row (the SQL prune is
        # atomic — either it ran end-to-end or nothing changed).
        return base

    base["messages_deleted"] = sql_counts["messages_deleted"]
    base["mentions_deleted"] = sql_counts["mentions_deleted"]
    base["sent_deleted"] = sql_counts["sent_deleted"]
    base["runs_deleted"] = sql_counts["runs_deleted"]
    msg_ids = sql_counts["message_ids"]

    # Vector fan-out — failure here is degraded, not fatal. The next prune
    # will retry (chroma delete-by-id is idempotent for missing ids).
    if vs is not None and msg_ids:
        try:
            base["vectors_deleted"] = vs.delete_many(msg_ids)
        except Exception as e:
            logger.warning(f"housekeeping vector delete_many failed: {e}")
            error_text = (error_text or "") + f" vectors: {e};"

    # File cache mark-and-sweep — same isolation rules. Split the result into
    # retention-driven (file_ids whose last reference was just deleted) and
    # pure orphans (cache files no message in the DB references). The grace
    # period skips files younger than `file_cache_grace_seconds`.
    try:
        refs_after = db.referenced_file_ids()
        lost_refs = refs_before - refs_after
        orphans, retention_swept, freed_bytes = _file_cache_sweep(
            attachments_path,
            referenced=refs_after,
            retention_driven_ids=lost_refs,
            min_age_seconds=file_cache_grace_seconds,
        )
        base["files_orphans_swept"] = orphans
        base["files_with_deleted_messages"] = retention_swept
        base["files_deleted"] = orphans + retention_swept
        base["files_freed_bytes"] = freed_bytes
    except Exception as e:
        logger.warning(f"housekeeping file cache sweep failed: {e}")
        error_text = (error_text or "") + f" files: {e};"

    base["finished_at"] = datetime.now(timezone.utc).isoformat()
    if error_text:
        base["error"] = error_text.strip()
        base["status"] = "partial"

    # Audit row regardless of partial errors — operator wants to see them.
    try:
        db.record_prune_run(base)
    except Exception as e:
        logger.warning(f"housekeeping audit row failed: {e}")

    return base


# ── file-cache mark-and-sweep ───────────────────────────────────────────────

def _file_cache_sweep(
    attachments_path: str,
    referenced: set,
    retention_driven_ids: Optional[set] = None,
    min_age_seconds: int = DEFAULT_FILE_CACHE_GRACE_SECONDS,
) -> tuple[int, int, int]:
    """Delete cache files whose stem is no longer referenced by any message.

    Content-addressed safe: a file_id referenced by ANY surviving message is
    kept, even if the message that originally added it was just pruned.

    Args:
        attachments_path: directory holding the `{file_id}{ext}` files.
        referenced: set of file_ids referenced by surviving messages right now.
        retention_driven_ids: file_ids whose last surviving reference was just
            deleted by the SQL prune. A swept file whose stem is in this set
            is classified as retention-driven; everything else is a pre-existing
            orphan (typically from filter/duplicate drops in earlier ingests).
        min_age_seconds: skip files younger than this (grace period — default
            1h). Protects a just-downloaded Pachca file with an expiring URL
            from being swept before its message lands.

    Returns:
        ``(orphans_swept, retention_swept, bytes_freed)`` — the orphan and
        retention totals match the dashboard's `files_orphans_swept` and
        `files_with_deleted_messages` keys.
    """
    path = Path(attachments_path)
    if not path.exists():
        return 0, 0, 0
    retention_driven_ids = retention_driven_ids or set()
    cutoff_mtime = time.time() - max(0, int(min_age_seconds))
    orphans = 0
    retention_swept = 0
    freed = 0
    for entry in path.iterdir():
        if not entry.is_file():
            continue
        if entry.stem in referenced:
            continue
        # Grace period: a file written < grace ago is too fresh to sweep — its
        # owning message may still be in flight or about to land in the next run.
        try:
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_mtime > cutoff_mtime:
            continue
        try:
            entry.unlink()
            freed += stat.st_size
            if entry.stem in retention_driven_ids:
                retention_swept += 1
            else:
                orphans += 1
        except OSError as e:
            logger.debug(f"file cache sweep: couldn't delete {entry.name}: {e}")
    return orphans, retention_swept, freed


def _file_cache_projection(
    attachments_path: str,
    db,
    min_age_seconds: int = DEFAULT_FILE_CACHE_GRACE_SECONDS,
) -> tuple[list, list]:
    """Dry-run version: return ``(all_files, would_unlink)`` without touching
    anything.

    Uses the LIVE referenced set (pre-prune) — see body for the conservative
    approximation we accept here. Same content-addressed safety rule AND the
    same grace period the real sweep enforces, so the projection matches what
    a real run at the same moment would do.
    """
    path = Path(attachments_path)
    if not path.exists():
        return [], []
    # Dry-run uses the LIVE referenced-file-ids set (pre-prune state). That
    # means refs only present in to-be-deleted messages are still seen as
    # "referenced", and the corresponding cache files won't appear in
    # would_unlink. Net effect: dry-run reports a CONSERVATIVE files_deleted
    # count (never overstates how much would be freed). The real run does the
    # accurate sweep AFTER the SQL prune commits.
    referenced = db.referenced_file_ids()
    cutoff_mtime = time.time() - max(0, int(min_age_seconds))
    all_files = [p for p in path.iterdir() if p.is_file()]
    would_unlink = []
    for p in all_files:
        if p.stem in referenced:
            continue
        try:
            if p.stat().st_mtime > cutoff_mtime:
                continue
        except OSError:
            continue
        would_unlink.append(p)
    return all_files, would_unlink
