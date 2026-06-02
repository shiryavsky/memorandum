"""Canonical message renderer + timestamp / permalink helpers.

Originally lived in `mcp_server/server.py`. Lifted here so the dashboard
and any future exporter can render messages the same way the MCP server
shows them to Claude. `mcp_server.server` re-imports the public names so
existing test imports keep working.
"""
import json
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo


def get_display_tz(config: Optional[dict]) -> ZoneInfo:
    """Return the configured display timezone (falls back to UTC)."""
    if config is None:
        return ZoneInfo("UTC")
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


def ext_marker(msg: dict) -> str:
    """Return ' [external]' for external senders, '' otherwise.

    Missing `internal` (e.g. live messages not yet ingested) is treated as
    internal so we never mislabel a sender whose status we don't know.
    """
    return "" if msg.get("internal", 1) else " [external]"


def format_message(msg: dict, max_length: int = 200, config: Optional[dict] = None,
                   show_thread: bool = True) -> str:
    """Format a message for display.

    Args:
        msg: Message dictionary
        max_length: Maximum text length before truncation
        config: Optional config dict; when provided, appends a permalink
        show_thread: When True (default), appends a `🧵 thread:<id>` marker
            for replies — useful for the agent to discover thread context.

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

    line = f"[{timestamp}] [{source}/{channel}] {sender}{ext_marker(msg)}: {text}"

    thread_id = msg.get("thread_id")
    if show_thread and thread_id:
        line += f" 🧵 thread:{thread_id}"

    if config is not None:
        url = make_message_url(msg, config)
        if url:
            line += f" 🔗 {url}"

    return line
