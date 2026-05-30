"""Configuration loader for Memorandum Message Collector."""
import yaml
from pathlib import Path
from typing import Optional


def load_config(path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {path}. "
            f"Please create config.yaml from the template."
        )
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_aliases(config: dict) -> tuple[list[dict], list[str]]:
    """Return (user_aliases, my_aliases) from config."""
    return config.get("user_aliases", []), config.get("my_aliases", [])


_DEFAULT_MAX_FETCH_WORKERS = 8


def get_ingest_settings(config: dict) -> dict:
    """Return normalized ingest settings.

    `fetch_workers`: how many sources to fetch concurrently. ``None``/``0``/missing
    means "auto" (number of enabled sources, clamped to ``max_fetch_workers``).
    ``1`` keeps the legacy strictly-sequential path. Negative is clamped to 1.

    `max_fetch_workers`: hard ceiling on the auto path, so a 30-source config
    doesn't open 30 concurrent HTTP fan-outs. Defaults to 8.
    """
    block = config.get("ingest") or {}
    raw_workers = block.get("fetch_workers")
    if raw_workers in (None, 0):
        fetch_workers: Optional[int] = None  # auto
    else:
        try:
            fetch_workers = max(1, int(raw_workers))
        except (TypeError, ValueError):
            fetch_workers = None
    try:
        max_workers = max(1, int(block.get("max_fetch_workers", _DEFAULT_MAX_FETCH_WORKERS)))
    except (TypeError, ValueError):
        max_workers = _DEFAULT_MAX_FETCH_WORKERS
    return {"fetch_workers": fetch_workers, "max_fetch_workers": max_workers}


_DEFAULT_ALIAS_EDIT_SETTINGS = {
    "allow_alias_edits": True,
    "max_entries": 500,
    "max_aliases_per_entry": 50,
    "max_list_fields": 50,
}


def get_alias_edit_settings(config: dict) -> dict:
    """Return normalized settings for the user_aliases write tools.

    Top-level keys (all optional, sensible defaults):
        allow_alias_edits     bool  default True
        max_entries           int   default 500
        max_aliases_per_entry int   default 50
        max_list_fields       int   default 50 (per list-typed field, e.g. responsible_for)
    Garbage / non-numeric overrides fall back to the default.
    """
    out = dict(_DEFAULT_ALIAS_EDIT_SETTINGS)
    out["allow_alias_edits"] = bool(config.get("allow_alias_edits", True))
    for k in ("max_entries", "max_aliases_per_entry", "max_list_fields"):
        if k in config:
            try:
                out[k] = max(1, int(config[k]))
            except (TypeError, ValueError):
                pass
    return out


_DEFAULT_RETENTION_DAYS = 365
_DEFAULT_PRUNE_INTERVAL_HOURS = 24
_DEFAULT_FILE_CACHE_GRACE_MINUTES = 60


def get_retention_settings(config: dict) -> dict:
    """Return normalized retention/housekeeping settings.

    **Retention is opt-in.** If the top-level ``retention:`` block is absent
    entirely, returns ``{"retention_days": None, …}`` — housekeeping does
    nothing. This avoids silently deleting data on a fresh install. Once the
    operator adds a ``retention:`` block, the per-key defaults apply:

      retention_days             int  default 365 once the block exists;
                                      0 / null inside the block = disabled
      prune_interval_hours       int  default 24
      file_cache_grace_minutes   int  default 60 — file-cache sweep skips
                                      anything younger than this so a just-
                                      downloaded attachment can't be reaped
                                      out from under an interrupted ingest.

    Garbage values inside the block fall back to the defaults. Negative ints
    are clamped to the default rather than 0 — silently disabling retention is
    a worse failure mode than over-keeping data.
    """
    if "retention" not in config or config.get("retention") is None:
        return {
            "retention_days": None,
            "prune_interval_hours": _DEFAULT_PRUNE_INTERVAL_HOURS,
            "file_cache_grace_minutes": _DEFAULT_FILE_CACHE_GRACE_MINUTES,
        }
    block = config.get("retention") or {}
    raw_days = block.get("retention_days", _DEFAULT_RETENTION_DAYS)
    if raw_days in (None, 0, "0"):
        retention_days: Optional[int] = None
    else:
        try:
            n = int(raw_days)
            retention_days = n if n > 0 else _DEFAULT_RETENTION_DAYS
        except (TypeError, ValueError):
            retention_days = _DEFAULT_RETENTION_DAYS
    try:
        interval = max(0, int(block.get("prune_interval_hours", _DEFAULT_PRUNE_INTERVAL_HOURS)))
    except (TypeError, ValueError):
        interval = _DEFAULT_PRUNE_INTERVAL_HOURS
    try:
        grace = max(0, int(block.get("file_cache_grace_minutes",
                                     _DEFAULT_FILE_CACHE_GRACE_MINUTES)))
    except (TypeError, ValueError):
        grace = _DEFAULT_FILE_CACHE_GRACE_MINUTES
    return {
        "retention_days": retention_days,
        "prune_interval_hours": interval,
        "file_cache_grace_minutes": grace,
    }


def get_internal_domains(config: dict) -> list[str]:
    """Return lower-cased bare email domains marked internal in config.

    Missing/empty `internal_domains:` returns []. Entries are stripped of
    whitespace and a leading '@' if present; empty/None entries are skipped.
    """
    raw = config.get("internal_domains") or []
    out: list[str] = []
    for item in raw:
        if not item:
            continue
        cleaned = str(item).strip().lstrip("@").lower()
        if cleaned:
            out.append(cleaned)
    return out


def get_sources(config: dict) -> list[tuple[str, dict]]:
    """Return (name, source_config) for every enabled source."""
    return [
        (name, src)
        for name, src in config.get("sources", {}).items()
        if src.get("enabled", True)
    ]
