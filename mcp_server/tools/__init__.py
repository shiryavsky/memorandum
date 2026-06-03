"""MCP tool handler implementations, split by domain.

Each submodule owns one cohesive surface (search, digests, channel/messaging,
threads, identity, files, info). The flat ``TOOL_HANDLERS`` mapping below is
the dispatcher's source of truth — server.py imports it and routes by name.

Tool modules access server-level state (db / config / resolver / live
connector builder) via ``from mcp_server import server as _srv`` and
``_srv.get_db()`` at call time. The runtime attribute lookup means
``unittest.mock.patch("mcp_server.server.get_db")`` keeps working — the
patch swaps the attribute on the server module, and tools read through
that same module reference.
"""
from .channels import (
    _get_new_messages,
    _list_channels,
    _record_sent,
    _send_message,
)
from .digests import (
    _build_channel_desc_lookup,
    _short_description,
    _summarize_channel,
    _summarize_messages,
    MAX_DESCRIPTION_LEN,
)
from .files import (
    _find_cached_file,
    _get_attached_file,
    _serve_file_content,
    _try_mattermost_download,
    _try_telegram_download,
)
from .identity import (
    _alias_edit_guard,
    _build_aliases_text,
    _get_user_aliases,
    _is_my_aliases_target,
    _remove_user_alias,
    _update_user_alias_strings,
    _upsert_user_alias,
)
from .info import _get_health, _get_stats
from .search import _search_messages
from .threads import (
    _find_by_issue,
    _get_thread,
    _resolve_my_sender_ids,
    _who_mentioned,
)


TOOL_HANDLERS = {
    "search_messages":           _search_messages,
    "summarize_channel":         _summarize_channel,
    "summarize_messages":        _summarize_messages,
    "list_channels":             _list_channels,
    "get_new_messages":          _get_new_messages,
    "get_thread":                _get_thread,
    "get_stats":                 _get_stats,
    "get_attached_file":         _get_attached_file,
    "get_user_aliases":          _get_user_aliases,
    "get_health":                _get_health,
    "send_message":              _send_message,
    "find_by_issue":             _find_by_issue,
    "who_mentioned":             _who_mentioned,
    "upsert_user_alias":         _upsert_user_alias,
    "remove_user_alias":         _remove_user_alias,
    "update_user_alias_strings": _update_user_alias_strings,
}
