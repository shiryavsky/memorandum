"""list_channels / get_new_messages / send_message — channel + messaging tools.

`_build_live_connector` (the live-fetch connector builder) lives on
`mcp_server.server` rather than here because tests patch it at that
path. These tools reach for it via `_srv._build_live_connector(...)` so
the patch keeps working.
"""
import json
import sys

from mcp.types import TextContent

from mcp_server import server as _srv
from mcp_server.tools.digests import _short_description
from pipeline.format import format_message


async def _list_channels(args: dict) -> list[TextContent]:
    """List known channels (id + name + description) from the DB for discovery."""
    rows = _srv.get_db().list_channels(source=args.get("source"))
    if not rows:
        return [TextContent(type="text", text="No channels found. Run an ingest first.")]

    lines = ["# Channels\n"]
    for r in rows:
        name = r.get("display_name") or r.get("name") or r.get("id")
        line = f"- [{r['source']}] {name} — id: `{r['id']}`"
        desc = _short_description(r.get("description"))
        if desc:
            line += f" — _{desc}_"
        lines.append(line)
    return [TextContent(type="text", text="\n".join(lines))]


async def _get_new_messages(args: dict) -> list[TextContent]:
    """Fetch messages newer than what's in the DB for a channel, live from the source."""
    source = args.get("source", "").strip()
    channel = args.get("channel", "").strip()
    limit = args.get("limit", 50)
    if not source or not channel:
        return [TextContent(type="text", text="Both 'source' and 'channel' are required.")]

    config = _srv.get_config()
    src_cfg = config.get("sources", {}).get(source)
    if not src_cfg:
        return [TextContent(type="text",
                            text=f"Unknown source '{source}'. Use get_stats to list sources.")]

    if src_cfg.get("type") == "email":
        return [TextContent(type="text",
                            text=f"Live fetch is not supported for IMAP sources ('{source}'); "
                                 f"the next scheduled ingest will pick up new email.")]

    text_extensions = set(config.get("text_extensions", [".txt", ".md", ".log", ".json", ".lst"]))
    connector = _srv._build_live_connector(source, src_cfg, text_extensions)
    if connector is None:
        return [TextContent(type="text",
                            text=f"Source type '{src_cfg.get('type')}' is not supported.")]

    try:
        connector.connect()
        messages = connector.fetch_new(channel, limit=limit)
    except NotImplementedError as e:
        return [TextContent(type="text", text=str(e))]
    except ValueError as e:
        return [TextContent(type="text", text=str(e))]
    except Exception as e:
        return [TextContent(type="text", text=f"Failed to read channel '{channel}': {e}")]
    finally:
        try:
            connector.disconnect()
        except Exception:
            pass

    db = _srv.get_db()
    messages = [m for m in messages if not db.exists(m["id"])]
    if not messages:
        return [TextContent(type="text",
                            text=f"No new messages in '{channel}' ({source}) since last ingest.")]

    # Classify internal/external live (these aren't ingested yet, so they carry no flag).
    resolver = _srv.get_resolver()
    source_is_internal = bool(src_cfg.get("internal", False))
    for m in messages:
        sender_id = m.get("sender_id") or ""
        email = m.get("sender_email")
        if not email and sender_id:
            row = db.get_sender(source, sender_id)
            email = (row or {}).get("email")
        m["internal"] = 1 if resolver.is_internal(
            m.get("sender", ""), email=email, source_internal=source_is_internal,
        ) else 0

    chan_label = messages[0].get("channel") or channel
    header = f"# {len(messages)} new message(s) in [{source}] {chan_label}\n"
    lines = [format_message(m, config=config) for m in messages]
    return [TextContent(type="text", text=header + "\n".join(lines))]


def _record_sent(source: str, channel: str, reply_to, text: str,
                 success: bool, message_id=None, error: str = None) -> None:
    """Log a send attempt (success or failure) to the sent_messages audit table."""
    try:
        _srv.get_db().record_sent_message({
            "source": source, "channel": channel, "reply_to": reply_to,
            "text": text, "success": success, "message_id": message_id, "error": error,
        })
    except Exception as e:
        print(f"Failed to log sent message: {e}", file=sys.stderr)


async def _send_message(args: dict) -> list[TextContent]:
    """Send a text message to a channel, gated by the source's allow_send flag."""
    source = args.get("source", "").strip()
    channel = args.get("channel", "").strip()
    text = args.get("text", "")
    reply_to = args.get("reply_to") or None
    if not source or not channel or not text:
        return [TextContent(type="text", text="'source', 'channel', and 'text' are required.")]

    config = _srv.get_config()
    src_cfg = config.get("sources", {}).get(source)
    if not src_cfg:
        return [TextContent(type="text",
                            text=f"Unknown source '{source}'. Use get_stats to list sources.")]

    if src_cfg.get("allow_send") is not True:
        return [TextContent(type="text",
                            text=f"Sending is disabled for source '{source}'. "
                                 f"Set allow_send: true in its config to enable.")]

    stype = src_cfg.get("type")
    connector = _srv._build_live_connector(source, src_cfg, set())
    if connector is None:
        return [TextContent(type="text", text=f"Source type '{stype}' is not supported.")]

    try:
        connector.connect()
        # Every connector exposes the same `send_message(channel, text, reply_to)`
        # surface; platform-specific glue (Telegram's business_connection_id
        # lookup, Pachca/Telegram's int-coercion of reply_to, Email's parent
        # lookup) lives inside the connector itself.
        message_id = connector.send_message(channel, text, reply_to=reply_to)
        url = connector.message_url(channel, message_id)
    except Exception as e:
        _record_sent(source, channel, reply_to, text, success=False, error=str(e))
        return [TextContent(type="text", text=f"Failed to send message to '{channel}' ({source}): {e}")]
    finally:
        try:
            connector.disconnect()
        except Exception:
            pass

    _record_sent(source, channel, reply_to, text, success=True, message_id=message_id)
    result = {"success": True, "message_id": message_id, "url": url}
    if stype == "email":
        result["draft"] = True
        result["note"] = ("Drafted to your Drafts folder — open your mail client to "
                          "review and send.")
    return [TextContent(type="text", text=json.dumps(result))]
