"""get_thread / find_by_issue / who_mentioned — reply chains, issue lookup, mentions."""
from mcp.types import TextContent

from config import get_aliases
from mcp_server import server as _srv
from pipeline.format import format_message


async def _get_thread(args: dict) -> list[TextContent]:
    """Reconstruct a conversation thread (root + replies) ordered by timestamp."""
    thread_id = args.get("thread_id", "").strip()
    if not thread_id:
        return [TextContent(type="text", text="thread_id is required.")]

    rows = _srv.get_db().get_thread(
        thread_id,
        channel=args.get("channel"),
        limit=args.get("limit", 50),
    )
    if not rows:
        return [TextContent(type="text", text=f"No thread found for thread_id '{thread_id}'.")]

    config = _srv.get_config()
    parts = [f"# Thread {thread_id} ({len(rows)} messages)\n"]
    for r in rows:
        line = format_message(r, config=config, show_thread=False)
        if r.get("reply_to_id"):
            line += f"  ↳ reply to {r['reply_to_id']}"
        parts.append(line)

    return [TextContent(type="text", text="\n".join(parts))]


async def _find_by_issue(args: dict) -> list[TextContent]:
    """Return messages referencing a YouTrack issue id via links or channel name."""
    issue_id = (args.get("issue_id") or "").strip()
    if not issue_id:
        return [TextContent(type="text", text="'issue_id' is required (e.g. 'PL-15491').")]

    rows = _srv.get_db().find_by_issue_id(issue_id, limit=args.get("limit", 50))
    if not rows:
        return [TextContent(type="text",
                            text=f"No messages found referencing '{issue_id}'.")]

    config = _srv.get_config()
    lines = [f"# {len(rows)} message(s) referencing {issue_id}\n"]
    lines.extend(format_message(r, config=config) for r in rows)
    return [TextContent(type="text", text="\n".join(lines))]


async def _who_mentioned(args: dict) -> list[TextContent]:
    """Return messages where `target` was @-mentioned. Resolves target/by via aliases."""
    target = (args.get("target") or "").strip()
    if not target:
        return [TextContent(type="text", text="'target' is required ('me' or a name/alias).")]

    config = _srv.get_config()
    _, my_aliases = get_aliases(config)
    resolver = _srv.get_resolver()

    if target.lower() == "me":
        if not my_aliases:
            return [TextContent(type="text",
                                text="'me' was requested but no my_aliases are configured.")]
        # The current user has no slot in user_aliases (`my_aliases` is its own
        # concept), so mentions of self are typically stored with `mentioned_canonical`
        # NULL and only the raw token / sender_id filled in. Match across all three
        # identifying columns rather than just canonical.
        target_canonical = my_aliases[0]
        tokens = [f"@{a}" for a in my_aliases]
        sender_ids = _resolve_my_sender_ids(config, my_aliases)
        by = (args.get("by") or "").strip()
        by_canonical = resolver.resolve(by) if by else None
        rows = _srv.get_db().get_mentions_for_identity(
            canonicals=[target_canonical],
            tokens=tokens,
            sender_ids=sender_ids,
            sender_canonical=by_canonical,
            source=args.get("source"),
            since=args.get("since"),
            until=args.get("until"),
            limit=args.get("limit", 50),
        )
    else:
        target_canonical = resolver.resolve(target)
        by = (args.get("by") or "").strip()
        by_canonical = resolver.resolve(by) if by else None
        rows = _srv.get_db().get_mentions(
            mentioned_canonical=target_canonical,
            sender_canonical=by_canonical,
            source=args.get("source"),
            since=args.get("since"),
            until=args.get("until"),
            limit=args.get("limit", 50),
        )

    if not rows:
        scope = f" by {by_canonical}" if by_canonical else ""
        return [TextContent(type="text",
                            text=f"No mentions of '{target_canonical}'{scope} found.")]

    header_by = f" by {by_canonical}" if by_canonical else ""
    lines = [f"# {len(rows)} mention(s) of {target_canonical}{header_by}\n"]
    for r in rows:
        token = r.get("mentioned_token") or ""
        marker = f"  ↳ as {token}" if token else ""
        lines.append(format_message(r, config=config) + marker)
    return [TextContent(type="text", text="\n".join(lines))]


def _resolve_my_sender_ids(config: dict, my_aliases: list[str]) -> list[str]:
    """Look up the current user's sender_ids across all configured sources.

    Each `my_aliases` entry may be a chat handle (`john.doe`) — we ask
    each source's senders table whether that string is a username. Hits become
    sender_ids the mention-id column can match against. Misses are silent (full
    names, nicknames, etc. simply don't show up in `senders.username`)."""
    db = _srv.get_db()
    out: list[str] = []
    seen: set = set()
    for source_name in (config.get("sources") or {}):
        for alias in my_aliases:
            sid = db.find_sender_id_by_username(source_name, alias)
            if sid and sid not in seen:
                seen.add(sid)
                out.append(sid)
    return out
