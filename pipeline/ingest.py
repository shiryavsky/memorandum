"""Ingest orchestrator for message collection pipeline."""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import time
from concurrent.futures import ThreadPoolExecutor

from config import (
    load_config,
    get_sources,
    get_aliases,
    get_internal_domains,
    get_ingest_settings,
)
from .alias_resolver import AliasResolver
from connectors.email_connector import EmailConnector
from connectors.mattermost_connector import MattermostConnector
from connectors.pachca_connector import PachcaConnector
from connectors.telegram_connector import TelegramConnector
from storage.db import Database
from storage.vector_store import VectorStore
from .filter_engine import FilterEngine


# ── YouTrack issue-id helpers ────────────────────────────────────────────────
# Single source of truth for the issue-id pattern. Used by URL extraction (message
# text) and channel-name parsing alike — connectors lazy-import these to avoid a
# circular dependency on this module.

_URL_RE = re.compile(r"https?://\S+")


def build_youtrack_issue_regex(prefixes: list) -> Optional[re.Pattern]:
    """Compile ``\\b(PREFIX1|PREFIX2|...)-\\d+\\b`` from the configured project prefixes.

    Returns None when prefixes is empty/missing — callers treat that as "issue-ID
    detection disabled" and skip classification gracefully.
    """
    cleaned = [p.strip() for p in (prefixes or []) if p and p.strip()]
    if not cleaned:
        return None
    pattern = r"\b(" + "|".join(re.escape(p) for p in cleaned) + r")-\d+\b"
    return re.compile(pattern)


def _url_host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1).lower() if m else ""


def extract_urls(text: str, youtrack_cfg: Optional[dict] = None) -> list[dict]:
    """Find URLs in a message and classify YouTrack issue links.

    YouTrack matches are detected by the configured ``base_url`` host; the issue id is
    pulled from the URL via the shared prefix regex. Everything else is returned as
    ``{"type": "other", "url": ...}``. If no YouTrack config is supplied, URLs are still
    extracted — just never classified as ``youtrack``.
    """
    if not text:
        return []
    cfg = youtrack_cfg or {}
    yt_host = _url_host((cfg.get("base_url") or "").rstrip("/"))
    issue_re = build_youtrack_issue_regex(cfg.get("project_prefixes"))

    out: list[dict] = []
    for raw_url in _URL_RE.findall(text):
        url = raw_url.rstrip(").,;:!?>")  # strip trailing punctuation/markup
        if yt_host and issue_re and _url_host(url) == yt_host:
            m = issue_re.search(url)
            if m:
                out.append({"type": "youtrack", "issue_id": m.group(0), "url": url})
                continue
        out.append({"type": "other", "url": url})
    return out


# ── @mention extraction ─────────────────────────────────────────────────────
# Recognizes:
#   • @username (Mattermost / Telegram / Pachca @nickname) — letters, digits,
#     underscores; may include dots (e.g. @john.doe).
#   • <@user_id> (Pachca raw form).
# Skips @here / @channel / @all / @everyone (broadcast sentinels, not people)
# and email-style `user@host` (the lookbehind blocks an alphanumeric before @).

_MENTION_RE = re.compile(
    r"<@(?P<id>\d+)>"                                              # <@123>
    r"|(?<![A-Za-z0-9._])@(?P<name>[A-Za-z0-9_][A-Za-z0-9_.]{0,63})"  # @john.doe
)
_MENTION_SENTINELS = {"here", "channel", "all", "everyone"}


def extract_mentions(text: str) -> list[dict]:
    """Find @mentions in a message body.

    Returns a list of dicts: ``{"token": raw form, "lookup": value to resolve,
    "kind": "username"|"user_id"}``. Same name appearing twice is returned twice
    — dedup lives at the row-insert layer if a caller wants it.
    """
    if not text:
        return []
    out: list[dict] = []
    for m in _MENTION_RE.finditer(text):
        if m.group("id"):
            uid = m.group("id")
            out.append({"token": f"<@{uid}>", "lookup": uid, "kind": "user_id"})
        else:
            name = m.group("name")
            if name.lower() in _MENTION_SENTINELS:
                continue
            out.append({"token": f"@{name}", "lookup": name, "kind": "username"})
    return out


def parse_channel_issue_ids(channel_name: str, prefixes: list) -> list:
    """Extract issue ids embedded in a channel name (e.g. ``Dev / PL-15491 mDK v.3``)."""
    issue_re = build_youtrack_issue_regex(prefixes)
    if not issue_re or not channel_name:
        return []
    seen: list = []
    for m in issue_re.finditer(channel_name):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return seen


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def run_ingest(
    since: Optional[datetime] = None,
    config_path: str = "config.yaml",
    force: bool = False
) -> dict:
    """Run the ingest pipeline for all configured sources.

    Args:
        since: Default start time for channels without saved state.
        config_path: Path to configuration file.
        force: If True, ignore saved channel state and use the since timestamp.

    Returns:
        Dictionary with ingest statistics.
    """
    config = load_config(config_path)
    db = Database(config["sqlite_path"])
    vs = VectorStore(config["chroma_path"], embedding=config.get("embedding"))
    text_extensions = set(config.get("text_extensions", [".txt", ".md", ".log", ".json", ".lst"]))
    youtrack_cfg = config.get("youtrack") or {}

    user_aliases, my_aliases = get_aliases(config)
    internal_domains = get_internal_domains(config)
    resolver = AliasResolver(user_aliases, my_aliases, internal_domains=internal_domains)
    db.upsert_aliases(user_aliases)

    # Sources flagged internal: true mean every sender from them is a company user.
    source_internal = {
        name: bool(cfg.get("internal", False))
        for name, cfg in config.get("sources", {}).items()
    }

    stats = {
        "sources_checked": 0,
        "messages_fetched": 0,
        "messages_filtered": 0,
        "messages_new": 0,
        "messages_duplicate": 0,
        "messages_failed": 0,
        "senders_cached": 0,
        "channels_scanned": 0,
        "channels_skipped": 0,
    }
    source_errors: list[dict] = []
    started_at = datetime.now(timezone.utc).isoformat()
    connectors = []

    try:
        # Build one (connector, filter_engine) pair per enabled source
        for source_name, source_cfg in get_sources(config):
            source_type = source_cfg.get("type")
            source_filters = source_cfg.get("filters", {})

            if source_type == "mattermost":
                try:
                    connector = MattermostConnector(
                        base_url=source_cfg["url"],
                        token=source_cfg["token"],
                        source_name=source_name,
                        skip_channels=source_filters.get("skip_channels"),
                        only_channels=source_filters.get("only_channels"),
                        db_callback=db.get_channel if not force else None,
                        db=db,
                        text_extensions=text_extensions,
                        youtrack_cfg=youtrack_cfg,
                    )
                    connector.connect()
                    filter_engine = FilterEngine(source_filters)
                    connectors.append((source_name, connector, filter_engine))
                    logger.info(f"[{source_name}] Mattermost connector initialized")
                    if source_filters.get("only_channels"):
                        logger.info(f"[{source_name}] only_channels: {source_filters['only_channels']}")
                except Exception as e:
                    logger.error(f"[{source_name}] Failed to initialize: {e}")
                    source_errors.append({"source": source_name, "error": str(e)[:300]})

            elif source_type == "telegram":
                try:
                    connector = TelegramConnector(
                        bot_token=source_cfg["token"],
                        source_name=source_name,
                        chat_ids=source_filters.get("only_channels"),
                        db_callback=db.get_channel if not force else None,
                        db=db,
                        text_extensions=text_extensions,
                        youtrack_cfg=youtrack_cfg,
                    )
                    connector.connect()
                    filter_engine = FilterEngine(source_filters)
                    connectors.append((source_name, connector, filter_engine))
                    logger.info(f"[{source_name}] Telegram connector initialized")
                except Exception as e:
                    logger.error(f"[{source_name}] Failed to initialize: {e}")
                    source_errors.append({"source": source_name, "error": str(e)[:300]})

            elif source_type == "pachca":
                try:
                    connector = PachcaConnector(
                        access_token=source_cfg["token"],
                        source_name=source_name,
                        skip_channels=source_filters.get("skip_channels"),
                        only_channels=source_filters.get("only_channels"),
                        db_callback=db.get_channel if not force else None,
                        db=db,
                        text_extensions=text_extensions,
                        youtrack_cfg=youtrack_cfg,
                    )
                    connector.connect()
                    filter_engine = FilterEngine(source_filters)
                    connectors.append((source_name, connector, filter_engine))
                    logger.info(f"[{source_name}] Pachca connector initialized")
                except Exception as e:
                    logger.error(f"[{source_name}] Failed to initialize: {e}")
                    source_errors.append({"source": source_name, "error": str(e)[:300]})

            elif source_type == "email":
                try:
                    connector = EmailConnector(
                        host=source_cfg["host"],
                        port=source_cfg.get("port", 993),
                        username=source_cfg["username"],
                        password=source_cfg["password"],
                        source_name=source_name,
                        folders=source_cfg.get("folders"),
                        skip_folders=source_filters.get("skip_folders"),
                        channel_names=source_cfg.get("channel_names"),
                        drafts_folder=source_cfg.get("drafts_folder", "Drafts"),
                        from_address=source_cfg.get("from_address"),
                        db_callback=db.get_channel if not force else None,
                        db=db,
                        text_extensions=text_extensions,
                        youtrack_cfg=youtrack_cfg,
                    )
                    connector.connect()
                    filter_engine = FilterEngine(source_filters)
                    connectors.append((source_name, connector, filter_engine))
                    logger.info(f"[{source_name}] Email (IMAP) connector initialized")
                except Exception as e:
                    logger.error(f"[{source_name}] Failed to initialize: {e}")
                    source_errors.append({"source": source_name, "error": str(e)[:300]})

            else:
                logger.warning(f"[{source_name}] Unknown source type '{source_type}', skipping")

        stats["sources_checked"] = len(connectors)

        if not connectors:
            logger.warning("No connectors enabled, nothing to do")
            return stats

        if since is None:
            since = datetime.now(timezone.utc) - timedelta(minutes=20)
        since_ms = int(since.timestamp() * 1000)
        logger.info(f"Scanning sources since {since} ({since_ms})")

        # Fetch + filter per source. Sources are independent units of work
        # dominated by network I/O, so we fan out across a thread pool.
        # `fetch_workers=1` keeps a strictly sequential code path (no executor)
        # for debugging / parity with the legacy behavior.
        ingest_settings = get_ingest_settings(config)
        worker_count = _resolve_worker_count(len(connectors), ingest_settings)
        logger.info(
            f"Fetching {len(connectors)} source(s) "
            f"with {'1 worker (sequential)' if worker_count == 1 else f'{worker_count} workers'}"
        )

        fetch_started = time.monotonic()
        results = _fetch_all(
            connectors, worker_count,
            since=since, force=force, since_ms=since_ms,
        )
        wall_ms = int((time.monotonic() - fetch_started) * 1000)
        _log_fetch_summary(results, wall_ms)

        all_messages = []
        for r in results:
            if r["status"] == "error":
                source_errors.append({"source": r["source"], "error": r["error"]})
                continue
            stats["messages_fetched"] += r["messages_count"]
            stats["channels_scanned"] += r["channels_scanned"]
            stats["channels_skipped"] += r["channels_skipped"]
            stats["messages_filtered"] += r["dropped"]
            all_messages.extend(r["kept"])

        if not all_messages:
            logger.info("No messages to store after filtering")
            return stats

        # Enrich messages with canonical sender, mention flag, and extracted @mentions.
        # `email` comes from the cached senders row when present (Mattermost/Pachca
        # populate it; Telegram leaves it empty). Domain rule fires when it does.
        # Email connector additionally attaches a `_recipient_emails` list — the
        # message is internal only when sender AND every recipient is internal.
        email_cache: dict[tuple, str | None] = {}
        for msg in all_messages:
            sender = msg.get("sender", "")
            source = msg.get("source") or ""
            sender_id = msg.get("sender_id") or ""
            email = msg.get("sender_email") or None
            if not email and source and sender_id:
                key = (source, sender_id)
                if key not in email_cache:
                    row = db.get_sender(source, sender_id)
                    email_cache[key] = (row or {}).get("email")
                email = email_cache[key]
            msg["canonical_sender"] = resolver.resolve(sender)
            msg["mentions_me"] = 1 if resolver.mentions_me(msg.get("text", "")) else 0
            sender_internal = resolver.is_internal(
                sender,
                email=email,
                source_internal=source_internal.get(source, False),
            )
            recipients = msg.pop("_recipient_emails", None)
            if sender_internal and recipients:
                # Every recipient must also be internal under the same rules.
                sender_internal = all(
                    resolver.is_internal("", email=addr,
                                         source_internal=source_internal.get(source, False))
                    for addr in recipients
                )
            msg["internal"] = 1 if sender_internal else 0
            msg["_mentions"] = extract_mentions(msg.get("text", ""))

        # Cache senders BEFORE the message+mention insert loop. _insert_mention_rows
        # looks up senders.username to fill mentions.mentioned_sender_id; if the
        # senders table is still empty when mentions are written, every lookup
        # misses and the column stays NULL forever for those rows.
        senders_to_cache: dict = {}
        for msg in all_messages:
            sender_id = msg.get("sender_id")
            source = msg.get("source")
            if sender_id and source:
                key = f"{source}:{sender_id}"
                if key not in senders_to_cache:
                    senders_to_cache[key] = {"sender_id": sender_id, "source": source}

        for source_name, connector, _ in connectors:
            if not hasattr(connector, "get_sender_info"):
                continue
            for key, sender_data in senders_to_cache.items():
                if sender_data["source"] == source_name:
                    try:
                        sender_info = connector.get_sender_info(sender_data["sender_id"])
                        db.upsert_sender(sender_info)
                        stats["senders_cached"] += 1
                    except Exception as e:
                        logger.debug(f"Failed to cache sender {sender_data['sender_id']}: {e}")

        for msg in all_messages:
            try:
                if db.exists(msg["id"]):
                    stats["messages_duplicate"] += 1
                    continue

                if db.insert(msg):
                    vs.insert(msg)
                    stats["messages_new"] += 1
                    _insert_mention_rows(db, msg, resolver)
                else:
                    stats["messages_duplicate"] += 1
            except Exception as e:
                logger.error(f"Failed to store message {msg['id']}: {e}")
                stats["messages_failed"] += 1

        logger.info(
            f"Ingest complete: {stats['messages_new']} new, "
            f"{stats['messages_duplicate']} duplicate, "
            f"{stats['messages_filtered']} filtered, "
            f"{stats['messages_failed']} failed"
        )
        return stats

    finally:
        _disconnect_all(connectors)
        finished_at = datetime.now(timezone.utc).isoformat()
        run_status = _ingest_status(stats["sources_checked"], source_errors)
        try:
            db.record_ingest_run({
                "started_at": started_at,
                "finished_at": finished_at,
                "status": run_status,
                "sources_checked": stats["sources_checked"],
                "messages_new": stats["messages_new"],
                "messages_fetched": stats["messages_fetched"],
                "errors": source_errors,
            })
        except Exception as e:
            logger.warning(f"Failed to record ingest run: {e}")

        # Retention / housekeeping (TASK-028). Only on a successful-ish run
        # (`ok` / `partial`) — never on a hard `error` since the DB may not
        # reflect what's actually in the upstream. Failures here NEVER abort
        # ingest; the next cycle will retry.
        if run_status in ("ok", "partial"):
            try:
                from config import get_retention_settings
                from pipeline.housekeeping import run_housekeeping
                ret = get_retention_settings(config)
                report = run_housekeeping(
                    db, vs,
                    file_cache_dir=config.get("file_cache_dir", "data/file_cache"),
                    retention_days=ret["retention_days"],
                    prune_interval_hours=ret["prune_interval_hours"],
                    file_cache_grace_seconds=int(ret["file_cache_grace_minutes"]) * 60,
                )
                if report["status"] not in ("disabled", "throttled"):
                    # Split the file-deletion count so the operator can tell
                    # "retention deleted N files" from "orphan cache cleanup
                    # swept M files" — they're independent activities sharing
                    # the same housekeeping pass.
                    logger.info(
                        f"Housekeeping {report['status']}: "
                        f"messages={report['messages_deleted']} "
                        f"mentions={report['mentions_deleted']} "
                        f"vectors={report['vectors_deleted']} "
                        f"files=retention:{report['files_with_deleted_messages']}+"
                        f"orphans:{report['files_orphans_swept']} "
                        f"sent={report['sent_deleted']} "
                        f"runs={report['runs_deleted']}"
                    )
            except Exception as e:
                logger.warning(f"Housekeeping failed (ingest unaffected): {e}")


def _log_fetch_summary(results: list[dict], wall_ms: int) -> None:
    """One INFO line per ingest run summarizing per-source fetch latency.

    Sorted slowest-first so the dominating source is at the head — that's the
    one to look at when an ingest run feels slow. With concurrent fetch the
    sum of per-source ms will exceed the wall clock; the trailing `wall=N ms`
    is the operationally interesting number."""
    if not results:
        return
    ordered = sorted(results, key=lambda r: r.get("fetch_ms", 0), reverse=True)
    parts = [f"{r['source']}={r.get('fetch_ms', 0)}ms" for r in ordered]
    logger.info(f"Fetch summary: {' '.join(parts)} (wall={wall_ms} ms)")


def _resolve_worker_count(num_sources: int, settings: dict) -> int:
    """Decide how many fetch workers to spin up for this run.

    `fetch_workers=None` (config: missing/0/null) means auto = num_sources, capped.
    An explicit positive value is used as-is (still capped by num_sources, since
    extra workers would idle). Empty source list returns 1 — no workers needed
    but the sequential fast-path stays simple.
    """
    if num_sources <= 0:
        return 1
    explicit = settings.get("fetch_workers")
    cap = settings.get("max_fetch_workers", 8)
    if explicit is None:
        return min(num_sources, cap)
    return max(1, min(explicit, num_sources))


def _fetch_one(source_name: str, connector, filter_engine,
               since, force: bool, since_ms: int) -> dict:
    """Run one source's fetch + filter and return a uniform result dict.

    Never raises — connector failures become ``{"status": "error", ...}`` so the
    parallel dispatcher can keep aggregating other sources' results. `fetch_ms`
    is wall-clock-per-source — handy for spotting which source dominates a run
    when several fetch in parallel.
    """
    started = time.monotonic()
    try:
        result = connector.fetch_messages(since, force=force, default_since_ms=since_ms)
        fetched = result["messages"]
        kept = filter_engine.filter_messages(fetched)
        dropped = len(fetched) - len(kept)
        fetch_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            f"[{source_name}] {result['messages_count']} fetched, "
            f"{dropped} filtered, {len(kept)} kept "
            f"({fetch_ms} ms)"
        )
        return {
            "status": "ok",
            "source": source_name,
            "messages_count": result["messages_count"],
            "channels_scanned": result["channels_scanned"],
            "channels_skipped": result["channels_skipped"],
            "kept": kept,
            "dropped": dropped,
            "fetch_ms": fetch_ms,
        }
    except Exception as e:
        fetch_ms = int((time.monotonic() - started) * 1000)
        logger.error(f"[{source_name}] Error fetching after {fetch_ms} ms: {e}")
        return {
            "status": "error",
            "source": source_name,
            "error": str(e)[:300],
            "fetch_ms": fetch_ms,
        }


def _fetch_all(connectors, worker_count: int,
               since, force: bool, since_ms: int) -> list[dict]:
    """Fan out per-source fetches. Sequential path for worker_count==1."""
    if worker_count == 1:
        return [
            _fetch_one(name, conn, fe, since, force, since_ms)
            for name, conn, fe in connectors
        ]
    with ThreadPoolExecutor(max_workers=worker_count,
                            thread_name_prefix="memorandum-fetch") as pool:
        futures = [
            pool.submit(_fetch_one, name, conn, fe, since, force, since_ms)
            for name, conn, fe in connectors
        ]
        # `future.result()` re-raises anything _fetch_one didn't catch; that's
        # a real bug, not a per-source failure — let it propagate.
        return [f.result() for f in futures]


def _insert_mention_rows(db: Database, msg: dict, resolver: AliasResolver) -> None:
    """Write `mentions` rows for a newly inserted message. Best-effort; swallows errors."""
    raw = msg.pop("_mentions", None) or []
    if not raw:
        return
    source = msg.get("source")
    sender_canonical = msg.get("canonical_sender") or resolver.resolve(msg.get("sender", ""))
    rows: list[dict] = []
    for m in raw:
        lookup = m.get("lookup", "")
        kind = m.get("kind")
        if kind == "user_id":
            mentioned_sender_id = lookup
            mentioned_canonical = None  # without a username we can't alias-resolve
        else:
            mentioned_sender_id = db.find_sender_id_by_username(source, lookup)
            resolved = resolver.resolve(lookup)
            mentioned_canonical = resolved if resolved != lookup else None
        rows.append({
            "message_id": msg["id"],
            "source": source,
            "sender_id": msg.get("sender_id"),
            "sender_canonical": sender_canonical,
            "mentioned_token": m["token"],
            "mentioned_canonical": mentioned_canonical,
            "mentioned_sender_id": mentioned_sender_id,
        })
    try:
        db.insert_mentions(rows)
    except Exception as e:
        logger.warning(f"Failed to insert mentions for {msg['id']}: {e}")


def _ingest_status(sources_checked: int, source_errors: list[dict]) -> str:
    if not source_errors:
        return "ok"
    if sources_checked == 0:
        return "error"
    return "partial"


def _disconnect_all(connectors):
    for source_name, connector, _ in connectors:
        try:
            connector.disconnect()
        except Exception as e:
            logger.warning(f"[{source_name}] Error disconnecting: {e}")


def main(args=None):
    """CLI entry point for running ingest manually."""
    import argparse

    parser = argparse.ArgumentParser(description="Run message ingest pipeline")
    parser.add_argument(
        "--hours", type=float, default=0.33,
        help="Hours back to fetch for new channels or when --force is used (default: 0.33 = ~20 minutes)"
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--force", action="store_true",
        help="Ignore saved channel state and scan from --hours timestamp"
    )
    parsed_args = parser.parse_args(args)

    if parsed_args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    since = datetime.now(timezone.utc) - timedelta(hours=parsed_args.hours)
    stats = run_ingest(since=since, config_path=parsed_args.config, force=parsed_args.force)

    print(f"\n{'='*50}")
    print("  INGEST STATISTICS")
    print(f"{'='*50}")
    print(f"  Sources checked:     {stats['sources_checked']}")
    print(f"  Channels scanned:    {stats['channels_scanned']}")
    print(f"  Channels skipped:    {stats['channels_skipped']}")
    print(f"  Messages fetched:    {stats['messages_fetched']}")
    print(f"  Messages filtered:   {stats['messages_filtered']}")
    print(f"  Messages stored:     {stats['messages_new']}")
    print(f"  Messages duplicate:  {stats['messages_duplicate']}")
    print(f"  Messages failed:     {stats['messages_failed']}")
    print(f"  Senders cached:      {stats['senders_cached']}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
