"""Shared `user_aliases` YAML writer surface.

Used by both:
  * ``memorandum aliases refresh --in-place`` (TASK-024) — appends stubs for
    senders the operator hasn't curated yet.
  * The three MCP write tools (``upsert_user_alias``, ``remove_user_alias``,
    ``update_user_alias_strings``) — TASK-029.

The whole point of this module is that there's exactly ONE path that mutates
``config.yaml``. Both callers go through ``load_aliases_yaml`` → mutate via the
``apply_*`` helpers → ``save_aliases_yaml``. Operator comments / key order /
quoting survive intact thanks to ``ruamel.yaml`` round-trip.

The ``apply_*`` helpers raise ``ValueError`` on bad input (missing canonical,
last-alias removal, cross-canonical alias theft) so the calling layer can
translate to its own error surface (CLI exit code / MCP tool text).

Schema is fixed at the TASK-023 field set:
``canonical_name``, ``aliases``, ``internal``, ``role``, ``team``,
``reports_to``, ``responsible_for``.
"""
from typing import Optional, Tuple


# Top-level fields recognized on a user_aliases entry. Anything else is left
# untouched (operator may have stashed comments-as-keys we shouldn't trample).
SCALAR_FIELDS = ("canonical_name", "internal", "role", "team", "reports_to")
LIST_FIELDS = ("aliases", "responsible_for")


# ── YAML I/O ─────────────────────────────────────────────────────────────────

def _yaml():
    """Return a configured ruamel.yaml YAML instance (round-trip mode).

    Lazy-imported so unrelated CLI verbs / tests don't pay for the dep.
    """
    try:
        from ruamel.yaml import YAML
    except ImportError as e:
        raise RuntimeError(
            "user_aliases writes require ruamel.yaml; install with `pip install ruamel.yaml`"
        ) from e
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load_aliases_yaml(config_path: str) -> Tuple[object, object]:
    """Load ``config.yaml`` for editing. Returns ``(yaml, doc)``.

    Caller mutates ``doc`` via the ``apply_*`` helpers, then hands the same
    ``(yaml, doc)`` pair to ``save_aliases_yaml``.
    """
    yaml = _yaml()
    with open(config_path) as f:
        doc = yaml.load(f)
    return yaml, doc


def save_aliases_yaml(config_path: str, yaml, doc) -> None:
    """Write ``doc`` back to ``config_path`` preserving comments / key order."""
    with open(config_path, "w") as f:
        yaml.dump(doc, f)


# ── Lookups ──────────────────────────────────────────────────────────────────

def _user_aliases(doc) -> list:
    """Get the user_aliases list, creating it (as a CommentedSeq) if missing."""
    if doc.get("user_aliases") is None:
        from ruamel.yaml.comments import CommentedSeq
        doc["user_aliases"] = CommentedSeq()
    return doc["user_aliases"]


def _find_entry(doc, canonical_name: str) -> Tuple[Optional[int], Optional[object]]:
    """Locate an existing entry by canonical_name (case-insensitive, trimmed).

    Returns ``(index, entry_ref)`` or ``(None, None)`` if not present.
    """
    if not canonical_name:
        return None, None
    needle = canonical_name.strip().lower()
    entries = _user_aliases(doc)
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        c = (e.get("canonical_name") or "").strip().lower()
        if c == needle:
            return i, e
    return None, None


def find_alias_owner(doc, alias_str: str) -> Optional[str]:
    """Which canonical_name (if any) currently owns this alias string?

    Used by ``apply_alias_string_change`` to refuse cross-canonical theft, and
    by the MCP upsert path to surface a clear "already owned by X" error.
    """
    if not alias_str:
        return None
    needle = alias_str.strip().lower()
    for entry in _user_aliases(doc):
        if not isinstance(entry, dict):
            continue
        for a in entry.get("aliases") or []:
            if isinstance(a, str) and a.strip().lower() == needle:
                return entry.get("canonical_name")
    return None


# ── Mutators ─────────────────────────────────────────────────────────────────

def apply_upsert(doc, entry: dict, *, conflict_policy: str = "merge") -> dict:
    """Create or update one user_aliases entry. Returns the resulting entry as dict.

    ``conflict_policy="merge"`` (default):
        Scalar fields (``role``, ``team``, ``internal``, …) overwrite when present
        in ``entry`` and are left alone otherwise. List fields (``aliases``,
        ``responsible_for``) are unioned (preserving existing order, appending
        new values uniquely).

    ``conflict_policy="replace"``:
        Existing entry is wiped, the new ``entry`` becomes its body verbatim.

    A missing entry (no matching canonical) is always created with the given
    fields regardless of policy.

    Raises ``ValueError`` on missing/empty canonical_name or non-list values for
    list-typed fields.
    """
    from ruamel.yaml.comments import CommentedMap

    canonical = (entry.get("canonical_name") or "").strip()
    if not canonical:
        raise ValueError("canonical_name is required and must be non-empty")
    if conflict_policy not in ("merge", "replace"):
        raise ValueError(f"unknown conflict_policy {conflict_policy!r}")

    # Normalize input: trim strings, validate list types early so we never half-write.
    for f in LIST_FIELDS:
        if f in entry and entry[f] is not None and not isinstance(entry[f], list):
            raise ValueError(f"{f!r} must be a list")

    idx, existing = _find_entry(doc, canonical)
    entries = _user_aliases(doc)

    if existing is None or conflict_policy == "replace":
        new = CommentedMap()
        new["canonical_name"] = canonical
        for f in SCALAR_FIELDS:
            if f == "canonical_name":
                continue
            if f in entry and entry[f] is not None:
                new[f] = entry[f]
        for f in LIST_FIELDS:
            if f in entry and entry[f] is not None:
                new[f] = _dedup_preserving_order(entry[f])
        # aliases default = [canonical] (only when creating fresh and not provided).
        if existing is None and "aliases" not in new:
            new["aliases"] = [canonical]
        if existing is None:
            entries.append(new)
        else:
            entries[idx] = new
        return _entry_to_plain(new)

    # merge into existing
    for f in SCALAR_FIELDS:
        if f == "canonical_name":
            continue
        if f in entry and entry[f] is not None:
            existing[f] = entry[f]
    for f in LIST_FIELDS:
        if f in entry and entry[f] is not None:
            current = list(existing.get(f) or [])
            existing[f] = _dedup_preserving_order(current + list(entry[f]))
    return _entry_to_plain(existing)


def apply_remove(doc, canonical_name: str) -> Optional[dict]:
    """Drop the entry whose canonical_name matches (case-insensitive).

    Returns the removed entry as a plain dict, or ``None`` if no such entry
    existed (caller decides whether that's an error).
    """
    idx, entry = _find_entry(doc, canonical_name)
    if entry is None:
        return None
    removed = _entry_to_plain(entry)
    del _user_aliases(doc)[idx]
    return removed


def apply_alias_string_change(
    doc,
    canonical_name: str,
    add: Optional[list] = None,
    remove: Optional[list] = None,
) -> dict:
    """Add and/or remove strings on one entry's ``aliases:`` list.

    Refuses to "steal" an alias already owned by a different canonical (raises
    ``ValueError`` naming the conflicting owner). Refuses to leave the entry
    with an empty aliases list (raises ``ValueError`` — caller should suggest
    ``remove_user_alias`` instead).

    Returns the post-edit entry as a plain dict.
    """
    idx, entry = _find_entry(doc, canonical_name)
    if entry is None:
        raise ValueError(f"no entry found for canonical_name {canonical_name!r}")

    add = list(add or [])
    remove = list(remove or [])
    if not add and not remove:
        raise ValueError("at least one of `add` or `remove` must be non-empty")

    # Conflict check: nothing in `add` can already belong to another canonical.
    for a in add:
        owner = find_alias_owner(doc, a)
        if owner and owner.strip().lower() != canonical_name.strip().lower():
            raise ValueError(
                f"alias {a!r} is already owned by canonical {owner!r}; "
                f"remove it from {owner!r} first or pick a different string"
            )

    current = list(entry.get("aliases") or [])
    remove_lc = {r.strip().lower() for r in remove}
    kept = [a for a in current if a.strip().lower() not in remove_lc]
    final = _dedup_preserving_order(kept + add)

    if not final:
        raise ValueError(
            "refusing to leave entry with no aliases; "
            "use remove_user_alias to delete the entry entirely"
        )
    entry["aliases"] = final
    return _entry_to_plain(entry)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _dedup_preserving_order(items: list) -> list:
    """Return ``items`` with later duplicates dropped, preserving first-seen order.

    Case-insensitive comparison on strings; non-strings compared by identity-ish
    via ``str(x).lower()`` since the schema fields here are all string-ish.
    """
    seen: set = set()
    out: list = []
    for x in items:
        key = str(x).strip().lower() if isinstance(x, str) else x
        if key in seen:
            continue
        seen.add(key)
        out.append(x)
    return out


def _entry_to_plain(entry) -> dict:
    """ruamel CommentedMap → plain dict so callers don't pay for the YAML type."""
    out: dict = {}
    for k, v in entry.items():
        if isinstance(v, list):
            out[k] = list(v)
        else:
            out[k] = v
    return out
