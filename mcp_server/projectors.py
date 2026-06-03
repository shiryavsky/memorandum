"""Per-tool argument projectors for the `tool_calls` audit log.

Every MCP tool call gets one row in `tool_calls` so the dashboard can show
which tools the agent uses and how often. The args column is JSON, but
storing the raw `arguments` dict would leak message bodies (send_message),
search queries (fine, but could be sensitive in some configs), or alias
payloads. Each entry in `TOOL_ARG_PROJECTORS` reduces the args to a shape
safe-and-useful for the dashboard — typically counts and target ids,
never content.

Tools without a projector entry get their args verbatim — those are
low-sensitivity surfaces (`list_channels`, `get_health`, …) where the
full args dict is fine.
"""
import json


def _truncate(s, limit: int = 120) -> str:
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    return s if len(s) <= limit else s[:limit] + "…"


TOOL_ARG_PROJECTORS = {
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
    # Threads / digests: target id / limits, no content.
    "get_thread": lambda a: {"thread_id": a.get("thread_id"), "limit": a.get("limit")},
    "summarize_channel": lambda a: {"channel": a.get("channel"), "hours": a.get("hours")},
    "summarize_messages": lambda a: {
        "hours": a.get("hours"),
        "days": a.get("days"),
        "since": a.get("since"),
        "source": a.get("source"),
        "channel": a.get("channel"),
    },
}


def args_summary_for(tool_name: str, args: dict) -> str:
    """Project + JSON-encode args for the tool_calls row. Falls back to verbatim."""
    args = args or {}
    projector = TOOL_ARG_PROJECTORS.get(tool_name)
    payload = projector(args) if projector else dict(args)
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps({"_unserializable": True})
