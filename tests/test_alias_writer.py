"""Tests for cli/alias_writer.py — the shared YAML writer surface.

Exercises the pure helpers (apply_upsert / apply_remove / apply_alias_string_change)
against a real ruamel.yaml round-trip on a tmp_path config so the comment-
preservation guarantee is genuinely tested.
"""
import pytest

from cli.alias_writer import (
    load_aliases_yaml,
    save_aliases_yaml,
    apply_upsert,
    apply_remove,
    apply_alias_string_change,
    find_alias_owner,
)


SEED_YAML = """\
# Top-of-file comment (operator's note about the file as a whole).
sqlite_path: "data/messages.db"

# Inline note above the user_aliases block.
user_aliases:
  - canonical_name: "Jane Smith"           # added by operator, keep this comment
    internal: true
    role: "Backend lead"
    team: "Platform"
    responsible_for: ["dev-pl"]
    aliases:
      - "jane"
      - "jsmith"
  - canonical_name: "Bob Wilson"
    aliases:
      - "bob"
      - "bwilson"
"""


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(SEED_YAML)
    return str(p)


# ── apply_upsert ──────────────────────────────────────────────────────────────

def test_upsert_creates_new_entry_with_defaults(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    out = apply_upsert(doc, {"canonical_name": "Carol Doe"})
    save_aliases_yaml(cfg, yaml, doc)
    assert out["canonical_name"] == "Carol Doe"
    assert out["aliases"] == ["Carol Doe"]  # defaulted on creation
    # Round-trip preserved the top-of-file comment.
    text = open(cfg).read()
    assert "Top-of-file comment" in text
    assert "Carol Doe" in text


def test_upsert_merge_unions_aliases_and_overwrites_scalars(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    out = apply_upsert(doc, {
        "canonical_name": "Jane Smith",
        "aliases": ["jsmith", "jane.s"],  # jsmith already present → dedup
        "role": "Engineering lead",        # overwrites scalar
        "responsible_for": ["PL-*"],       # appended to existing ["dev-pl"]
    })
    assert "jane" in out["aliases"]        # untouched survives
    assert "jsmith" in out["aliases"] and out["aliases"].count("jsmith") == 1
    assert "jane.s" in out["aliases"]
    assert out["role"] == "Engineering lead"
    assert out["team"] == "Platform"        # not provided → untouched
    assert set(out["responsible_for"]) == {"dev-pl", "PL-*"}


def test_upsert_canonical_match_is_case_insensitive(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    out = apply_upsert(doc, {"canonical_name": "JANE SMITH", "role": "new role"})
    # Touched the existing Jane Smith entry — didn't create a new one.
    assert out["canonical_name"] == "Jane Smith"
    save_aliases_yaml(cfg, yaml, doc)
    text = open(cfg).read()
    # Only one Jane Smith.
    assert text.lower().count("jane smith") == 1 or text.lower().count('"jane smith"') == 1


def test_upsert_replace_policy_wipes_other_fields(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    out = apply_upsert(doc,
                       {"canonical_name": "Jane Smith", "aliases": ["jsmith"]},
                       conflict_policy="replace",
                       )
    assert "role" not in out
    assert "team" not in out
    assert out["aliases"] == ["jsmith"]


def test_upsert_empty_canonical_raises(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    with pytest.raises(ValueError, match="canonical_name is required"):
        apply_upsert(doc, {"canonical_name": "   "})


def test_upsert_non_list_aliases_raises(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    with pytest.raises(ValueError, match="must be a list"):
        apply_upsert(doc, {"canonical_name": "X", "aliases": "not-a-list"})


def test_upsert_preserves_operator_comment_on_existing_entry(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    apply_upsert(doc, {"canonical_name": "Jane Smith", "role": "Director"})
    save_aliases_yaml(cfg, yaml, doc)
    text = open(cfg).read()
    assert "added by operator, keep this comment" in text


def test_upsert_creates_user_aliases_block_when_missing(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text('sqlite_path: "x"\n')  # no user_aliases at all
    yaml, doc = load_aliases_yaml(str(p))
    apply_upsert(doc, {"canonical_name": "Carol"})
    save_aliases_yaml(str(p), yaml, doc)
    text = open(str(p)).read()
    assert "user_aliases:" in text and "Carol" in text


# ── apply_remove ──────────────────────────────────────────────────────────────

def test_remove_returns_removed_entry(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    removed = apply_remove(doc, "Bob Wilson")
    assert removed["canonical_name"] == "Bob Wilson"
    assert "bob" in removed["aliases"]
    save_aliases_yaml(cfg, yaml, doc)
    assert "Bob Wilson" not in open(cfg).read()


def test_remove_case_insensitive(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    removed = apply_remove(doc, "bob WILSON")
    assert removed is not None
    assert removed["canonical_name"] == "Bob Wilson"


def test_remove_missing_returns_none(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    assert apply_remove(doc, "Ghost Person") is None


# ── apply_alias_string_change ─────────────────────────────────────────────────

def test_alias_string_add(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    out = apply_alias_string_change(doc, "Jane Smith", add=["jane.smith"])
    assert "jane.smith" in out["aliases"]
    # Existing aliases untouched
    assert "jane" in out["aliases"]
    assert "jsmith" in out["aliases"]


def test_alias_string_remove(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    out = apply_alias_string_change(doc, "Jane Smith", remove=["jsmith"])
    assert "jsmith" not in out["aliases"]
    assert "jane" in out["aliases"]


def test_alias_string_remove_case_insensitive(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    out = apply_alias_string_change(doc, "Jane Smith", remove=["JSMITH"])
    assert "jsmith" not in out["aliases"]


def test_alias_string_refuses_to_steal_from_other_canonical(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    with pytest.raises(ValueError, match="already owned by canonical 'Bob Wilson'"):
        apply_alias_string_change(doc, "Jane Smith", add=["bob"])


def test_alias_string_refuses_to_empty_aliases(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    with pytest.raises(ValueError, match="refusing to leave entry with no aliases"):
        apply_alias_string_change(doc, "Bob Wilson", remove=["bob", "bwilson"])


def test_alias_string_unknown_canonical_raises(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    with pytest.raises(ValueError, match="no entry found"):
        apply_alias_string_change(doc, "Ghost", add=["nope"])


def test_alias_string_empty_add_and_remove_raises(cfg):
    yaml, doc = load_aliases_yaml(cfg)
    with pytest.raises(ValueError, match="at least one"):
        apply_alias_string_change(doc, "Jane Smith")


def test_find_alias_owner(cfg):
    _, doc = load_aliases_yaml(cfg)
    assert find_alias_owner(doc, "bob") == "Bob Wilson"
    assert find_alias_owner(doc, "JANE") == "Jane Smith"  # case-insensitive lookup
    assert find_alias_owner(doc, "nobody") is None
