"""user_aliases read + write tools.

`_invalidate_config_cache` stays in server.py — it touches that module's
globals. Write handlers reach for it via `_srv._invalidate_config_cache()`.
"""
import json
from typing import Optional

from mcp.types import TextContent

from config import get_aliases, get_alias_edit_settings
from mcp_server import server as _srv


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
    return [TextContent(type="text", text=_build_aliases_text(_srv.get_config()))]


async def _upsert_user_alias(args: dict) -> list[TextContent]:
    """Create or merge a single user_aliases entry. The MCP write surface."""
    config = _srv.get_config()
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
    yaml_obj, doc = load_aliases_yaml(_srv._config_path)
    needle = canonical.lower()
    is_new = not any(
        isinstance(e, dict) and (e.get("canonical_name") or "").strip().lower() == needle
        for e in _user_aliases(doc)
    )
    if is_new and len(_user_aliases(doc)) >= settings["max_entries"]:
        return [TextContent(type="text",
                            text=f"user_aliases already at the cap of {settings['max_entries']} "
                                 f"entries; remove an entry first or raise max_entries in config.")]

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
    save_aliases_yaml(_srv._config_path, yaml_obj, doc)
    _srv._invalidate_config_cache()
    return [TextContent(type="text", text=json.dumps({"ok": True, "entry": result},
                                                     default=str, ensure_ascii=False))]


async def _remove_user_alias(args: dict) -> list[TextContent]:
    """Delete one user_aliases entry by canonical_name."""
    config = _srv.get_config()
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
    yaml_obj, doc = load_aliases_yaml(_srv._config_path)
    removed = apply_remove(doc, canonical)
    if removed is None:
        return [TextContent(type="text",
                            text=f"No entry found for canonical_name {canonical!r}.")]
    save_aliases_yaml(_srv._config_path, yaml_obj, doc)
    _srv._invalidate_config_cache()
    return [TextContent(type="text", text=json.dumps({"ok": True, "removed": removed},
                                                     default=str, ensure_ascii=False))]


async def _update_user_alias_strings(args: dict) -> list[TextContent]:
    """Add and/or remove alias strings on one entry."""
    config = _srv.get_config()
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
    yaml_obj, doc = load_aliases_yaml(_srv._config_path)

    from cli.alias_writer import _find_entry
    _, entry = _find_entry(doc, canonical)
    if entry is None:
        return [TextContent(type="text",
                            text=f"No entry found for canonical_name {canonical!r}.")]
    current = list(entry.get("aliases") or [])
    remove_lc = {r.strip().lower() for r in remove}
    projected = [a for a in current if a.strip().lower() not in remove_lc] + list(add)
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
    save_aliases_yaml(_srv._config_path, yaml_obj, doc)
    _srv._invalidate_config_cache()
    return [TextContent(type="text", text=json.dumps({"ok": True, "entry": result},
                                                     default=str, ensure_ascii=False))]
