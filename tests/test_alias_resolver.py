"""Tests for pipeline/alias_resolver.py."""
from pipeline.alias_resolver import AliasResolver


ALIASES = [
    {"canonical_name": "Jane Smith", "aliases": ["jane", "jane.smith", "J.S."]},
    {"canonical_name": "Bob Wilson", "aliases": ["bob", "bwilson"]},
]
MY_ALIASES = ["john", "johnd", "John Doe"]


def _resolver(user_aliases=None, my_aliases=None, internal_domains=None):
    return AliasResolver(user_aliases or ALIASES, my_aliases or MY_ALIASES,
                         internal_domains=internal_domains)


# ── resolve() ─────────────────────────────────────────────────────────────────

def test_resolve_returns_canonical_for_known_alias():
    r = _resolver()
    assert r.resolve("jane") == "Jane Smith"
    assert r.resolve("bob") == "Bob Wilson"


def test_resolve_returns_original_for_unknown():
    r = _resolver()
    assert r.resolve("alice") == "alice"


def test_resolve_is_case_insensitive():
    r = _resolver()
    assert r.resolve("JANE") == "Jane Smith"
    assert r.resolve("Jane.Smith") == "Jane Smith"


def test_resolve_handles_empty_user_aliases():
    r = AliasResolver([], MY_ALIASES)
    assert r.resolve("jane") == "jane"


# ── resolve_known() ───────────────────────────────────────────────────────────

def test_resolve_known_returns_canonical_for_alias():
    r = _resolver()
    assert r.resolve_known("jane") == "Jane Smith"


def test_resolve_known_returns_canonical_when_input_is_the_canonical():
    """Regression: when the canonical name is itself the handle people mention
    (e.g. ``canonical_name: "john.doe"``), `resolve_known` must still recognise
    it as a known identity — the old `resolved != lookup` heuristic in
    pipeline/ingest.py used to return None here."""
    r = AliasResolver(
        [{"canonical_name": "john.doe", "aliases": ["John Doe"]}],
        my_aliases=[],
    )
    assert r.resolve_known("john.doe") == "john.doe"
    assert r.resolve_known("JOHN.DOE") == "john.doe"


def test_resolve_known_returns_none_for_unknown():
    r = _resolver()
    assert r.resolve_known("alice") is None


def test_resolve_known_returns_none_for_empty():
    r = _resolver()
    assert r.resolve_known("") is None


# ── mentions_me() ─────────────────────────────────────────────────────────────

def test_mentions_me_detects_alias_substring():
    r = _resolver()
    assert r.mentions_me("hey @john can you review this?") is True


def test_mentions_me_detects_full_name():
    r = _resolver()
    assert r.mentions_me("John Doe approved the PR") is True


def test_mentions_me_is_case_insensitive():
    r = _resolver()
    assert r.mentions_me("JOHND please check") is True


def test_mentions_me_returns_false_for_no_match():
    r = _resolver()
    assert r.mentions_me("alice and bob are working on it") is False


def test_mentions_me_returns_false_when_no_aliases_configured():
    r = AliasResolver(ALIASES, [])
    assert r.mentions_me("john please help") is False


# ── is_internal() ─────────────────────────────────────────────────────────────

_INTERNAL_ALIASES = [
    {"canonical_name": "Jane Smith", "internal": True, "aliases": ["jane", "jsmith"]},
    {"canonical_name": "Bob Wilson", "aliases": ["bob"]},  # external (no flag)
]


def test_is_internal_current_user_always_internal():
    r = _resolver(user_aliases=[], my_aliases=["john"])
    assert r.is_internal("john") is True
    assert r.is_internal("JOHN") is True  # case-insensitive


def test_is_internal_true_for_flagged_alias_group():
    r = _resolver(user_aliases=_INTERNAL_ALIASES)
    assert r.is_internal("jane") is True
    assert r.is_internal("jsmith") is True  # any alias of the group


def test_is_internal_false_for_unflagged_group():
    r = _resolver(user_aliases=_INTERNAL_ALIASES)
    assert r.is_internal("bob") is False


def test_is_internal_false_for_unknown_sender():
    r = _resolver(user_aliases=_INTERNAL_ALIASES)
    assert r.is_internal("stranger") is False


# ── is_internal() — layered precedence ─────────────────────────────

def test_is_internal_source_flag_is_lowest_layer():
    r = _resolver(user_aliases=[])
    assert r.is_internal("stranger", source_internal=True) is True
    assert r.is_internal("stranger", source_internal=False) is False


def test_is_internal_domain_promotes_stranger():
    r = _resolver(user_aliases=[], internal_domains=["mycompany.com"])
    assert r.is_internal("alice", email="alice@mycompany.com") is True
    # External domain → not promoted, falls through to source flag (False here).
    assert r.is_internal("bob", email="bob@client.com") is False


def test_is_internal_domain_is_case_insensitive():
    r = _resolver(user_aliases=[], internal_domains=["MyCompany.com"])
    assert r.is_internal("alice", email="alice@MYCOMPANY.com") is True
    assert r.is_internal("alice", email="alice@mycompany.COM") is True


def test_is_internal_domain_exact_match_only_no_subdomain():
    r = _resolver(user_aliases=[], internal_domains=["mycompany.com"])
    # v1: no wildcards; subdomain does NOT match the bare domain.
    assert r.is_internal("carol", email="carol@dev.mycompany.com") is False


def test_is_internal_alias_false_demotes_domain_internal():
    """A contractor on @mycompany.com: alias `internal: false` overrides the domain rule."""
    aliases = [{"canonical_name": "Contractor", "aliases": ["contractor"], "internal": False}]
    r = _resolver(user_aliases=aliases, internal_domains=["mycompany.com"])
    assert r.is_internal("contractor", email="contractor@mycompany.com") is False


def test_is_internal_alias_false_demotes_source_flag():
    """A guest in an internal Mattermost: alias `internal: false` wins over source flag."""
    aliases = [{"canonical_name": "Guest", "aliases": ["guest"], "internal": False}]
    r = _resolver(user_aliases=aliases)
    assert r.is_internal("guest", source_internal=True) is False


def test_is_internal_alias_true_promotes_external_domain():
    """Embedded partner with a client domain but flagged internal in user_aliases."""
    aliases = [{"canonical_name": "Partner", "aliases": ["partner"], "internal": True}]
    r = _resolver(user_aliases=aliases, internal_domains=["mycompany.com"])
    assert r.is_internal("partner", email="partner@client.com") is True


def test_is_internal_my_aliases_always_internal_overriding_everything():
    aliases = [{"canonical_name": "John Doe", "aliases": ["john"], "internal": False}]
    # Even with `internal: false` in the alias, the current user stays internal.
    r = _resolver(user_aliases=aliases, my_aliases=["john"])
    assert r.is_internal("john") is True
    assert r.is_internal("john", email="john@client.com") is True


def test_is_internal_alias_absent_is_not_an_opinion():
    """Earlier behavior: an alias group with no `internal` key falls through to domain/source."""
    aliases = [{"canonical_name": "Bob Wilson", "aliases": ["bob"]}]  # no internal key
    r = _resolver(user_aliases=aliases, internal_domains=["mycompany.com"])
    # Falls through: no email, no source flag → False.
    assert r.is_internal("bob") is False
    # Domain promotes when email matches.
    assert r.is_internal("bob", email="bob@mycompany.com") is True
    # Source flag also promotes when no email match.
    assert r.is_internal("bob", source_internal=True) is True


def test_is_internal_no_email_and_no_rule_matches_legacy_behavior():
    """Backward-compatible: missing email + no domains + no source flag = old behavior."""
    aliases = [
        {"canonical_name": "Jane Smith", "internal": True, "aliases": ["jane"]},
        {"canonical_name": "Bob Wilson", "aliases": ["bob"]},
    ]
    r = _resolver(user_aliases=aliases)
    assert r.is_internal("jane") is True
    assert r.is_internal("bob") is False
    assert r.is_internal("stranger") is False


# ── is_domain_internal() ──────────────────────────────────────────────────────

def test_is_domain_internal_no_domains_configured_returns_false():
    r = _resolver(user_aliases=[])
    assert r.is_domain_internal("alice@mycompany.com") is False


def test_is_domain_internal_no_at_sign_returns_false():
    r = _resolver(user_aliases=[], internal_domains=["mycompany.com"])
    assert r.is_domain_internal("not-an-email") is False
    assert r.is_domain_internal("") is False
    assert r.is_domain_internal(None) is False  # tolerates None


def test_is_domain_internal_strips_leading_at_in_domain_config():
    """Lenient config parsing: @mycompany.com and mycompany.com both work."""
    r = _resolver(user_aliases=[], internal_domains=["@mycompany.com"])
    assert r.is_domain_internal("alice@mycompany.com") is True


# ── alias_groups() ────────────────────────────────────────────────────────────

def test_alias_groups_returns_all_groups():
    r = _resolver()
    groups = r.alias_groups()
    canonicals = {g["canonical_name"] for g in groups}
    assert "Jane Smith" in canonicals
    assert "Bob Wilson" in canonicals


def test_alias_groups_empty_when_no_user_aliases():
    r = AliasResolver([], MY_ALIASES)
    assert r.alias_groups() == []


# ── get_meta ───────────────────────────────────────────────────────

def test_get_meta_returns_role_team_reports_to_responsible_for():
    r = AliasResolver([
        {"canonical_name": "Jane Smith", "aliases": ["jane"], "internal": True,
         "role": "Backend lead", "team": "Platform",
         "responsible_for": ["dev-pl", "PL-*"]},
        {"canonical_name": "Alex Petrov", "aliases": ["alex"], "internal": True,
         "role": "Junior backend", "team": "Platform", "reports_to": "Jane Smith"},
    ], MY_ALIASES)

    assert r.get_meta("Jane Smith") == {
        "role": "Backend lead", "team": "Platform",
        "responsible_for": ["dev-pl", "PL-*"],
    }
    assert r.get_meta("Alex Petrov") == {
        "role": "Junior backend", "team": "Platform", "reports_to": "Jane Smith",
    }


def test_get_meta_returns_empty_dict_for_unknown_canonical():
    r = AliasResolver([{"canonical_name": "Jane", "aliases": ["jane"]}], MY_ALIASES)
    assert r.get_meta("Nobody") == {}


def test_get_meta_omits_unset_fields():
    r = AliasResolver([
        {"canonical_name": "Bob Wilson", "aliases": ["bob"], "role": "CTO @ Acme"},
    ], MY_ALIASES)
    assert r.get_meta("Bob Wilson") == {"role": "CTO @ Acme"}


def test_get_meta_is_isolated_copy():
    """Mutating the returned dict shouldn't bleed back into the resolver."""
    r = AliasResolver([
        {"canonical_name": "X", "aliases": ["x"], "role": "dev"},
    ], MY_ALIASES)
    out = r.get_meta("X")
    out["role"] = "MUTATED"
    assert r.get_meta("X") == {"role": "dev"}


def test_reports_to_unknown_canonical_does_not_crash():
    """Free-form config — a stale reports_to value must not raise."""
    r = AliasResolver([
        {"canonical_name": "Alex", "aliases": ["alex"], "reports_to": "Ghost Person"},
    ], MY_ALIASES)
    assert r.get_meta("Alex")["reports_to"] == "Ghost Person"
