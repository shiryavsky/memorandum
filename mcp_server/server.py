"""MCP server entry point — wires tool schemas, dispatch, and shared state.

Tool **implementations** live under :mod:`mcp_server.tools` (one module per
domain — search / digests / channels / threads / identity / files / info).
Tool **schemas** (what Claude sees in tool introspection) live in
:mod:`mcp_server.schemas`. Tool **call args projection** for the audit log
lives in :mod:`mcp_server.projectors`.

This module owns:
- The :class:`mcp.server.Server` instance and its two decorated entry
  points (``list_tools`` and ``call_tool``).
- Shared singletons (``_db``, ``_vs``, ``_config``, ``_resolver``) and
  the accessors that build them lazily. Tools import this module as
  ``from mcp_server import server as _srv`` and call ``_srv.get_db()``
  at runtime so ``unittest.mock.patch("mcp_server.server.get_db")``
  keeps working through the indirection.
- ``_build_live_connector`` — kept here (not in ``tools/channels.py``)
  because existing tests patch it at this path.
- ``_invalidate_config_cache`` — touches this module's globals; can't
  live elsewhere.
- The ``main`` coroutine that runs the stdio loop.

Tests historically imported many tool handlers from ``mcp_server.server``.
We re-export the same names below so those imports keep working without
churn.
"""
import sys
from pathlib import Path

# Make the project root importable when this file is invoked directly
# (e.g. via the MCP Inspector or `python mcp_server/server.py`). Must run
# BEFORE any `from connectors. / pipeline. / storage. / config` imports.
sys.path.insert(0, str(Path(__file__).parent.parent))

# When launched as `__main__` (script invocation or `python -m
# mcp_server.server`), self-register under the canonical name. The
# `tools.channels -> from mcp_server import server as _srv` import
# below otherwise re-executes this file under `mcp_server.server` and
# hits the half-initialized `from mcp_server.tools import TOOL_HANDLERS`
# below — the load-order trick in this module's docstring only works
# when server.py is *first* loaded under its real name.
if __name__ == "__main__":
    sys.modules.setdefault("mcp_server.server", sys.modules[__name__])

import asyncio
from typing import Optional

from mcp.types import TextContent, Tool
from mcp.server import Server
from mcp.server.stdio import stdio_server

from config import (
    get_aliases,
    get_internal_domains,
    load_config,
)
from connectors.factory import build_connector
from mcp_server.projectors import TOOL_ARG_PROJECTORS, args_summary_for
from mcp_server.schemas import tool_schemas
from pipeline.alias_resolver import AliasResolver
# Re-imported here so test imports like
# `from mcp_server.server import format_message` keep working.
from pipeline.format import (  # noqa: F401 — public re-export
    ext_marker as _ext_marker,
    format_message,
    format_timestamp,
    get_display_tz,
    make_message_url,
)
from storage.db import Database
from storage.vector_store import VectorStore


# Initialize server
app = Server("message-collector")

# Global instances (initialized on first use)
_db: Optional[Database] = None
_vs: Optional[VectorStore] = None
_config: Optional[dict] = None
_resolver: Optional[AliasResolver] = None
_resolver_config_id: Optional[int] = None  # id() of the config dict the resolver was built from
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


def get_resolver() -> AliasResolver:
    """Get or build the AliasResolver from the current config.

    The cache key is ``id(config)`` rather than a bare nullable flag — when
    ``_invalidate_config_cache`` swaps the dict (alias-write tools do this
    after editing the YAML) the resolver rebuilds without anyone having
    to remember to null it out. Same mechanic keeps tests honest: when a
    test patches ``get_config`` to return a different mock dict, the
    resolver follows.
    """
    global _resolver, _resolver_config_id
    config = get_config()
    if _resolver is None or _resolver_config_id != id(config):
        user_aliases, my_aliases = get_aliases(config)
        _resolver = AliasResolver(
            user_aliases, my_aliases,
            internal_domains=get_internal_domains(config),
        )
        _resolver_config_id = id(config)
    return _resolver


def _invalidate_config_cache() -> None:
    """Drop the cached config so the next tool call re-reads from disk.

    Also resets the cached Database and VectorStore handles — they were
    constructed from ``_config["sqlite_path"]`` / ``_config["chroma_path"]``,
    and if those paths changed in the new config the live handles would
    still point at the old files.
    """
    global _config, _db, _vs, _resolver, _resolver_config_id
    if _db is not None:
        try:
            _db.close()
        except Exception:
            pass
    _config = None
    _db = None
    _vs = None
    _resolver = None
    _resolver_config_id = None


def _build_live_connector(source: str, src_cfg: dict, text_extensions: set):
    """Build a connector for a live-fetch / send tool, or None if unsupported.

    Passes the live DB handle through. Connectors guard their own writes
    during read-only paths (`fetch_new` etc.); the handle is here so
    send-time lookups can resolve their own platform glue without leaking
    into the dispatcher (Telegram's business_connection_id, Email's parent
    message lookup).

    Kept on this module (not in ``tools/channels.py``) because existing
    tests patch it at ``mcp_server.server._build_live_connector``.
    """
    return build_connector(
        source_type=src_cfg.get("type"),
        source_name=source,
        src_cfg=src_cfg,
        db=get_db(),
        db_callback=get_db().get_channel,
        text_extensions=text_extensions,
        youtrack_cfg=get_config().get("youtrack") or {},
    )


# Re-export the projector + summarizer for back-compat with existing tests.
_args_summary_for = args_summary_for
_TOOL_ARG_PROJECTORS = TOOL_ARG_PROJECTORS


# Tool handler imports — done AFTER the accessors above are defined so the
# circular `mcp_server.tools.<x> -> mcp_server.server` import sees a
# partially-loaded but functional server module.
from mcp_server.tools import (  # noqa: E402
    TOOL_HANDLERS,
    _build_aliases_text,        # noqa: F401 — test re-exports below
    _build_channel_desc_lookup,  # noqa: F401
    _find_by_issue,             # noqa: F401
    _find_cached_file,          # noqa: F401
    _get_attached_file,         # noqa: F401
    _get_health,                # noqa: F401
    _get_new_messages,          # noqa: F401
    _get_stats,                 # noqa: F401
    _get_thread,                # noqa: F401
    _get_user_aliases,          # noqa: F401
    _list_channels,             # noqa: F401
    _remove_user_alias,         # noqa: F401
    _search_messages,           # noqa: F401
    _send_message,              # noqa: F401
    _serve_file_content,        # noqa: F401
    _short_description,         # noqa: F401
    _summarize_channel,         # noqa: F401
    _summarize_messages,        # noqa: F401
    _try_mattermost_download,   # noqa: F401
    _try_telegram_download,     # noqa: F401
    _update_user_alias_strings,  # noqa: F401
    _upsert_user_alias,         # noqa: F401
    _who_mentioned,             # noqa: F401
)
_TOOL_HANDLERS = TOOL_HANDLERS


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available MCP tools."""
    return tool_schemas()


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
                args_summary=args_summary_for(name, arguments),
                duration_ms=duration_ms,
                success=success,
                error=error_text,
            )
        except Exception:
            # Logging is best-effort; never let it block a tool response.
            pass


async def _dispatch_tool(name: str, arguments: dict) -> list[TextContent]:
    """Pure routing — kept separate from call_tool so the logging wrapper stays small."""
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    return await handler(arguments)


def parse_args():
    """Parse command line arguments."""
    import argparse
    parser = argparse.ArgumentParser(description="Memorandum MCP Server")
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
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
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
