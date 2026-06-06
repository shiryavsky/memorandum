"""Tests for config.py — load_config and get_sources."""
import pytest
import yaml

from config import load_config, get_sources, get_aliases, get_internal_domains
from config import get_ingest_settings, get_alias_edit_settings, get_retention_settings


def test_load_config_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("nonexistent_config_file_xyz.yaml")


def test_load_config_parses_valid_yaml(tmp_path):
    cfg = {"sqlite_path": "data/db.sqlite", "chroma_path": "data/chroma", "sources": {}}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(cfg))
    result = load_config(str(p))
    assert result == cfg


# ── secrets merge ────────────────────────────────────────────────────────────

def test_load_config_merges_secrets_per_source(tmp_path):
    """`sources[name]` entries from secrets_path get shallow-merged into cfg."""
    secrets = {"sources": {
        "work_mm": {"token": "PAT-secret"},
        "work_tg": {"token": "123:secret"},
    }}
    secrets_path = tmp_path / "secrets.yaml"
    secrets_path.write_text(yaml.dump(secrets))

    cfg = {
        "secrets_path": str(secrets_path),
        "sources": {
            "work_mm": {"type": "mattermost", "url": "u", "enabled": True},
            "work_tg": {"type": "telegram", "enabled": True},
        },
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg))

    result = load_config(str(cfg_path))
    assert result["sources"]["work_mm"]["token"] == "PAT-secret"
    assert result["sources"]["work_mm"]["url"] == "u"  # main config keys preserved
    assert result["sources"]["work_tg"]["token"] == "123:secret"


def test_load_config_secrets_path_via_env_var(tmp_path, monkeypatch):
    """MEMORANDUM_SECRETS_PATH overrides the default when no secrets_path key is set."""
    secrets_path = tmp_path / "secrets.yaml"
    secrets_path.write_text(yaml.dump({"sources": {"s1": {"token": "from-env"}}}))
    monkeypatch.setenv("MEMORANDUM_SECRETS_PATH", str(secrets_path))

    cfg = {"sources": {"s1": {"type": "telegram"}}}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg))

    result = load_config(str(cfg_path))
    assert result["sources"]["s1"]["token"] == "from-env"


def test_load_config_missing_secrets_file_is_ok(tmp_path):
    """Pointing at a non-existent secrets file is fine — no merge, no error."""
    cfg = {
        "secrets_path": str(tmp_path / "does-not-exist.yaml"),
        "sources": {"s1": {"type": "telegram", "token": "inline"}},
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg))

    result = load_config(str(cfg_path))
    assert result["sources"]["s1"]["token"] == "inline"  # inline value untouched


def test_load_config_secrets_ignored_for_unknown_source(tmp_path, caplog):
    """A secrets entry for a source not in main config is logged + ignored.

    Silently creating a source on every load would be a surprising failure mode
    (typo in secrets.yaml) — refuse and warn instead.
    """
    secrets_path = tmp_path / "secrets.yaml"
    secrets_path.write_text(yaml.dump({"sources": {"ghost": {"token": "x"}}}))
    cfg = {
        "secrets_path": str(secrets_path),
        "sources": {"real": {"type": "telegram"}},
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg))

    with caplog.at_level("WARNING"):
        result = load_config(str(cfg_path))
    assert "ghost" not in result["sources"]
    assert any("ghost" in r.message for r in caplog.records)


def test_get_sources_returns_enabled_only():
    config = {
        "sources": {
            "s1": {"type": "mattermost", "enabled": True},
            "s2": {"type": "telegram", "enabled": False},
        }
    }
    result = get_sources(config)
    names = [name for name, _ in result]
    assert names == ["s1"]


def test_get_sources_absent_key():
    result = get_sources({})
    assert result == []


def test_get_aliases_returns_user_and_my_aliases():
    config = {
        "user_aliases": [{"canonical_name": "Jane", "aliases": ["jane"]}],
        "my_aliases": ["john", "johnd"],
    }
    user_aliases, my_aliases = get_aliases(config)
    assert len(user_aliases) == 1
    assert user_aliases[0]["canonical_name"] == "Jane"
    assert my_aliases == ["john", "johnd"]


def test_get_aliases_returns_empty_lists_when_absent():
    user_aliases, my_aliases = get_aliases({})
    assert user_aliases == []
    assert my_aliases == []


def test_get_sources_skips_disabled():
    config = {
        "sources": {
            "disabled_one": {"enabled": False},
            "enabled_one": {"enabled": True},
            "default_enabled": {},  # enabled: True by default
        }
    }
    names = [name for name, _ in get_sources(config)]
    assert "disabled_one" not in names
    assert "enabled_one" in names
    assert "default_enabled" in names


def test_get_internal_domains_missing_returns_empty():
    assert get_internal_domains({}) == []
    assert get_internal_domains({"internal_domains": None}) == []
    assert get_internal_domains({"internal_domains": []}) == []


def test_get_internal_domains_lowercases_and_trims():
    cfg = {"internal_domains": ["  MyCompany.com  ", "Other.org"]}
    assert get_internal_domains(cfg) == ["mycompany.com", "other.org"]


def test_get_internal_domains_strips_leading_at():
    cfg = {"internal_domains": ["@mycompany.com", "@other.org"]}
    assert get_internal_domains(cfg) == ["mycompany.com", "other.org"]


def test_get_internal_domains_drops_empty_and_none_entries():
    cfg = {"internal_domains": ["mycompany.com", "", None, "   ", "other.org"]}
    assert get_internal_domains(cfg) == ["mycompany.com", "other.org"]


# ── get_ingest_settings ────────────────────────────────────────────

def test_get_ingest_settings_defaults():
    s = get_ingest_settings({})
    assert s == {"fetch_workers": None, "max_fetch_workers": 8, "lookback_minutes": 75}


def test_get_ingest_settings_explicit_workers_passed_through():
    s = get_ingest_settings({"ingest": {"fetch_workers": 4}})
    assert s["fetch_workers"] == 4


def test_get_ingest_settings_zero_means_auto():
    s = get_ingest_settings({"ingest": {"fetch_workers": 0}})
    assert s["fetch_workers"] is None


def test_get_ingest_settings_one_keeps_legacy_path():
    s = get_ingest_settings({"ingest": {"fetch_workers": 1}})
    assert s["fetch_workers"] == 1


def test_get_ingest_settings_negative_clamped_to_one():
    s = get_ingest_settings({"ingest": {"fetch_workers": -3}})
    assert s["fetch_workers"] == 1


def test_get_ingest_settings_garbage_workers_falls_back_to_auto():
    s = get_ingest_settings({"ingest": {"fetch_workers": "many"}})
    assert s["fetch_workers"] is None


def test_get_ingest_settings_max_workers_override():
    s = get_ingest_settings({"ingest": {"max_fetch_workers": 16}})
    assert s["max_fetch_workers"] == 16


def test_get_ingest_settings_garbage_max_workers_falls_back():
    s = get_ingest_settings({"ingest": {"max_fetch_workers": "infinity"}})
    assert s["max_fetch_workers"] == 8


def test_get_ingest_settings_lookback_explicit():
    s = get_ingest_settings({"ingest": {"lookback_minutes": 30}})
    assert s["lookback_minutes"] == 30


def test_get_ingest_settings_lookback_clamped_to_one():
    s = get_ingest_settings({"ingest": {"lookback_minutes": 0}})
    assert s["lookback_minutes"] == 1


def test_get_ingest_settings_garbage_lookback_falls_back():
    s = get_ingest_settings({"ingest": {"lookback_minutes": "soon"}})
    assert s["lookback_minutes"] == 75


# ── get_alias_edit_settings ────────────────────────────────────────

def test_get_alias_edit_settings_defaults():
    s = get_alias_edit_settings({})
    assert s == {
        "allow_alias_edits": True,
        "max_entries": 500,
        "max_aliases_per_entry": 50,
        "max_list_fields": 50,
    }


def test_get_alias_edit_settings_disable_flag():
    s = get_alias_edit_settings({"allow_alias_edits": False})
    assert s["allow_alias_edits"] is False


def test_get_alias_edit_settings_overrides_caps():
    s = get_alias_edit_settings({"max_entries": 100, "max_aliases_per_entry": 20})
    assert s["max_entries"] == 100
    assert s["max_aliases_per_entry"] == 20
    assert s["max_list_fields"] == 50  # untouched


def test_get_alias_edit_settings_garbage_cap_falls_back():
    s = get_alias_edit_settings({"max_entries": "lots"})
    assert s["max_entries"] == 500


def test_get_alias_edit_settings_negative_cap_clamped_to_one():
    s = get_alias_edit_settings({"max_aliases_per_entry": -5})
    assert s["max_aliases_per_entry"] == 1


# ── get_retention_settings ─────────────────────────────────────────

def test_get_retention_settings_block_absent_means_disabled():
    """Operator hasn't opted in → housekeeping does nothing on a fresh install."""
    s = get_retention_settings({})
    assert s["retention_days"] is None
    assert s["prune_interval_hours"] == 24


def test_get_retention_settings_explicit_null_is_disabled():
    s = get_retention_settings({"retention": None})
    assert s["retention_days"] is None


def test_get_retention_settings_block_present_applies_defaults():
    s = get_retention_settings({"retention": {}})
    assert s["retention_days"] == 365
    assert s["prune_interval_hours"] == 24


def test_get_retention_settings_explicit_zero_in_block_is_disabled():
    s = get_retention_settings({"retention": {"retention_days": 0}})
    assert s["retention_days"] is None


def test_get_retention_settings_explicit_value():
    s = get_retention_settings({"retention": {"retention_days": 90, "prune_interval_hours": 6}})
    assert s["retention_days"] == 90
    assert s["prune_interval_hours"] == 6


def test_get_retention_settings_garbage_days_falls_back_to_default():
    s = get_retention_settings({"retention": {"retention_days": "lots"}})
    assert s["retention_days"] == 365


def test_get_retention_settings_negative_days_clamped_to_default_not_disabled():
    """Silently disabling is a worse failure than over-keeping."""
    s = get_retention_settings({"retention": {"retention_days": -7}})
    assert s["retention_days"] == 365


def test_get_retention_settings_garbage_interval_falls_back():
    s = get_retention_settings({"retention": {"retention_days": 90, "prune_interval_hours": "soon"}})
    assert s["prune_interval_hours"] == 24


def test_get_retention_settings_file_cache_grace_default():
    """Once the block exists, the file-cache grace defaults to 60 minutes."""
    s = get_retention_settings({"retention": {}})
    assert s["file_cache_grace_minutes"] == 60


def test_get_retention_settings_file_cache_grace_override():
    s = get_retention_settings({"retention": {"file_cache_grace_minutes": 5}})
    assert s["file_cache_grace_minutes"] == 5


def test_get_retention_settings_file_cache_grace_garbage_falls_back():
    s = get_retention_settings({"retention": {"file_cache_grace_minutes": "soon"}})
    assert s["file_cache_grace_minutes"] == 60


def test_get_retention_settings_file_cache_grace_negative_clamped_to_zero():
    s = get_retention_settings({"retention": {"file_cache_grace_minutes": -10}})
    assert s["file_cache_grace_minutes"] == 0


def test_get_retention_settings_block_absent_still_returns_grace_key():
    """The key must always exist in the returned dict — callers index it."""
    s = get_retention_settings({})
    assert s["file_cache_grace_minutes"] == 60
