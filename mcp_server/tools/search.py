"""search_messages — keyword (SQL LIKE) or semantic (Chroma) search."""
from mcp.types import TextContent

from mcp_server import server as _srv
from pipeline.format import format_message, format_timestamp, get_display_tz


async def _search_messages(args: dict) -> list[TextContent]:
    """Search messages by keyword or semantic similarity."""
    mode = args.get("mode", "semantic")
    limit = args.get("limit", 20)
    filter_mentions_me = args.get("mentions_me", False)
    source = args.get("source")
    channel = args.get("channel")

    # Resolve channel id/name/display_name to the canonical channel_id so the
    # Chroma metadata filter (which stores channel_id) and SQLite path agree.
    # Pinning source from the resolved row also disambiguates cross-source name
    # collisions for the rest of the call.
    if channel:
        ch_row = _srv.get_db().resolve_channel(channel, source)
        if ch_row:
            channel = ch_row["id"]
            source = source or ch_row["source"]

    if mode == "semantic":
        hits = _srv.get_vs().semantic_search(
            query=args["query"],
            n_results=limit,
            source=source,
            channel=channel,
            since=args.get("since"),
        )

        if not hits:
            return [TextContent(type="text", text="No semantic matches found.")]

        config = _srv.get_config()
        tz = get_display_tz(config)
        db_rows = {r["id"]: r for r in _srv.get_db().get_by_ids([h["id"] for h in hits])}
        results = []
        for h in hits:
            row = db_rows.get(h["id"])
            if filter_mentions_me and (not row or not row.get("mentions_me")):
                continue
            if row:
                results.append(format_message(row, config=config))
            else:
                results.append(
                    f"[{format_timestamp(h['metadata'].get('timestamp', ''), tz)}] "
                    f"[{h['metadata'].get('source', '')}/{h['metadata'].get('channel', '')}] "
                    f"{h['metadata'].get('sender', '')}: {h['text'][:200]}"
                )
    else:
        rows = _srv.get_db().search(
            query=args["query"],
            source=source,
            channel=channel,
            since=args.get("since"),
            mentions_me=filter_mentions_me,
            limit=limit,
        )

        if not rows:
            return [TextContent(type="text", text="No keyword matches found.")]

        results = [format_message(r, config=_srv.get_config()) for r in rows]

    return [TextContent(type="text", text="\n".join(results))]
