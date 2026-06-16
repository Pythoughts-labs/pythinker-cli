"""CLI tests for the `pythinker plugin marketplace` command group."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pythinker_code.cli.plugin import cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def _share_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path / "share"))


def _make_directory_marketplace(root: Path) -> Path:
    manifest = root / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {"name": "mk", "plugins": [{"name": "demo", "version": "1.0.0", "source": "./demo"}]}
        ),
        encoding="utf-8",
    )
    pm = root / "demo" / ".claude-plugin" / "plugin.json"
    pm.parent.mkdir(parents=True)
    pm.write_text(json.dumps({"name": "demo", "version": "1.0.0"}), encoding="utf-8")
    return root


def test_marketplace_add_list_remove(tmp_path: Path) -> None:
    market = _make_directory_marketplace(tmp_path / "market")

    result = runner.invoke(cli, ["marketplace", "add", str(market), "--name", "mk"])
    assert result.exit_code == 0, result.output
    assert "Added marketplace 'mk'" in result.output

    listed = runner.invoke(cli, ["marketplace", "list"])
    assert "mk" in listed.output

    removed = runner.invoke(cli, ["marketplace", "remove", "mk"])
    assert removed.exit_code == 0
    assert "mk" not in runner.invoke(cli, ["marketplace", "list"]).output


def test_marketplace_install_and_installed_and_uninstall(tmp_path: Path) -> None:
    market = _make_directory_marketplace(tmp_path / "market")
    runner.invoke(cli, ["marketplace", "add", str(market), "--name", "mk"])

    installed = runner.invoke(cli, ["marketplace", "install", "demo", "mk"])
    assert installed.exit_code == 0, installed.output
    assert "Installed 'demo@mk'" in installed.output

    listing = runner.invoke(cli, ["marketplace", "installed"])
    assert "demo@mk" in listing.output

    # name@marketplace form also works for uninstall.
    removed = runner.invoke(cli, ["marketplace", "uninstall", "demo@mk"])
    assert removed.exit_code == 0, removed.output
    assert "demo@mk" not in runner.invoke(cli, ["marketplace", "installed"]).output


def test_marketplace_install_unknown_errors(tmp_path: Path) -> None:
    result = runner.invoke(cli, ["marketplace", "install", "demo", "ghost"])
    assert result.exit_code == 1
    assert "not configured" in result.output


def test_marketplace_refresh(tmp_path: Path) -> None:
    market = _make_directory_marketplace(tmp_path / "market")
    runner.invoke(cli, ["marketplace", "add", str(market), "--name", "mk"])
    result = runner.invoke(cli, ["marketplace", "refresh", "mk"])
    assert result.exit_code == 0, result.output
    assert "1 plugin(s) available" in result.output


def test_marketplace_add_derives_name_from_github(tmp_path: Path) -> None:
    result = runner.invoke(cli, ["marketplace", "add", "anthropics/claude-plugins-official"])
    assert result.exit_code == 0, result.output
    assert "claude-plugins-official" in runner.invoke(cli, ["marketplace", "list"]).output


def test_plugin_disable_then_enable_roundtrip(tmp_path: Path) -> None:
    from pythinker_code.config import load_config

    result = runner.invoke(cli, ["disable", "ponytail"])
    assert result.exit_code == 0, result.output
    assert "Disabled plugin 'ponytail'" in result.output
    assert "ponytail" in load_config().plugins.disabled

    # Idempotent: disabling again is a no-op.
    assert "already disabled" in runner.invoke(cli, ["disable", "ponytail"]).output

    result = runner.invoke(cli, ["enable", "ponytail"])
    assert result.exit_code == 0, result.output
    assert "Enabled plugin 'ponytail'" in result.output
    assert "ponytail" not in load_config().plugins.disabled
