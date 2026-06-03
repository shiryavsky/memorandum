"""Tests for cli/__main__.py — the argparse dispatcher."""
import pytest
from unittest.mock import patch

from cli.__main__ import main


def test_main_routes_health_with_args():
    with patch("cli.health.run") as run:
        main(["health", "--config", "x.yaml", "--json"])
    run.assert_called_once_with(config_path="x.yaml", as_json=True)


def test_main_routes_aliases_refresh():
    with patch("cli.aliases.refresh") as refresh:
        main(["aliases", "refresh", "--config", "x.yaml", "--in-place"])
    refresh.assert_called_once_with(config_path="x.yaml", in_place=True, as_json=False)


def test_main_requires_command():
    with pytest.raises(SystemExit):
        main([])


def test_main_aliases_requires_action():
    with pytest.raises(SystemExit):
        main(["aliases"])


# ── prune ─────────────────────────────────────────────────────────

def test_main_routes_prune_dry_run_by_default():
    with patch("cli.prune.run") as run:
        main(["prune", "--config", "x.yaml"])
    run.assert_called_once_with(config_path="x.yaml", dry_run=True,
                                days_override=None, as_json=False)


def test_main_routes_prune_commit_flips_dry_run():
    with patch("cli.prune.run") as run:
        main(["prune", "--commit", "--config", "x.yaml"])
    run.assert_called_once_with(config_path="x.yaml", dry_run=False,
                                days_override=None, as_json=False)


def test_main_routes_prune_with_days_override():
    with patch("cli.prune.run") as run:
        main(["prune", "--commit", "--days", "30"])
    run.assert_called_once_with(config_path="config.yaml", dry_run=False,
                                days_override=30, as_json=False)


def test_main_prune_dry_run_and_commit_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        main(["prune", "--dry-run", "--commit"])


# ── dashboard ─────────────────────────────────────────────────────

def test_main_routes_dashboard_defaults():
    with patch("cli.dashboard.run") as run:
        main(["dashboard"])
    run.assert_called_once_with(config_path="config.yaml", refresh=5,
                                once=False, no_color=False, mock=False)


def test_main_routes_dashboard_with_overrides():
    with patch("cli.dashboard.run") as run:
        main(["dashboard", "--config", "x.yaml", "--refresh", "10",
              "--once", "--no-color", "--mock"])
    run.assert_called_once_with(config_path="x.yaml", refresh=10,
                                once=True, no_color=True, mock=True)
