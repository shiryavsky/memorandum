"""Construct a connector instance from a source config block.

Until this module existed, `pipeline/ingest.py` and `mcp_server/server.py`
each contained a four-arm `if source_type == "..."` ladder that built the
same four connectors with nearly-identical kwargs. Bug fixes had to land
in two places; a new connector required a fourth branch in both. One
factory funnels both call sites.

The shape difference between the call sites is intentional:
- Ingest passes the writable `db` handle and a `source_filters` dict
  (skip / only / skip_folders).
- MCP passes `db=None` for read-only live-fetch tools — and `db=<live>`
  for the email drafts path. The filter block is irrelevant there
  because the MCP surface doesn't honor it (an operator skip rule
  shouldn't hide a live answer from the agent).

Returning ``None`` for an unknown ``source_type`` is the documented
"skip with a warning" contract that both call sites already implement.
"""
from typing import Any, Optional

from .email_connector import EmailConnector
from .mattermost_connector import MattermostConnector
from .pachca_connector import PachcaConnector
from .telegram_connector import TelegramConnector


def build_connector(
    source_type: str,
    source_name: str,
    src_cfg: dict,
    *,
    db: Any,
    db_callback: Any,
    text_extensions: set,
    attachments_path: str = "data/attachments",
    youtrack_cfg: Optional[dict] = None,
    source_filters: Optional[dict] = None,
):
    """Build a connector or return None if `source_type` is unknown.

    `db_callback` is the read-only sync-state lookup (typically
    `db.get_channel`). Ingest sets it to None on a forced full-rescan.

    `source_filters` carries the source's `filters:` block from config —
    skip_channels / only_channels / skip_folders. MCP callers can omit
    it entirely (read-only live fetches don't apply operator filters).
    """
    sf = source_filters or {}
    common = dict(
        source_name=source_name,
        db_callback=db_callback,
        db=db,
        text_extensions=text_extensions,
        youtrack_cfg=youtrack_cfg or {},
    )

    if source_type == "mattermost":
        return MattermostConnector(
            base_url=src_cfg["url"], token=src_cfg["token"],
            skip_channels=sf.get("skip_channels"),
            only_channels=sf.get("only_channels"),
            attachments_path=attachments_path,
            **common,
        )

    if source_type == "telegram":
        return TelegramConnector(
            bot_token=src_cfg["token"],
            chat_ids=sf.get("only_channels"),
            attachments_path=attachments_path,
            **common,
        )

    if source_type == "pachca":
        return PachcaConnector(
            access_token=src_cfg["token"],
            skip_channels=sf.get("skip_channels"),
            only_channels=sf.get("only_channels"),
            attachments_path=attachments_path,
            **common,
        )

    if source_type == "email":
        return EmailConnector(
            host=src_cfg["host"], port=src_cfg.get("port", 993),
            username=src_cfg["username"], password=src_cfg["password"],
            folders=src_cfg.get("folders"),
            skip_folders=sf.get("skip_folders"),
            channel_names=src_cfg.get("channel_names"),
            drafts_folder=src_cfg.get("drafts_folder", "Drafts"),
            from_address=src_cfg.get("from_address"),
            attachments_path=attachments_path,
            **common,
        )

    return None
