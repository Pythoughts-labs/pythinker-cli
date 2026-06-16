"""Tests for the plugin activation policy and config-driven wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pythinker_code.plugin import integration, loader
from pythinker_code.plugin.policy import (
    PluginPolicy,
    current_plugin_policy,
    policy_from_config,
    reset_plugin_policy,
    set_plugin_policy,
)


def test_policy_from_config_defaults() -> None:
    pol = policy_from_config(discover_external=True, external_exec=False, enabled=[])
    assert pol.discover_external is True
    assert pol.external_exec is False
    assert pol.enabled is None  # empty list -> all enabled


def test_policy_from_config_named_enable_set() -> None:
    pol = policy_from_config(discover_external=True, external_exec=True, enabled=["a", "b"])
    assert pol.enabled == frozenset({"a", "b"})
    assert pol.external_exec is True


def test_policy_from_config_blank_entries_mean_enable_all() -> None:
    # A stray [""] / whitespace-only entry must not silently disable every plugin:
    # blanks are dropped and an all-blank list collapses to None (enable all).
    def enabled_for(entries: list[str]) -> frozenset[str] | None:
        return policy_from_config(
            discover_external=True, external_exec=False, enabled=entries
        ).enabled

    assert enabled_for([""]) is None
    assert enabled_for(["  "]) is None
    assert enabled_for(["", " a "]) == frozenset({"a"})  # blanks dropped, real names survive


def test_policy_from_config_disabled_drops_blanks() -> None:
    pol = policy_from_config(
        discover_external=True, external_exec=False, enabled=[], disabled=["", " x "]
    )
    assert pol.disabled == frozenset({"x"})


def test_default_policy_auto_detects_external_safe_only() -> None:
    # The shipped default: external skills/commands/agents auto-detect; exec off.
    default = PluginPolicy()
    assert default.discover_external is True
    assert default.external_exec is False


def test_set_and_reset_policy() -> None:
    assert current_plugin_policy() == PluginPolicy()  # default
    token = set_plugin_policy(PluginPolicy(external_exec=True))
    try:
        assert current_plugin_policy().external_exec is True
    finally:
        reset_plugin_policy(token)
    assert current_plugin_policy() == PluginPolicy()


def _install_external_skill_plugin(claude_root: Path, name: str) -> None:
    root = claude_root / name / name / "1.0.0"
    pm = root / ".claude-plugin" / "plugin.json"
    pm.parent.mkdir(parents=True)
    pm.write_text(json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8")
    skill = root / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: {name}\ndescription: d\n---\n# {name}", encoding="utf-8")


@pytest.fixture
def _external_ponytail(tmp_path: Path, monkeypatch):
    claude = tmp_path / "claude"
    _install_external_skill_plugin(claude, "ponytail")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [claude])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])


def test_external_skills_auto_detected_by_default(_external_ponytail) -> None:
    # No config, no symlink: an external Claude plugin's skills are found.
    assert integration.plugin_skill_dirs()


def test_discover_external_false_ignores_external(_external_ponytail) -> None:
    token = set_plugin_policy(PluginPolicy(discover_external=False))
    try:
        assert integration.plugin_skill_dirs() == []
    finally:
        reset_plugin_policy(token)


def test_enable_filter_excludes_external(_external_ponytail) -> None:
    token = set_plugin_policy(
        policy_from_config(discover_external=True, external_exec=False, enabled=["other"])
    )
    try:
        assert integration.plugin_skill_dirs() == []
    finally:
        reset_plugin_policy(token)


def test_disabled_excludes_plugin(_external_ponytail) -> None:
    # ponytail auto-detects by default; disabling it by name turns it off.
    assert integration.plugin_skill_dirs()
    token = set_plugin_policy(PluginPolicy(disabled=frozenset({"ponytail"})))
    try:
        assert integration.plugin_skill_dirs() == []
    finally:
        reset_plugin_policy(token)


def _install_external_mcp_plugin(claude_root: Path, name: str) -> None:
    root = claude_root / name / name / "1.0.0"
    pm = root / ".claude-plugin" / "plugin.json"
    pm.parent.mkdir(parents=True)
    pm.write_text(
        json.dumps({"name": name, "version": "1.0.0", "mcpServers": {"db": {"command": "x"}}}),
        encoding="utf-8",
    )


def test_external_mcp_is_opt_in(tmp_path: Path, monkeypatch) -> None:
    claude = tmp_path / "claude"
    _install_external_mcp_plugin(claude, "dbplug")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [claude])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])

    # Default: external exec artifacts (MCP) are NOT activated.
    assert integration.plugin_mcp_servers() == {}
    # Opt-in via external_exec.
    token = set_plugin_policy(PluginPolicy(external_exec=True))
    try:
        assert integration.plugin_mcp_servers() == {"db": {"command": "x"}}
    finally:
        reset_plugin_policy(token)


@pytest.fixture(autouse=True)
def _reset_policy():
    yield
    set_plugin_policy(PluginPolicy())
