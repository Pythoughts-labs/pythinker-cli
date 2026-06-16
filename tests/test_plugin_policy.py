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


def test_policy_from_config_empty_enables_all() -> None:
    pol = policy_from_config(include_external=True, enabled=[])
    assert pol.include_external is True
    assert pol.enabled is None  # empty list -> all enabled


def test_policy_from_config_named_enable_set() -> None:
    pol = policy_from_config(include_external=False, enabled=["a", "b"])
    assert pol.enabled == frozenset({"a", "b"})


def test_set_and_reset_policy() -> None:
    assert current_plugin_policy() == PluginPolicy()  # default
    token = set_plugin_policy(PluginPolicy(include_external=True))
    try:
        assert current_plugin_policy().include_external is True
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


def test_collector_honors_contextvar_policy(tmp_path: Path, monkeypatch) -> None:
    claude = tmp_path / "claude"
    _install_external_skill_plugin(claude, "ponytail")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [claude])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])

    # Default policy: external off -> nothing.
    assert integration.plugin_skill_dirs() == []

    # Config enables external -> the collector (no explicit arg) honors it.
    token = set_plugin_policy(policy_from_config(include_external=True, enabled=[]))
    try:
        assert integration.plugin_skill_dirs()  # finds the external plugin's skills
    finally:
        reset_plugin_policy(token)


def test_contextvar_enable_filter(tmp_path: Path, monkeypatch) -> None:
    claude = tmp_path / "claude"
    _install_external_skill_plugin(claude, "ponytail")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [claude])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])

    # External on, but enable-set excludes ponytail -> nothing.
    token = set_plugin_policy(policy_from_config(include_external=True, enabled=["something-else"]))
    try:
        assert integration.plugin_skill_dirs() == []
    finally:
        reset_plugin_policy(token)


@pytest.fixture(autouse=True)
def _reset_policy():
    # Guard against a leaked policy from a failed test.
    yield
    set_plugin_policy(PluginPolicy())
