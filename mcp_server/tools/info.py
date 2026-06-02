"""get_stats / get_health — small introspection tools."""
from mcp.types import TextContent

from mcp_server import server as _srv


async def _get_stats(args: dict) -> list[TextContent]:
    """Get message statistics."""
    db = _srv.get_db()
    vs = _srv.get_vs()
    config = _srv.get_config()

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


async def _get_health(_args: dict) -> list[TextContent]:
    """Return ingest-run status, per-source freshness, recorded errors."""
    from pipeline.health import build_health_report, format_health_text
    config = _srv.get_config()
    report = build_health_report(_srv.get_db(), config)
    return [TextContent(type="text", text=format_health_text(report, config))]
