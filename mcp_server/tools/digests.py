"""summarize_channel / summarize_messages — windowed message digests."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from mcp.types import TextContent

from mcp_server import server as _srv
from pipeline.format import (
    ext_marker as _ext_marker,
    format_timestamp,
    get_display_tz,
    make_message_url,
)


MAX_DESCRIPTION_LEN = 300


def _short_description(desc: Optional[str]) -> Optional[str]:
    """Collapse whitespace and truncate a channel description for inline display."""
    if not desc:
        return None
    flat = " ".join(desc.split())
    return flat if len(flat) <= MAX_DESCRIPTION_LEN else flat[:MAX_DESCRIPTION_LEN - 1] + "…"


def _build_channel_desc_lookup(db) -> dict[tuple, str]:
    """Return a (source, display_name)→description map for adding context in digests."""
    out: dict[tuple, str] = {}
    for r in db.list_channels():
        desc = r.get("description")
        if not desc:
            continue
        name = r.get("display_name") or r.get("name") or r.get("id")
        out[(r["source"], name)] = desc
    return out


async def _summarize_channel(args: dict) -> list[TextContent]:
    """Get messages from a channel for summarization."""
    since_str = args.get("since")
    if since_str:
        since = since_str
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()

    rows = _srv.get_db().search(
        channel=args["channel"],
        since=since,
        limit=args.get("max_msgs", 100),
    )

    if not rows:
        return [TextContent(
            type="text",
            text=f"No messages found in channel '{args['channel']}' for the specified period.",
        )]

    config = _srv.get_config()
    tz = get_display_tz(config)
    parts = [f"## Messages from {args['channel']} (since {since})\n"]
    # Prepend the channel description as context (first match across sources is fine; name
    # collisions are rare and the description still belongs to one of them).
    desc_lookup = _build_channel_desc_lookup(_srv.get_db())
    desc = next((d for (_, name), d in desc_lookup.items() if name == args["channel"]), None)
    desc = _short_description(desc)
    if desc:
        parts.append(f"_Channel description: {desc}_\n")
    for r in rows:
        line = f"[{format_timestamp(r['timestamp'], tz)}] {r['sender']}{_ext_marker(r)}: {r['text']}"
        url = make_message_url(r, config)
        if url:
            line += f" 🔗 {url}"
        parts.append(line)

    return [TextContent(type="text", text="\n".join(parts))]


async def _summarize_messages(args: dict) -> list[TextContent]:
    """Generate a digest of messages from a flexible time range.

    Args:
        hours: Number of hours to look back (overrides days)
        days: Number of days to look back (default: 1)
        source: Optional filter by source
        channel: Optional filter by channel name
        max_messages: Maximum messages per channel (default: 100)
    """
    hours = args.get("hours")
    if hours:
        delta = timedelta(hours=hours)
        time_desc = f"{hours} hours"
    else:
        days = args.get("days", 1)
        delta = timedelta(days=days)
        time_desc = f"{days} day{'s' if days != 1 else ''}"

    since = (datetime.now(timezone.utc) - delta).isoformat()

    rows = _srv.get_db().search(
        source=args.get("source"),
        channel=args.get("channel"),
        since=since,
        limit=1000,
    )

    if not rows:
        return [TextContent(
            type="text",
            text=f"No messages in the last {time_desc}.",
        )]

    by_channel: dict[tuple, list] = {}
    for r in rows:
        key = (r['source'], r['channel'])
        by_channel.setdefault(key, []).append(r)

    config = _srv.get_config()
    max_per_channel = args.get("max_messages", 100)
    desc_lookup = _build_channel_desc_lookup(_srv.get_db())
    parts = [f"# Message Digest: Last {time_desc}\n"]
    parts.append(f"_Total: {len(rows)} messages across {len(by_channel)} channels_\n")

    for (src, ch), msgs in sorted(by_channel.items(), key=lambda x: len(x[1]), reverse=True):
        parts.append(f"\n## [{src}] {ch} ({len(msgs)} messages)\n")
        desc = _short_description(desc_lookup.get((src, ch)))
        if desc:
            parts.append(f"_{desc}_\n")
        for m in msgs[:max_per_channel]:
            url = make_message_url(m, config)
            link = f" 🔗 {url}" if url else ""
            parts.append(f"- **{m['sender']}**{_ext_marker(m)}: {m['text'][:150]}{link}")
        if len(msgs) > max_per_channel:
            parts.append(f"- ... and {len(msgs) - max_per_channel} more")

    return [TextContent(type="text", text="\n".join(parts))]
