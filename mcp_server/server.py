"""MCP server exposing message collection tools to Claude."""
from connectors.telegram_connector import TelegramConnector
from connectors.pachca_connector import PachcaConnector
from connectors.mattermost_connector import MattermostConnector
from connectors.email_connector import EmailConnector
from pipeline.alias_resolver import AliasResolver
from storage.vector_store import VectorStore
from storage.db import Database
from config import (
    load_config,
    get_aliases,
    get_internal_domains,
    get_alias_edit_settings,
)
from mcp.types import Tool, TextContent
from mcp.server.stdio import stdio_server
from mcp.server import Server
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))


# Initialize server
app = Server("message-collector")

# Global instances (initialized on first use)
_db: Optional[Database] = None
_vs: Optional[VectorStore] = None
_config: Optional[dict] = None
_config_path: str = "config.yaml"


def get_db() -> Database:
    """Get or create database instance."""
    global _db, _config
    if _db is None:
        _config = _config or load_config(_config_path)
        _db = Database(_config["sqlite_path"])
    return _db


def get_vs() -> VectorStore:
    """Get or create vector store instance."""
    global _vs, _config
    if _vs is None:
        _config = _config or load_config(_config_path)
        _vs = VectorStore(_config["chroma_path"], embedding=_config.get("embedding"))
    return _vs


def get_config() -> dict:
    """Get or load configuration."""
    global _config
    if _config is None:
        _config = load_config(_config_path)
    return _config


def get_display_tz(config: dict) -> ZoneInfo:
    """Return the configured display timezone (falls back to UTC)."""
    tz_name = config.get("display_timezone", "UTC")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def format_timestamp(ts: str, tz: ZoneInfo) -> str:
    """Convert a UTC ISO timestamp to a local datetime string (YYYY-MM-DD HH:MM)."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ts[:16]


def make_message_url(msg: dict, config: dict) -> Optional[str]:
    """Build a permalink to the original message, or return None if not possible."""
    source = msg.get("source", "")
    raw = msg.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = {}
    raw = raw or {}

    source_type = config.get("sources", {}).get(source, {}).get("type", "")

    if source_type == "mattermost":
        source_url = config["sources"][source].get("url", "").rstrip("/")
        if not source_url:
            return None
        team_name = raw.get("team_name", "")
        channel_name = msg.get("channel_name", "")
        post_id = raw.get("post_id", "")
        if not (team_name and channel_name and post_id):
            return None
        return f"{source_url}/{team_name}/channels/{channel_name}/p/{post_id}"

    if source_type == "telegram":
        chat_type = raw.get("chat_type", "")
        if chat_type not in ("channel", "supergroup"):
            return None
        chat_id = raw.get("chat_id", "")
        message_id = raw.get("message_id", "")
        if not (chat_id and message_id):
            return None
        return f"https://t.me/c/{chat_id}/{message_id}"

    if source_type == "pachca":
        chat_id = raw.get("chat_id", "")
        message_id = raw.get("message_id", "")
        if not (chat_id and message_id):
            return None
        return f"https://app.pachca.com/chats/{chat_id}?message={message_id}"

    return None


def _ext_marker(msg: dict) -> str:
    """Return ' [external]' for external senders, '' otherwise.

    Missing 'internal' (e.g. live messages not yet ingested) is treated as internal so we
    never mislabel a sender whose status we don't know.
    """
    return "" if msg.get("internal", 1) else " [external]"


def format_message(msg: dict, max_length: int = 200, config: Optional[dict] = None,
                   show_thread: bool = True) -> str:
    """Format a message for display.

    Args:
        msg: Message dictionary
        max_length: Maximum text length before truncation
        config: Optional config dict; when provided, appends a permalink

    Returns:
        Formatted message string
    """
    tz = get_display_tz(config) if config is not None else ZoneInfo("UTC")
    timestamp = format_timestamp(msg.get("timestamp", ""), tz)
    source = msg.get("source", "")
    channel = msg.get("channel", "")
    sender = msg.get("sender", "")
    text = msg.get("text", "")

    if len(text) > max_length:
        text = text[:max_length] + "..."

    line = f"[{timestamp}] [{source}/{channel}] {sender}{_ext_marker(msg)}: {text}"

    thread_id = msg.get("thread_id")
    if show_thread and thread_id:
        line += f" 🧵 thread:{thread_id}"

    if config is not None:
        url = make_message_url(msg, config)
        if url:
            line += f" 🔗 {url}"

    return line


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available MCP tools."""
    return [
        Tool(
            name="search_messages",
            description="Search messages by keyword or semantic meaning. "
                        "Use semantic mode for natural language queries. "
                        "Messages that are replies show a '🧵 thread:{id}' marker — "
                        "pass that id to get_thread to read the full conversation. "
                        "Senders outside your company are tagged '[external]' after the name; "
                        "internal (company) senders are unmarked.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (keyword or natural language)"
                    },
                    "source": {
                        "type": "string",
                        "description": ("Filter by source name (e.g., 'work_mattermost'). "
                                        "Use get_stats to see available source names.")
                    },
                    "channel": {
                        "type": "string",
                        "description": "Filter by channel name"
                    },
                    "since": {
                        "type": "string",
                        "description": "ISO date (e.g., 2025-04-01)"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["keyword", "semantic"],
                        "default": "semantic",
                        "description": "Search mode: keyword or semantic"
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "description": "Maximum number of results"
                    },
                    "mentions_me": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, return only messages where the current user is mentioned"
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="summarize_channel",
            description="Get recent messages from a specific channel for summarization.",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Channel name to summarize"
                    },
                    "since": {
                        "type": "string",
                        "description": "ISO date (default: 7 days ago)"
                    },
                    "max_msgs": {
                        "type": "integer",
                        "default": 100,
                        "description": "Maximum messages to retrieve"
                    }
                },
                "required": ["channel"]
            }
        ),
        Tool(
            name="summarize_messages",
            description="Get a digest of messages from a flexible time range, grouped by channel. "
                        "Senders outside your company are tagged '[external]'; internal senders are unmarked.",
            inputSchema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Number of hours to look back (e.g., 4, 24, 168 for a week). Overrides 'days'."
                    },
                    "days": {
                        "type": "integer",
                        "description": "Number of days to look back (e.g., 1, 7, 30). Default: 1"
                    },
                    "source": {
                        "type": "string",
                        "description": "Optional: filter by source name (e.g., 'work_mattermost')"
                    },
                    "channel": {
                        "type": "string",
                        "description": "Optional: filter by channel name"
                    },
                    "max_messages": {
                        "type": "integer",
                        "default": 100,
                        "description": "Maximum messages per channel (default: 100)"
                    }
                }
            }
        ),
        Tool(
            name="list_channels",
            description="List known channels (id, name, source, description) from the local "
                        "database. Use this to find the channel id to pass to get_new_messages, "
                        "and to read each channel's human-written purpose/topic when present. "
                        "Only channels seen by a prior ingest are listed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Optional: filter by source name"
                    }
                }
            }
        ),
        Tool(
            name="get_new_messages",
            description="Get messages in a channel that are newer than what's in the local "
                        "database — the gap since the last ingest — fetched live from the source. "
                        "Call this before replying in a chat so you don't miss the latest messages. "
                        "Requires the channel id (use list_channels to find it). Works for "
                        "Mattermost, Pachca, and Telegram. Returns only messages not yet stored, "
                        "ordered oldest→newest. Senders outside your company are tagged '[external]'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Source name (use list_channels or get_stats)"
                    },
                    "channel": {
                        "type": "string",
                        "description": "Channel id within the source (from list_channels)"
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Maximum messages to return (default: 50)"
                    }
                },
                "required": ["source", "channel"]
            }
        ),
        Tool(
            name="get_thread",
            description="Reconstruct a full conversation thread (root message + all replies) "
                        "by thread_id, ordered by timestamp. thread_id is the root post id "
                        "(Mattermost root_id); get it from the '🧵 thread:{id}' marker shown on "
                        "reply messages in search_messages output. Replies show their parent via "
                        "reply_to_id. Senders outside your company are tagged '[external]'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Root post id of the thread"
                    },
                    "channel": {
                        "type": "string",
                        "description": "Optional channel name to narrow the search"
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Maximum messages to retrieve (default: 50)"
                    }
                },
                "required": ["thread_id"]
            }
        ),
        Tool(
            name="find_decisions",
            description="Find messages that contain decisions, conclusions, or action items.",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Optional topic to narrow search"
                    },
                    "channel": {
                        "type": "string",
                        "description": "Optional channel filter"
                    },
                    "since": {
                        "type": "string",
                        "description": "ISO date (default: 30 days ago)"
                    }
                }
            }
        ),
        Tool(
            name="get_stats",
            description="Get statistics about stored messages.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Optional: filter by source"
                    }
                }
            }
        ),
        Tool(
            name="get_user_aliases",
            description="Return configured user aliases plus, when set, each person's role, "
                        "team, reports_to (escalation path), and responsible_for (channels / "
                        "projects / issue prefixes they own). Read this early in a session to "
                        "ground references to people — it carries the who-is-who context that "
                        "doesn't fit into every message. '[internal]' marks company staff; "
                        "unmarked users are external.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_attached_file",
            description=(
                "Get content of an attached file by file_id. "
                "For Telegram and Pachca: file_id is shown inline in message text as 'file_id=...' "
                "(e.g. '[photo, file_id=AgAC...]', '[image: pic.jpg, file_id=3560]'). "
                "For Mattermost: file_id is found in message attachment metadata. "
                "Pachca files are downloaded into the cache at ingest (their URLs expire), so they "
                "are served from cache. "
                "Returns text content for text files; base64-encoded content for binary files "
                "(photos, PDFs, etc.). Downloads and caches on first access."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "File ID (visible in message text for Telegram as 'file_id=...')"
                    }
                },
                "required": ["file_id"]
            }
        ),
        Tool(
            name="get_health",
            description="Get integration and database health: last ingest run status, "
                        "per-source message count and freshness (oldest and most recent message), "
                        "and any errors from the last run.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="find_by_issue",
            description="Find messages referencing a YouTrack issue id (e.g. PL-15491). "
                        "Matches messages whose links resolve to that issue (raw.urls) and "
                        "messages from channels whose name carries the id (channels.extra). "
                        "Requires `youtrack.project_prefixes` configured.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_id": {
                        "type": "string",
                        "description": "Issue id like 'PL-15491'"
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Maximum messages to return (default: 50)"
                    }
                },
                "required": ["issue_id"]
            }
        ),
        Tool(
            name="send_message",
            description="Send a text message to a channel (Mattermost, Telegram, Pachca) or "
                        "draft an email reply (email source). Real sends are visible to other "
                        "people — use them deliberately.\n\n"
                        "MANDATORY before every chat send: call get_new_messages for the same "
                        "source and channel. If it returns ANY new messages, DO NOT send — the "
                        "context you based your reply on is stale. Re-read the new messages, "
                        "reconsider, and draft a fresh reply (which may differ or no longer be "
                        "needed). (Skip this step only for email sources — get_new_messages "
                        "punts for IMAP; the user reviews the draft in their mail client anyway.)\n\n"
                        "Sending only works for sources explicitly configured with allow_send: true; "
                        "the tool refuses otherwise. Pass the channel id (from list_channels), not "
                        "its name. Use reply_to to thread under an existing message: Mattermost "
                        "root post id, Telegram message id, or Pachca parent message id.\n\n"
                        "EMAIL SOURCES are special: send_message **drafts a reply into the user's "
                        "Drafts folder via IMAP** rather than sending it — the user reviews it in "
                        "their mail client (Gmail, Outlook, etc.) and clicks Send themselves. "
                        "`reply_to` is REQUIRED for email and must be the original Message-ID "
                        "being replied to (look it up in the parent message's raw.message_id); "
                        "recipients (reply-all) and threading headers are derived from it. "
                        "Result will include `draft: true`.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Source name (must have allow_send: true in config)"
                    },
                    "channel": {
                        "type": "string",
                        "description": "Channel id within the source (from list_channels)"
                    },
                    "text": {
                        "type": "string",
                        "description": "Message text to send"
                    },
                    "reply_to": {
                        "type": "string",
                        "description": "Optional id of the message to reply to (threads the reply)"
                    }
                },
                "required": ["source", "channel", "text"]
            }
        ),
        Tool(
            name="who_mentioned",
            description="Find messages where someone @-mentioned a person. "
                        "Pass `target` as a person's canonical name or any of their aliases; "
                        "pass `target=\"me\"` for the current user. Optional `by` filters to "
                        "mentions authored by a specific person (also canonical/alias). "
                        "Returns messages sorted newest first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Mentioned person — canonical name, any alias, or 'me' for the current user"
                    },
                    "by": {
                        "type": "string",
                        "description": "Optional: filter to mentions authored by this person (canonical or alias)"
                    },
                    "source": {
                        "type": "string",
                        "description": "Optional source name filter"
                    },
                    "since": {
                        "type": "string",
                        "description": "Optional UTC ISO timestamp lower bound (e.g. '2026-05-21T00:00:00')"
                    },
                    "until": {
                        "type": "string",
                        "description": "Optional UTC ISO timestamp upper bound"
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Maximum messages to return (default: 50)"
                    }
                },
                "required": ["target"]
            }
        ),
        Tool(
            name="upsert_user_alias",
            description="Persist a fact about a person into the user_aliases config — this is "
                        "the agent's durable MEMORY LAYER about people. Use it when you learn "
                        "something that should survive the session: a role change, a team move, "
                        "new project ownership, an additional handle, a confirmed canonical "
                        "name. Creates a new entry if `canonical_name` is unknown; otherwise "
                        "MERGES the provided fields into the existing entry (list fields like "
                        "aliases / responsible_for are unioned uniquely; scalars overwrite). "
                        "Do NOT use this for one-off context you don't expect to need next "
                        "session — that's what conversational memory is for.",
            inputSchema={
                "type": "object",
                "properties": {
                    "canonical_name": {
                        "type": "string",
                        "description": "The person's canonical (display) name. Case-insensitive lookup."
                    },
                    "aliases": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Handles / nicknames the person goes by across sources (added uniquely on merge)"
                    },
                    "internal": {
                        "type": "boolean",
                        "description": ("true = company staff; false = explicit external "
                                        "(e.g. contractor on an internal domain)")
                    },
                    "role": {"type": "string", "description": "Free-form job/function, e.g. 'Backend lead'"},
                    "team": {"type": "string", "description": "Free-form group, e.g. 'Platform'"},
                    "reports_to": {
                        "type": "string",
                        "description": "canonical_name of the manager (free-form; forward references OK)"
                    },
                    "responsible_for": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Channels / project prefixes / issue prefixes this person owns"
                    }
                },
                "required": ["canonical_name"]
            }
        ),
        Tool(
            name="remove_user_alias",
            description="Delete a user_aliases entry by canonical_name (case-insensitive). Use "
                        "when a person leaves or you confirmed an entry is wrong. Refuses to "
                        "touch any entry that matches a my_aliases value (identity is operator "
                        "territory). Returns the removed entry so you can re-create it if you "
                        "made a mistake; for full audit / undo, use `git diff config.yaml`.",
            inputSchema={
                "type": "object",
                "properties": {
                    "canonical_name": {
                        "type": "string",
                        "description": "The canonical_name to remove (case-insensitive)."
                    }
                },
                "required": ["canonical_name"]
            }
        ),
        Tool(
            name="update_user_alias_strings",
            description="Add and/or remove specific alias strings on one existing entry. Use "
                        "when you learn a new nickname or want to retire one — narrower than "
                        "upsert_user_alias which merges multiple fields. Refuses to STEAL an "
                        "alias already owned by a different canonical (will name the owner); "
                        "refuses to empty an entry's aliases list (use remove_user_alias for "
                        "that).",
            inputSchema={
                "type": "object",
                "properties": {
                    "canonical_name": {"type": "string", "description": "The entry to edit (case-insensitive)."},
                    "add":    {"type": "array", "items": {"type": "string"},
                               "description": "Alias strings to append (deduplicated)."},
                    "remove": {"type": "array", "items": {"type": "string"},
                               "description": "Alias strings to drop (case-insensitive)."}
                },
                "required": ["canonical_name"]
            }
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls from Claude.

    Times each dispatch, logs one row to `tool_calls` with a per-tool
    redacted args summary (so the dashboard's MCP-usage panel has a feed).
    Logging failures are swallowed inside `db.log_tool_call` so they NEVER
    break the actual tool response.
    """
    import time as _time
    started = _time.monotonic()
    success = True
    error_text = None
    try:
        return await _dispatch_tool(name, arguments)
    except Exception as e:
        success = False
        error_text = str(e)[:300]
        raise
    finally:
        duration_ms = int((_time.monotonic() - started) * 1000)
        try:
            get_db().log_tool_call(
                tool_name=name,
                args_summary=_args_summary_for(name, arguments),
                duration_ms=duration_ms,
                success=success,
                error=error_text,
            )
        except Exception:
            # Logging is best-effort; never let it block a tool response.
            pass


async def _dispatch_tool(name: str, arguments: dict) -> list[TextContent]:
    """Pure routing — kept separate from call_tool so the logging wrapper stays small."""
    if name == "search_messages":
        return await _search_messages(arguments)
    elif name == "summarize_channel":
        return await _summarize_channel(arguments)
    elif name == "summarize_messages":
        return await _summarize_messages(arguments)
    elif name == "daily_digest":
        return await _daily_digest(arguments)
    elif name == "list_channels":
        return await _list_channels(arguments)
    elif name == "get_new_messages":
        return await _get_new_messages(arguments)
    elif name == "get_thread":
        return await _get_thread(arguments)
    elif name == "find_decisions":
        return await _find_decisions(arguments)
    elif name == "get_stats":
        return await _get_stats(arguments)
    elif name == "get_attached_file":
        return await _get_attached_file(arguments)
    elif name == "get_user_aliases":
        return await _get_user_aliases(arguments)
    elif name == "get_health":
        return await _get_health(arguments)
    elif name == "send_message":
        return await _send_message(arguments)
    elif name == "find_by_issue":
        return await _find_by_issue(arguments)
    elif name == "who_mentioned":
        return await _who_mentioned(arguments)
    elif name == "upsert_user_alias":
        return await _upsert_user_alias(arguments)
    elif name == "remove_user_alias":
        return await _remove_user_alias(arguments)
    elif name == "update_user_alias_strings":
        return await _update_user_alias_strings(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ── Per-tool args_summary redaction (TASK-026) ───────────────────────────────
# Stored verbatim in tool_calls.args_summary so the dashboard can show
# "agent searched for 'deploy' 18 times today" without leaking message bodies.
# Tools that handle sensitive payloads explicitly project them down.

def _truncate(s, limit: int = 120) -> str:
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    return s if len(s) <= limit else s[:limit] + "…"


_TOOL_ARG_PROJECTORS = {
    # NEVER store the message body — only its shape.
    "send_message": lambda a: {
        "source": a.get("source"),
        "channel": a.get("channel"),
        "text_len": len(a.get("text") or ""),
        "has_reply_to": bool(a.get("reply_to")),
    },
    # Query is fine to keep (operator search terms aren't secret), but cap it.
    "search_messages": lambda a: {
        "query": _truncate(a.get("query") or "", 120),
        "mode": a.get("mode"),
        "source": a.get("source"),
        "channel": a.get("channel"),
        "limit": a.get("limit"),
        "mentions_me": bool(a.get("mentions_me")),
    },
    # Write tools — store only the canonical target, not the field payload.
    "upsert_user_alias": lambda a: {"canonical_name": a.get("canonical_name")},
    "remove_user_alias": lambda a: {"canonical_name": a.get("canonical_name")},
    "update_user_alias_strings": lambda a: {"canonical_name": a.get("canonical_name")},
    # Threads / decisions / digests: target id / limits, no content.
    "get_thread": lambda a: {"thread_id": a.get("thread_id"), "limit": a.get("limit")},
    "find_decisions": lambda a: {"hours": a.get("hours"), "limit": a.get("limit"),
                                 "topic": _truncate(a.get("topic"), 60)},
    "summarize_channel": lambda a: {"channel": a.get("channel"), "hours": a.get("hours")},
    "summarize_messages": lambda a: {"hours": a.get("hours"), "since": a.get("since")},
    "daily_digest": lambda a: {"hours": a.get("hours")},
}


def _args_summary_for(tool_name: str, args: dict) -> str:
    """Project + JSON-encode args for the tool_calls row. Falls back to verbatim."""
    args = args or {}
    projector = _TOOL_ARG_PROJECTORS.get(tool_name)
    payload = projector(args) if projector else dict(args)
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps({"_unserializable": True})


async def _search_messages(args: dict) -> list[TextContent]:
    """Search messages by keyword or semantic similarity."""
    mode = args.get("mode", "semantic")
    limit = args.get("limit", 20)
    filter_mentions_me = args.get("mentions_me", False)

    if mode == "semantic":
        # Semantic search using vector embeddings
        hits = get_vs().semantic_search(
            query=args["query"],
            n_results=limit,
            source=args.get("source"),
            channel=args.get("channel"),
            since=args.get("since")
        )

        if not hits:
            return [TextContent(type="text", text="No semantic matches found.")]

        config = get_config()
        tz = get_display_tz(config)
        db_rows = {r["id"]: r for r in get_db().get_by_ids([h["id"] for h in hits])}
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
        # Keyword search using SQLite LIKE
        rows = get_db().search(
            query=args["query"],
            source=args.get("source"),
            channel=args.get("channel"),
            since=args.get("since"),
            mentions_me=filter_mentions_me,
            limit=limit
        )

        if not rows:
            return [TextContent(type="text", text="No keyword matches found.")]

        results = [format_message(r, config=get_config()) for r in rows]

    return [TextContent(type="text", text="\n".join(results))]


async def _summarize_channel(args: dict) -> list[TextContent]:
    """Get messages from a channel for summarization."""
    since_str = args.get("since")
    if since_str:
        since = since_str
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()

    rows = get_db().search(
        channel=args["channel"],
        since=since,
        limit=args.get("max_msgs", 100)
    )

    if not rows:
        return [TextContent(
            type="text",
            text=f"No messages found in channel '{args['channel']}' for the specified period."
        )]

    # Format messages as a blob for summarization
    config = get_config()
    tz = get_display_tz(config)
    parts = [f"## Messages from {args['channel']} (since {since})\n"]
    # Prepend the channel description as context (first match across sources is fine; name
    # collisions are rare and the description still belongs to one of them).
    desc_lookup = _build_channel_desc_lookup(get_db())
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


async def _daily_digest(args: dict) -> list[TextContent]:
    """Generate a digest of messages from the last 24 hours."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    rows = get_db().search(
        source=args.get("source"),
        since=since,
        limit=500
    )

    if not rows:
        return [TextContent(
            type="text",
            text="No messages in the last 24 hours."
        )]

    # Group by (source, channel)
    by_channel: dict[tuple, list] = {}
    for r in rows:
        key = (r['source'], r['channel'])
        by_channel.setdefault(key, []).append(r)

    config = get_config()
    parts = ["# Daily Message Digest\n"]
    for (src, ch), msgs in by_channel.items():
        parts.append(f"\n## [{src}] {ch} ({len(msgs)} messages)\n")
        for m in msgs[:10]:
            url = make_message_url(m, config)
            link = f" 🔗 {url}" if url else ""
            parts.append(f"- **{m['sender']}**{_ext_marker(m)}: {m['text'][:120]}{link}")
        if len(msgs) > 10:
            parts.append(f"- ... and {len(msgs) - 10} more")

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

    rows = get_db().search(
        source=args.get("source"),
        channel=args.get("channel"),
        since=since,
        limit=1000
    )

    if not rows:
        return [TextContent(
            type="text",
            text=f"No messages in the last {time_desc}."
        )]

    # Group by (source, channel)
    by_channel: dict[tuple, list] = {}
    for r in rows:
        key = (r['source'], r['channel'])
        by_channel.setdefault(key, []).append(r)

    config = get_config()
    max_per_channel = args.get("max_messages", 100)
    desc_lookup = _build_channel_desc_lookup(get_db())
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


async def _list_channels(args: dict) -> list[TextContent]:
    """List known channels (id + name + description) from the DB for discovery."""
    rows = get_db().list_channels(source=args.get("source"))
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


def _build_live_connector(source: str, src_cfg: dict, text_extensions: set):
    """Build a read-only connector (db=None) for a live fetch, or None if unsupported."""
    source_type = src_cfg.get("type")
    db_callback = get_db().get_channel
    youtrack_cfg = get_config().get("youtrack") or {}
    if source_type == "mattermost":
        return MattermostConnector(
            base_url=src_cfg["url"], token=src_cfg["token"], source_name=source,
            db_callback=db_callback, db=None, text_extensions=text_extensions,
            youtrack_cfg=youtrack_cfg,
        )
    if source_type == "pachca":
        return PachcaConnector(
            access_token=src_cfg["token"], source_name=source,
            db_callback=db_callback, db=None, text_extensions=text_extensions,
            youtrack_cfg=youtrack_cfg,
        )
    if source_type == "telegram":
        return TelegramConnector(
            bot_token=src_cfg["token"], source_name=source,
            db_callback=db_callback, db=None, text_extensions=text_extensions,
            youtrack_cfg=youtrack_cfg,
        )
    if source_type == "email":
        # For send_message (drafts) we need a writable connector — the live-fetch
        # path punts before reaching this constructor.
        return EmailConnector(
            host=src_cfg["host"], port=src_cfg.get("port", 993),
            username=src_cfg["username"], password=src_cfg["password"],
            source_name=source,
            folders=src_cfg.get("folders"),
            channel_names=src_cfg.get("channel_names"),
            drafts_folder=src_cfg.get("drafts_folder", "Drafts"),
            from_address=src_cfg.get("from_address"),
            db_callback=db_callback, db=get_db(), text_extensions=text_extensions,
            youtrack_cfg=youtrack_cfg,
        )
    return None


async def _get_new_messages(args: dict) -> list[TextContent]:
    """Fetch messages newer than what's in the DB for a channel, live from the source."""
    source = args.get("source", "").strip()
    channel = args.get("channel", "").strip()
    limit = args.get("limit", 50)
    if not source or not channel:
        return [TextContent(type="text", text="Both 'source' and 'channel' are required.")]

    config = get_config()
    src_cfg = config.get("sources", {}).get(source)
    if not src_cfg:
        return [TextContent(type="text",
                            text=f"Unknown source '{source}'. Use get_stats to list sources.")]

    if src_cfg.get("type") == "email":
        return [TextContent(type="text",
                            text=f"Live fetch is not supported for IMAP sources ('{source}'); "
                                 f"the next scheduled ingest will pick up new email.")]

    text_extensions = set(config.get("text_extensions", [".txt", ".md", ".log", ".json", ".lst"]))
    connector = _build_live_connector(source, src_cfg, text_extensions)
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

    db = get_db()
    messages = [m for m in messages if not db.exists(m["id"])]
    if not messages:
        return [TextContent(type="text",
                            text=f"No new messages in '{channel}' ({source}) since last ingest.")]

    # Classify internal/external live (these aren't ingested yet, so they carry no flag).
    user_aliases, my_aliases = get_aliases(config)
    resolver = AliasResolver(user_aliases, my_aliases,
                             internal_domains=get_internal_domains(config))
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


def _sent_message_url(source: str, stype: str, channel: str, message_id,
                      connector, config: dict) -> Optional[str]:
    """Best-effort permalink for a just-sent message, reusing make_message_url."""
    if message_id in (None, ""):
        return None
    raw: dict = {}
    msg: dict = {"source": source, "raw": raw}
    if stype == "mattermost":
        resolved = connector._resolve_channel(channel)
        if resolved:
            raw["team_name"] = resolved.get("team_name", "")
            raw["post_id"] = str(message_id)
            msg["channel_name"] = resolved.get("name", "")
    elif stype == "telegram":
        s = str(channel)
        raw["chat_type"] = "supergroup" if s.startswith("-100") else "private"
        raw["chat_id"] = s[4:] if s.startswith("-100") else s.lstrip("-")
        raw["message_id"] = str(message_id)
    elif stype == "pachca":
        raw["chat_id"] = str(channel)
        raw["message_id"] = str(message_id)
    return make_message_url(msg, config)


def _record_sent(source: str, channel: str, reply_to, text: str,
                 success: bool, message_id=None, error: str = None) -> None:
    """Log a send attempt (success or failure) to the sent_messages audit table."""
    try:
        get_db().record_sent_message({
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

    config = get_config()
    src_cfg = config.get("sources", {}).get(source)
    if not src_cfg:
        return [TextContent(type="text",
                            text=f"Unknown source '{source}'. Use get_stats to list sources.")]

    if src_cfg.get("allow_send") is not True:
        return [TextContent(type="text",
                            text=f"Sending is disabled for source '{source}'. "
                                 f"Set allow_send: true in its config to enable.")]

    stype = src_cfg.get("type")
    connector = _build_live_connector(source, src_cfg, set())
    if connector is None:
        return [TextContent(type="text", text=f"Source type '{stype}' is not supported.")]

    try:
        connector.connect()
        if stype == "mattermost":
            message_id = connector.send_message(channel, text, root_id=reply_to)
        elif stype == "telegram":
            row = get_db().get_channel_row(source, channel)
            extra = row.get("extra") if row else None
            bcid = extra.get("business_connection_id") if isinstance(extra, dict) else None
            message_id = connector.send_message(channel, text,
                                                reply_to=int(reply_to) if reply_to else None,
                                                business_connection_id=bcid)
        elif stype == "pachca":
            message_id = connector.send_message(channel, text,
                                                parent_message_id=int(reply_to) if reply_to else None)
        elif stype == "email":
            # Email = draft in Drafts folder, NOT a live send. `channel` is unused
            # by the connector for drafts; reply_to is the parent Message-ID.
            message_id = connector.send_message(channel, text, reply_to=reply_to)
        else:
            return [TextContent(type="text", text=f"Source type '{stype}' is not supported.")]
        url = _sent_message_url(source, stype, channel, message_id, connector, config)
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


async def _get_thread(args: dict) -> list[TextContent]:
    """Reconstruct a conversation thread (root + replies) ordered by timestamp."""
    thread_id = args.get("thread_id", "").strip()
    if not thread_id:
        return [TextContent(type="text", text="thread_id is required.")]

    rows = get_db().get_thread(
        thread_id,
        channel=args.get("channel"),
        limit=args.get("limit", 50),
    )
    if not rows:
        return [TextContent(type="text", text=f"No thread found for thread_id '{thread_id}'.")]

    config = get_config()
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

    rows = get_db().find_by_issue_id(issue_id, limit=args.get("limit", 50))
    if not rows:
        return [TextContent(type="text",
                            text=f"No messages found referencing '{issue_id}'.")]

    config = get_config()
    lines = [f"# {len(rows)} message(s) referencing {issue_id}\n"]
    lines.extend(format_message(r, config=config) for r in rows)
    return [TextContent(type="text", text="\n".join(lines))]


async def _who_mentioned(args: dict) -> list[TextContent]:
    """Return messages where `target` was @-mentioned. Resolves target/by via aliases."""
    target = (args.get("target") or "").strip()
    if not target:
        return [TextContent(type="text", text="'target' is required ('me' or a name/alias).")]

    config = get_config()
    user_aliases, my_aliases = get_aliases(config)
    resolver = AliasResolver(user_aliases, my_aliases)

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
        rows = get_db().get_mentions_for_identity(
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
        rows = get_db().get_mentions(
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
    db = get_db()
    out: list[str] = []
    seen: set = set()
    for source_name in (config.get("sources") or {}):
        for alias in my_aliases:
            sid = db.find_sender_id_by_username(source_name, alias)
            if sid and sid not in seen:
                seen.add(sid)
                out.append(sid)
    return out


# ── user_aliases write tools (TASK-029) ──────────────────────────────────────

def _alias_edit_guard(config: dict) -> Optional[str]:
    """Return an error message if alias edits are disabled, else None."""
    if not get_alias_edit_settings(config).get("allow_alias_edits", True):
        return ("Alias edits are disabled (top-level `allow_alias_edits: false` in config). "
                "Flip to `true` to enable agent-driven edits.")
    return None


def _is_my_aliases_target(config: dict, canonical_name: str) -> bool:
    """True if `canonical_name` collides (case-insensitive) with any my_aliases value."""
    _, my_aliases = get_aliases(config)
    needle = (canonical_name or "").strip().lower()
    return any((a or "").strip().lower() == needle for a in my_aliases)


def _invalidate_config_cache() -> None:
    """Drop the cached config so the next tool call re-reads from disk."""
    global _config
    _config = None


async def _upsert_user_alias(args: dict) -> list[TextContent]:
    """Create or merge a single user_aliases entry. The MCP write surface."""
    config = get_config()
    err = _alias_edit_guard(config)
    if err:
        return [TextContent(type="text", text=err)]

    canonical = (args.get("canonical_name") or "").strip()
    if not canonical:
        return [TextContent(type="text", text="`canonical_name` is required and must be non-empty.")]
    if _is_my_aliases_target(config, canonical):
        return [TextContent(type="text",
                            text=f"Refusing to edit a my_aliases canonical ({canonical!r}); "
                                 f"identity is operator territory.")]

    settings = get_alias_edit_settings(config)
    # Caps — refuse before touching the file.
    aliases = args.get("aliases") or []
    if not isinstance(aliases, list):
        return [TextContent(type="text", text="`aliases` must be a list of strings.")]
    if len(aliases) > settings["max_aliases_per_entry"]:
        return [TextContent(type="text",
                            text=f"Too many aliases ({len(aliases)}); cap is "
                                 f"max_aliases_per_entry={settings['max_aliases_per_entry']}.")]
    responsible_for = args.get("responsible_for") or []
    if responsible_for and len(responsible_for) > settings["max_list_fields"]:
        return [TextContent(type="text",
                            text=f"responsible_for too long ({len(responsible_for)}); cap is "
                                 f"max_list_fields={settings['max_list_fields']}.")]

    from cli.alias_writer import (
        load_aliases_yaml, save_aliases_yaml, apply_upsert,
        _user_aliases, find_alias_owner,
    )
    yaml_obj, doc = load_aliases_yaml(_config_path)
    # Cap entries — only for fresh creations (merges don't grow the list).
    needle = canonical.lower()
    is_new = not any(
        isinstance(e, dict) and (e.get("canonical_name") or "").strip().lower() == needle
        for e in _user_aliases(doc)
    )
    if is_new and len(_user_aliases(doc)) >= settings["max_entries"]:
        return [TextContent(type="text",
                            text=f"user_aliases already at the cap of {settings['max_entries']} "
                                 f"entries; remove an entry first or raise max_entries in config.")]

    # Conflict check on alias strings BEFORE the write, so we can name the owner.
    conflicting: list = []
    for a in aliases:
        owner = find_alias_owner(doc, a)
        if owner and owner.strip().lower() != needle:
            conflicting.append((a, owner))
    if conflicting:
        msg = "; ".join(f"{a!r} already owned by {o!r}" for a, o in conflicting)
        return [TextContent(type="text",
                            text=f"Refusing upsert: {msg}. Remove from the other entry first.")]

    payload: dict = {"canonical_name": canonical, "aliases": list(aliases)}
    for f in ("internal", "role", "team", "reports_to", "responsible_for"):
        if f in args and args[f] is not None:
            payload[f] = args[f]

    try:
        result = apply_upsert(doc, payload, conflict_policy="merge")
    except ValueError as e:
        return [TextContent(type="text", text=f"Refusing upsert: {e}")]
    save_aliases_yaml(_config_path, yaml_obj, doc)
    _invalidate_config_cache()
    return [TextContent(type="text", text=json.dumps({"ok": True, "entry": result},
                                                     default=str, ensure_ascii=False))]


async def _remove_user_alias(args: dict) -> list[TextContent]:
    """Delete one user_aliases entry by canonical_name."""
    config = get_config()
    err = _alias_edit_guard(config)
    if err:
        return [TextContent(type="text", text=err)]

    canonical = (args.get("canonical_name") or "").strip()
    if not canonical:
        return [TextContent(type="text", text="`canonical_name` is required and must be non-empty.")]
    if _is_my_aliases_target(config, canonical):
        return [TextContent(type="text",
                            text=f"Refusing to remove a my_aliases canonical ({canonical!r}); "
                                 f"identity is operator territory.")]

    from cli.alias_writer import load_aliases_yaml, save_aliases_yaml, apply_remove
    yaml_obj, doc = load_aliases_yaml(_config_path)
    removed = apply_remove(doc, canonical)
    if removed is None:
        return [TextContent(type="text",
                            text=f"No entry found for canonical_name {canonical!r}.")]
    save_aliases_yaml(_config_path, yaml_obj, doc)
    _invalidate_config_cache()
    return [TextContent(type="text", text=json.dumps({"ok": True, "removed": removed},
                                                     default=str, ensure_ascii=False))]


async def _update_user_alias_strings(args: dict) -> list[TextContent]:
    """Add and/or remove alias strings on one entry."""
    config = get_config()
    err = _alias_edit_guard(config)
    if err:
        return [TextContent(type="text", text=err)]

    canonical = (args.get("canonical_name") or "").strip()
    if not canonical:
        return [TextContent(type="text", text="`canonical_name` is required and must be non-empty.")]
    if _is_my_aliases_target(config, canonical):
        return [TextContent(type="text",
                            text=f"Refusing to edit a my_aliases canonical ({canonical!r}); "
                                 f"identity is operator territory.")]

    add = args.get("add") or []
    remove = args.get("remove") or []
    if not isinstance(add, list) or not isinstance(remove, list):
        return [TextContent(type="text", text="`add` and `remove` must be lists of strings.")]
    if not add and not remove:
        return [TextContent(type="text",
                            text="At least one of `add` or `remove` must be non-empty.")]

    settings = get_alias_edit_settings(config)
    from cli.alias_writer import (
        load_aliases_yaml, save_aliases_yaml, apply_alias_string_change,
    )
    yaml_obj, doc = load_aliases_yaml(_config_path)

    # Cap check: project the resulting alias count and refuse if it would exceed.
    from cli.alias_writer import _find_entry
    _, entry = _find_entry(doc, canonical)
    if entry is None:
        return [TextContent(type="text",
                            text=f"No entry found for canonical_name {canonical!r}.")]
    current = list(entry.get("aliases") or [])
    remove_lc = {r.strip().lower() for r in remove}
    projected = [a for a in current if a.strip().lower() not in remove_lc] + list(add)
    # dedup for accurate count
    seen: set = set()
    projected_unique: list = []
    for a in projected:
        k = a.strip().lower() if isinstance(a, str) else a
        if k in seen:
            continue
        seen.add(k)
        projected_unique.append(a)
    if len(projected_unique) > settings["max_aliases_per_entry"]:
        return [TextContent(type="text",
                            text=f"Would push aliases to {len(projected_unique)} entries; cap is "
                                 f"max_aliases_per_entry={settings['max_aliases_per_entry']}.")]

    try:
        result = apply_alias_string_change(doc, canonical, add=add, remove=remove)
    except ValueError as e:
        return [TextContent(type="text", text=f"Refusing update: {e}")]
    save_aliases_yaml(_config_path, yaml_obj, doc)
    _invalidate_config_cache()
    return [TextContent(type="text", text=json.dumps({"ok": True, "entry": result},
                                                     default=str, ensure_ascii=False))]


async def _find_decisions(args: dict) -> list[TextContent]:
    """Find messages that look like decisions or action items."""
    # Construct decision-oriented query
    topic = args.get("topic", "")
    query = f"decision conclusion agreed action item todo {topic}"

    hits = get_vs().semantic_search(
        query=query,
        n_results=30,
        source=None,
        channel=args.get("channel"),
        since=args.get("since")
    )

    if not hits:
        return [TextContent(type="text", text="No decisions or action items found.")]

    config = get_config()
    tz = get_display_tz(config)
    db_rows = {r["id"]: r for r in get_db().get_by_ids([h["id"] for h in hits])}
    results = []
    for h in hits:
        row = db_rows.get(h["id"])
        if row:
            url = make_message_url(row, config)
            link = f" 🔗 {url}" if url else ""
            results.append(
                f"[{format_timestamp(row['timestamp'], tz)}] "
                f"[{row['source']}/{row['channel']}] "
                f"**{row['sender']}**{_ext_marker(row)}: {row['text']}{link}"
            )
        else:
            results.append(
                f"[{format_timestamp(h['metadata'].get('timestamp', ''), tz)}] "
                f"[{h['metadata'].get('source', '')}/{h['metadata'].get('channel', '')}] "
                f"**{h['metadata'].get('sender', '')}**: {h['text']}"
            )

    return [TextContent(type="text", text="# Decisions & Action Items\n\n" + "\n".join(results))]


async def _get_stats(args: dict) -> list[TextContent]:
    """Get message statistics."""
    db = get_db()
    vs = get_vs()
    config = get_config()

    total = db.count()
    vector_count = vs.count()

    parts = [
        "# Message Statistics",
        f"- Total messages in SQLite: {total}",
    ]
    for src_name, src_cfg in config.get("sources", {}).items():
        count = db.count(source=src_name)
        src_type = src_cfg.get("type", "?")
        enabled = "" if src_cfg.get("enabled", True) else " (disabled)"
        parts.append(f"  - {src_name} [{src_type}]{enabled}: {count}")
    parts.append(f"- Total messages in Vector Store: {vector_count}")

    return [TextContent(type="text", text="\n".join(parts))]


def _find_cached_file(cache_dir: Path, file_id: str) -> Optional[Path]:
    """Return the first cached file whose stem matches file_id (any extension)."""
    try:
        for p in cache_dir.iterdir():
            if p.stem == file_id:
                return p
    except OSError:
        pass
    return None


def _serve_file_content(content: bytes, ext: str, text_extensions: set) -> str:
    import base64
    if ext in text_extensions or not ext:
        try:
            return content.decode("utf-8", errors="ignore")
        except Exception:
            pass
    b64 = base64.b64encode(content).decode("ascii")
    return f"[base64]:{b64}"


def _try_telegram_download(token: str, file_id: str, cache_dir: Path, text_extensions: set) -> Optional[str]:
    import requests
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            return None
        file_path_str = data["result"]["file_path"]
        ext = Path(file_path_str).suffix.lower()

        file_resp = requests.get(
            f"https://api.telegram.org/file/bot{token}/{file_path_str}",
            timeout=60,
        )
        file_resp.raise_for_status()
        content = file_resp.content

        (cache_dir / f"{file_id}{ext}").write_bytes(content)
        return _serve_file_content(content, ext, text_extensions)
    except Exception:
        return None


def _try_mattermost_download(base_url: str, token: str, file_id: str,
                             cache_dir: Path, text_extensions: set) -> Optional[str]:
    import requests
    import mimetypes
    try:
        url = f"{base_url.rstrip('/')}/api/v4/files/{file_id}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        resp.raise_for_status()
        content = resp.content

        content_type = resp.headers.get("Content-Type", "")
        mime_type = content_type.split(";")[0].strip()
        ext = mimetypes.guess_extension(mime_type) or ""

        cache_path = cache_dir / (file_id + ext) if ext else cache_dir / file_id
        cache_path.write_bytes(content)
        return _serve_file_content(content, ext, text_extensions)
    except Exception:
        return None


async def _get_attached_file(args: dict) -> list[TextContent]:
    """Get content of an attached file — cache first, then Telegram, then Mattermost."""
    file_id = args.get("file_id", "").strip()
    if not file_id:
        return [TextContent(type="text", text="file_id is required.")]

    cache_dir = Path("data/file_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    config = get_config()
    text_extensions = set(config.get("text_extensions", [".txt", ".md", ".log", ".json", ".lst"]))

    # 1. Cache hit (any extension — covers both Mattermost and Telegram text files)
    cached = _find_cached_file(cache_dir, file_id)
    if cached:
        try:
            return [TextContent(type="text", text=_serve_file_content(
                cached.read_bytes(), cached.suffix.lower(), text_extensions
            ))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error reading cached file: {e}")]

    # 2. Try each enabled Telegram source
    for src in config.get("sources", {}).values():
        if src.get("type") == "telegram" and src.get("enabled", True) and src.get("token"):
            result = _try_telegram_download(src["token"], file_id, cache_dir, text_extensions)
            if result is not None:
                return [TextContent(type="text", text=result)]

    # 3. Try each enabled Mattermost source
    for src in config.get("sources", {}).values():
        if (src.get("type") == "mattermost" and src.get("enabled", True)
                and src.get("url") and src.get("token")):
            result = _try_mattermost_download(src["url"], src["token"],
                                              file_id, cache_dir, text_extensions)
            if result is not None:
                return [TextContent(type="text", text=result)]

    return [TextContent(type="text", text=f"File '{file_id}' not found in cache or any configured source.")]


def _build_aliases_text(config: dict) -> str:
    """Format user aliases for MCP output (extracted for testability).

    Each group renders as a primary line "**Name** [internal] — Role (Team): aliases"
    with role / team folded inline where present. ``reports_to`` and ``responsible_for``
    land on an indented follow-on line so the primary line stays readable.
    """
    user_aliases, my_aliases = get_aliases(config)
    parts = []
    if my_aliases:
        parts.append("## Current User Aliases (my_aliases)\n" + ", ".join(my_aliases))
    if user_aliases:
        parts.append("## Known Users")
        for group in user_aliases:
            name = group.get("canonical_name", "")
            aliases = ", ".join(group.get("aliases", []))
            tag = " [internal]" if group.get("internal") else ""

            role = (group.get("role") or "").strip()
            team = (group.get("team") or "").strip()
            role_bit = ""
            if role and team:
                role_bit = f" — {role} ({team})"
            elif role:
                role_bit = f" — {role}"
            elif team:
                role_bit = f" — ({team})"

            parts.append(f"- **{name}**{tag}{role_bit}: {aliases}")

            extras = []
            reports_to = (group.get("reports_to") or "").strip()
            if reports_to:
                extras.append(f"reports to {reports_to}")
            responsible_for = group.get("responsible_for")
            if responsible_for:
                if isinstance(responsible_for, list):
                    rf = ", ".join(str(x) for x in responsible_for if str(x).strip())
                else:
                    rf = str(responsible_for).strip()
                if rf:
                    extras.append(f"responsible for: {rf}")
            if extras:
                parts.append("  " + "; ".join(extras))
    if not parts:
        parts.append("No aliases configured.")
    return "\n".join(parts)


async def _get_user_aliases(_args: dict) -> list[TextContent]:
    """Return all configured user aliases and indicate which belong to the current user."""
    return [TextContent(type="text", text=_build_aliases_text(get_config()))]


async def _get_health(_args: dict) -> list[TextContent]:
    from pipeline.health import build_health_report, format_health_text
    config = get_config()
    report = build_health_report(get_db(), config)
    return [TextContent(type="text", text=format_health_text(report, config))]


def parse_args():
    """Parse command line arguments."""
    import argparse
    parser = argparse.ArgumentParser(description="Memorandum MCP Server")
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config.yaml (default: config.yaml)"
    )
    return parser.parse_args()


async def main():
    """Main entry point for MCP server."""
    global _config_path
    args = parse_args()
    _config_path = args.config

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
