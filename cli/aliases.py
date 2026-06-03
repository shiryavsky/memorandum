"""``memorandum aliases refresh`` — append-only stub generator for user_aliases.

Diffs ``messages.sender`` (the source of *who exists*) against the alias index
already in ``config.yaml`` (the source of *who is whom*) and emits stub entries
for senders never seen in any alias group. Existing entries are sacred — never
edited, reordered, or removed.

Default output is stdout YAML the user reviews and pastes. ``--in-place`` appends
to ``config.yaml`` using ``ruamel.yaml`` round-trip so comments and key order
survive intact.
"""
import json as _json
import sys

from config import load_config, get_aliases
from storage.db import Database


# ── core: diff DB senders against existing aliases ────────────────────────────

def _build_known_alias_index(user_aliases: list, my_aliases: list = None) -> set:
    """Set of lower-cased names already covered by the alias config.

    A sender is "known" if it matches either a ``canonical_name`` or an entry in
    any ``user_aliases`` group's ``aliases`` list, **or** appears in ``my_aliases``
    (the current user — never a refresh candidate). Match is case-insensitive on
    a trimmed name.
    """
    known: set = set()
    for group in user_aliases or []:
        if not isinstance(group, dict):
            continue
        c = (group.get("canonical_name") or "")
        if isinstance(c, str) and c.strip():
            known.add(c.strip().lower())
        for a in group.get("aliases") or []:
            if isinstance(a, str) and a.strip():
                known.add(a.strip().lower())
    for a in my_aliases or []:
        if isinstance(a, str) and a.strip():
            known.add(a.strip().lower())
    return known


def _list_db_senders(db: Database) -> list:
    """Distinct senders with their per-source message counts, most-active first.

    Each row is ``{"sender": str, "count": int, "sources": {source: n, ...}}``.
    Empty/None senders are dropped. A sender appearing in multiple sources (same
    username on Telegram and Mattermost, say) gets one entry with the breakdown.
    """
    rows = db.conn.execute("""
        SELECT sender, source, COUNT(*) AS n
          FROM messages
         WHERE sender IS NOT NULL AND sender != ''
         GROUP BY sender, source
    """).fetchall()
    by_sender: dict = {}
    for r in rows:
        entry = by_sender.setdefault(r["sender"],
                                     {"sender": r["sender"], "count": 0, "sources": {}})
        entry["count"] += r["n"]
        entry["sources"][r["source"]] = r["n"]
    return sorted(by_sender.values(), key=lambda e: (-e["count"], e["sender"]))


def _candidate_entries(db: Database, user_aliases: list, my_aliases: list = None) -> list:
    """Senders in the DB not covered by ``user_aliases`` *or* ``my_aliases``."""
    known = _build_known_alias_index(user_aliases, my_aliases)
    out = []
    for row in _list_db_senders(db):
        name = row["sender"]
        if name.strip().lower() in known:
            continue
        out.append({
            "canonical_name": name,
            "aliases": [name],
            "count": row["count"],
            "sources": row["sources"],
        })
    return out


def _format_seen(entry: dict) -> str:
    """Render a ``seen N times in ...`` comment.

    Single source → ``seen 460 times in work_telegram``.
    Multiple    → ``seen 460 times (work_telegram: 300, company_mattermost: 160)``.
    """
    sources = entry.get("sources") or {}
    total = entry.get("count", 0)
    if len(sources) == 1:
        (src, n), = sources.items()
        return f"seen {n} times in {src}"
    parts = ", ".join(f"{src}: {n}" for src, n in
                      sorted(sources.items(), key=lambda kv: (-kv[1], kv[0])))
    return f"seen {total} times ({parts})" if parts else f"seen {total} times"


# ── output formats ────────────────────────────────────────────────────────────

def _stub_yaml(candidates: list) -> str:
    """Append-ready YAML block for stdout. Hints for role/team/reports_to fields kept commented."""
    if not candidates:
        return "# No new senders to add — every sender in the DB is already covered.\n"
    lines = [
        f"# {len(candidates)} new sender(s) seen in DB but not yet in user_aliases.",
        "# Sorted by message count (most-active first). Paste these under the",
        "# `user_aliases:` list in config.yaml, then edit canonical_name / aliases /",
        "# internal / role / team to taste.",
        "",
    ]
    for c in candidates:
        name = c["canonical_name"]
        # Escape any embedded double quotes so the stub stays parseable YAML.
        safe = name.replace('"', '\\"')
        lines.append(f'  - canonical_name: "{safe}"   # {_format_seen(c)}')
        lines.append(f'    aliases: ["{safe}"]')
        lines.append("    # internal: true")
        lines.append('    # role: ""')
        lines.append('    # team: ""')
        lines.append('    # reports_to: ""              # canonical_name of manager')
        lines.append('    # responsible_for: []         # channels / projects / issue prefixes')
        lines.append("")
    return "\n".join(lines)


def _candidates_json(candidates: list) -> str:
    return _json.dumps(
        [{"canonical_name": c["canonical_name"], "aliases": [c["canonical_name"]],
          "count": c["count"], "sources": c.get("sources", {})}
         for c in candidates],
        indent=2, ensure_ascii=False,
    )


# ── --in-place append (ruamel.yaml round-trip) ────────────────────────────────

def _append_in_place(config_path: str, candidates: list) -> int:
    """Append stub entries to ``config.yaml``'s ``user_aliases:`` list.

    Routes through ``cli.alias_writer`` (the shared YAML surface — same one the
    MCP write tools use), so the CLI append path and the MCP write path can't
    drift. The append-only contract is preserved: existing entries with
    the same canonical_name are merged into (effectively no-op for these stubs
    that only carry canonical_name + aliases identical to what's already there).
    Returns the number of candidates processed.
    """
    try:
        from cli.alias_writer import (
            load_aliases_yaml, save_aliases_yaml,
            apply_upsert, _user_aliases,
        )
    except ImportError as e:
        print(f"--in-place requires ruamel.yaml; install with `pip install ruamel.yaml` ({e})",
              file=sys.stderr)
        sys.exit(1)

    yaml, doc = load_aliases_yaml(config_path)

    for c in candidates:
        name = c["canonical_name"]
        apply_upsert(doc, {"canonical_name": name, "aliases": [name]},
                     conflict_policy="merge")
        # Attach the "added by ..." trailing comment to the just-appended row,
        # so the human can see why this entry showed up. Best-effort — never
        # fail an append for a missing comment.
        try:
            entries = _user_aliases(doc)
            entries[-1].yaml_add_eol_comment(
                f"added by `memorandum aliases refresh` — {_format_seen(c)}",
                "canonical_name",
            )
        except Exception:
            pass

    save_aliases_yaml(config_path, yaml, doc)
    return len(candidates)


# ── entry point ───────────────────────────────────────────────────────────────

def refresh(config_path: str = "config.yaml", in_place: bool = False,
            as_json: bool = False) -> None:
    """Read DB → diff against ``user_aliases`` → emit/append stubs for unknowns."""
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"config not found: {config_path}", file=sys.stderr)
        sys.exit(2)

    try:
        db = Database(config["sqlite_path"])
    except Exception as e:
        print(f"db not accessible ({config.get('sqlite_path')}): {e}", file=sys.stderr)
        sys.exit(2)

    user_aliases, my_aliases = get_aliases(config)
    try:
        candidates = _candidate_entries(db, user_aliases, my_aliases)
    finally:
        db.close()

    if as_json:
        print(_candidates_json(candidates))
        sys.exit(0)

    if in_place:
        n = _append_in_place(config_path, candidates)
        msg = f"Appended {n} stub(s) to {config_path}." if n else \
              "No new senders to append; config unchanged."
        print(msg, file=sys.stderr)
        sys.exit(0)

    print(_stub_yaml(candidates))
    sys.exit(0)
