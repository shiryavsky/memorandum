"""Tool schemas for the Memorandum MCP server.

These are the Tool() declarations the MCP SDK ships to Claude when it
introspects the server — the contract that tells the agent what tools
exist and what arguments they accept. Kept in its own module so the
server entry point stays focused on wiring, and so adding/editing a
tool description does not require scrolling past every handler.
"""
from mcp.types import Tool


def tool_schemas() -> list[Tool]:
    """Return the full list of MCP Tool declarations."""
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
                "are served from cache. Downloads and caches on first access. "
                "Returns a JSON object with fields: "
                "`file_path` (absolute path to the cached file on disk — pass directly to vision_analyze), "
                "`size` (bytes), `content_type` (MIME type), and `content` (decoded text for text files, "
                "`[base64]:<data>` for binary). On error, returns a plain-text error message instead of JSON."
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
