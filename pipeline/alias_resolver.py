"""Alias resolution for sender names and current-user mention detection."""

# Optional per-user metadata fields surfaced via `get_meta()` (TASK-023).
# Free-form text; the resolver doesn't validate or normalize them.
_META_FIELDS = ("role", "team", "reports_to", "responsible_for")


class AliasResolver:
    """Maps sender name aliases to canonical names and detects current-user mentions.

    Built from config `user_aliases` (list of {canonical_name, aliases, internal?,
    role?, team?, reports_to?, responsible_for?}), `my_aliases` (list of alias
    strings for the person running Memorandum), and optional `internal_domains`
    (list of bare email domains whose owners are treated as company staff).

    Internal/external precedence — broadest → most specific:
        1. source `internal:` flag (passed in as `source_internal`)
        2. `internal_domains` (an email's domain matches)
        3. per-alias `internal: true|false` (wins over both above)
        4. `my_aliases` (always internal — the running user)
    """

    def __init__(
        self,
        user_aliases: list[dict],
        my_aliases: list[str],
        internal_domains: list[str] | None = None,
    ):
        # alias (lowercased) → canonical_name
        self._map: dict[str, str] = {}
        # canonical_name → True/False — present ONLY when the alias group
        # explicitly set `internal:`. Absent entries mean "no opinion".
        self._explicit_internal: dict[str, bool] = {}
        # canonical_name → {role, team, reports_to, responsible_for}; absent fields omitted
        self._meta: dict[str, dict] = {}
        for group in user_aliases:
            canonical = group.get("canonical_name", "")
            for alias in group.get("aliases", []):
                self._map[alias.lower()] = canonical
            if canonical:
                if "internal" in group:
                    self._explicit_internal[canonical] = bool(group["internal"])
                meta = {f: group[f] for f in _META_FIELDS if group.get(f)}
                if meta:
                    self._meta[canonical] = meta

        self._my_aliases: list[str] = [a.lower() for a in my_aliases]
        self._internal_domains: set[str] = {
            d.lower().strip().lstrip("@") for d in (internal_domains or []) if d
        }

    def resolve(self, sender: str) -> str:
        """Return canonical name for sender, or the original value if no alias matches."""
        return self._map.get(sender.lower(), sender)

    def is_domain_internal(self, email: str) -> bool:
        """Return True if the email's domain is in `internal_domains` (case-insensitive)."""
        if not email or "@" not in email or not self._internal_domains:
            return False
        domain = email.rsplit("@", 1)[-1].lower().strip()
        return domain in self._internal_domains

    def is_internal(
        self,
        sender: str,
        email: str | None = None,
        source_internal: bool = False,
    ) -> bool:
        """Return True if the sender is internal under the layered precedence.

        Order — first match wins:
          1. my_aliases → True (current user is always internal)
          2. explicit alias verdict (True or False) — wins over the rest
          3. internal_domains email match → True
          4. source_internal → True
          5. else → False
        """
        if sender and sender.lower() in self._my_aliases:
            return True
        canonical = self.resolve(sender) if sender else sender
        if canonical in self._explicit_internal:
            return self._explicit_internal[canonical]
        if email and self.is_domain_internal(email):
            return True
        return bool(source_internal)

    def mentions_me(self, text: str) -> bool:
        """Return True if the text contains any of the current user's aliases."""
        if not self._my_aliases:
            return False
        text_lower = text.lower()
        return any(alias in text_lower for alias in self._my_aliases)

    def alias_groups(self) -> list[dict]:
        """Return the alias mapping as a list of {canonical_name, aliases} dicts."""
        groups: dict[str, list[str]] = {}
        for alias, canonical in self._map.items():
            groups.setdefault(canonical, []).append(alias)
        return [{"canonical_name": c, "aliases": aliases} for c, aliases in groups.items()]

    def get_meta(self, canonical_name: str) -> dict:
        """Return the user's role/team/reports_to/responsible_for, or ``{}`` if unset.

        Looked up by canonical name (the value `resolve()` produces). Unknown
        canonicals return an empty dict — callers don't need to special-case.
        """
        return dict(self._meta.get(canonical_name, {}))
