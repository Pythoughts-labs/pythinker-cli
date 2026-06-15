"""End-to-end: installed plugins contribute artifacts to discovery paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pythinker_host.path import HostPath

from pythinker_code.plugin import integration, loader


def _install_plugin_with_skill(cache: Path, plugin: str, skill: str) -> Path:
    """Create a Claude-style plugin with a nested skill under *cache*."""
    root = cache / plugin / plugin / "1.0.0"
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"name": plugin, "version": "1.0.0"}), encoding="utf-8")
    skill_md = root / "skills" / skill / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(
        f"---\nname: {skill}\ndescription: {skill} does things\n---\n# {skill}\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def _no_external(monkeypatch):
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])


def test_plugin_skill_dirs_finds_nested_skill(tmp_path: Path, monkeypatch, _no_external) -> None:
    cache = tmp_path / "cache"
    root = _install_plugin_with_skill(cache, "ponytail", "ponytail")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)

    dirs = integration.plugin_skill_dirs()
    assert root / "skills" in dirs


def test_disabled_plugin_contributes_no_skills(tmp_path: Path, monkeypatch, _no_external) -> None:
    cache = tmp_path / "cache"
    _install_plugin_with_skill(cache, "ponytail", "ponytail")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)

    # Enable-set excludes the plugin -> no skill dirs.
    assert integration.plugin_skill_dirs(is_enabled=set()) == []


@pytest.mark.asyncio
async def test_resolve_skills_roots_includes_plugin_skill_dir(
    tmp_path: Path, monkeypatch, _no_external
) -> None:
    from pythinker_code.skill import discover_skills, resolve_skills_roots

    cache = tmp_path / "cache"
    root = _install_plugin_with_skill(cache, "ponytail", "ponytail")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)
    # Isolate user/project discovery to an empty home.
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    work_dir = HostPath.unsafe_from_local_path(tmp_path / "project")
    roots = await resolve_skills_roots(work_dir)
    root_paths = {str(r.root) for r in roots}
    assert str((root / "skills").resolve()) in root_paths

    # And the nested skill is actually discoverable from that root.
    skills = await discover_skills(HostPath.unsafe_from_local_path(root / "skills"), scope="extra")
    assert any(s.name == "ponytail" for s in skills)


def _install_plugin_with_agent(cache: Path, plugin: str, agent: str) -> Path:
    root = cache / plugin / plugin / "1.0.0"
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"name": plugin, "version": "1.0.0"}), encoding="utf-8")
    agent_md = root / "agents" / f"{agent}.md"
    agent_md.parent.mkdir(parents=True, exist_ok=True)
    agent_md.write_text(
        f"---\nname: {agent}\ndescription: {agent} agent\n---\nPrompt body\n", encoding="utf-8"
    )
    return root


def test_plugin_agent_dirs_finds_agents(tmp_path: Path, monkeypatch, _no_external) -> None:
    cache = tmp_path / "cache"
    root = _install_plugin_with_agent(cache, "tools", "reviewer")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)
    assert root / "agents" in integration.plugin_agent_dirs()


@pytest.mark.asyncio
async def test_resolve_agent_roots_includes_plugin_agents(
    tmp_path: Path, monkeypatch, _no_external
) -> None:
    from pythinker_code.subagents.discovery import discover_markdown_agents, resolve_agent_roots

    cache = tmp_path / "cache"
    _install_plugin_with_agent(cache, "tools", "reviewer")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    roots = await resolve_agent_roots(HostPath.unsafe_from_local_path(tmp_path / "project"))
    assert any(r.scope == "plugin" for r in roots)
    agents = await discover_markdown_agents(roots)
    assert any(a.name == "reviewer" for a in agents)


def _install_plugin_with_command(cache: Path, plugin: str, command: str) -> Path:
    root = cache / plugin / plugin / "1.0.0"
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"name": plugin, "version": "1.0.0"}), encoding="utf-8")
    cmd_md = root / "commands" / f"{command}.md"
    cmd_md.parent.mkdir(parents=True, exist_ok=True)
    cmd_md.write_text(f"---\ndescription: {command} command\n---\nDo {command}\n", encoding="utf-8")
    return root


@pytest.mark.asyncio
async def test_discover_prompt_templates_includes_plugin_commands(
    tmp_path: Path, monkeypatch, _no_external
) -> None:
    from pythinker_code.prompt_templates import discover_prompt_templates

    cache = tmp_path / "cache"
    _install_plugin_with_command(cache, "tools", "ship")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    templates = await discover_prompt_templates(HostPath.unsafe_from_local_path(tmp_path / "proj"))
    assert "ship" in templates
    assert templates["ship"].scope == "plugin"


def test_external_plugins_are_opt_in(tmp_path: Path, monkeypatch) -> None:
    # A plugin only in the Claude root is ignored by default, included on opt-in.
    claude = tmp_path / "claude"
    _install_plugin_with_skill(claude, "ponytail", "ponytail")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [claude])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])

    assert integration.plugin_skill_dirs() == []  # default: external off
    assert integration.plugin_skill_dirs(include_external=True)  # opt-in finds it
