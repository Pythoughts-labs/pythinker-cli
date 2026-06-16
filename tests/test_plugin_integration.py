"""End-to-end: installed plugins contribute artifacts to discovery paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pythinker_host.path import HostPath

from pythinker_code.plugin import integration, loader
from pythinker_code.plugin.policy import PluginPolicy


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
    assert integration.plugin_skill_dirs(PluginPolicy(enabled=frozenset())) == []


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


def test_plugin_mcp_servers_collects_from_manifest(
    tmp_path: Path, monkeypatch, _no_external
) -> None:
    cache = tmp_path / "cache"
    root = cache / "db" / "db" / "1.0.0"
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"name": "db", "version": "1.0.0", "mcpServers": {"pg": {"command": "pg-mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)

    servers = integration.plugin_mcp_servers()
    assert servers == {"pg": {"command": "pg-mcp"}}
    # Disabled -> contributes nothing.
    assert integration.plugin_mcp_servers(PluginPolicy(enabled=frozenset())) == {}


def test_plugin_mcp_servers_expands_plugin_root(tmp_path: Path, monkeypatch, _no_external) -> None:
    from typing import Any, cast

    cache = tmp_path / "cache"
    root = cache / "db" / "db" / "1.0.0"
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": "db",
                "version": "1.0.0",
                "mcpServers": {"pg": {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/srv.js"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)

    pg = cast("dict[str, Any]", integration.plugin_mcp_servers()["pg"])
    assert pg["args"] == [f"{root}/srv.js"]  # ${CLAUDE_PLUGIN_ROOT} expanded to the plugin root


def test_plugin_hook_defs_expands_plugin_data(tmp_path: Path, monkeypatch, _no_external) -> None:
    from pythinker_code.plugin.directories import plugin_data_dir

    cache = tmp_path / "cache"
    root = cache / "sp" / "sp" / "1.0.0"
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "sp", "version": "1.0.0"}), encoding="utf-8")
    hooks = root / "hooks" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "${PYTHINKER_PLUGIN_DATA}/r.sh"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)

    defs = integration.plugin_hook_defs()
    assert defs[0].command == f"{plugin_data_dir('sp')}/r.sh"


def test_plugin_hook_defs_translates_claude_hooks(
    tmp_path: Path, monkeypatch, _no_external
) -> None:
    cache = tmp_path / "cache"
    root = cache / "sp" / "sp" / "1.0.0"
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "sp", "version": "1.0.0"}), encoding="utf-8")
    hooks = root / "hooks" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "${CLAUDE_PLUGIN_ROOT}/run.sh start",
                                    "timeout": 12,
                                }
                            ],
                        }
                    ],
                    "BogusEvent": [{"hooks": [{"type": "command", "command": "x"}]}],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)

    defs = integration.plugin_hook_defs()
    assert len(defs) == 1  # BogusEvent dropped
    hook = defs[0]
    assert hook.event == "SessionStart"
    assert hook.matcher == "startup"
    assert hook.timeout == 12
    # ${CLAUDE_PLUGIN_ROOT} expanded to the plugin root.
    assert str(root) in hook.command
    assert "${CLAUDE_PLUGIN_ROOT}" not in hook.command


def test_plugin_hook_defs_inline_manifest_and_malformed_skip(
    tmp_path: Path, monkeypatch, _no_external
) -> None:
    cache = tmp_path / "cache"
    root = cache / "ip" / "ip" / "1.0.0"
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": "ip",
                "version": "1.0.0",
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {"type": "command", "command": "ok"},
                                {"type": "other", "command": "ignored"},
                                {"command": 123},
                            ]
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)

    defs = integration.plugin_hook_defs()
    assert [d.command for d in defs] == ["ok"]  # non-command + malformed dropped


def test_external_skills_auto_detected_but_disablable(tmp_path: Path, monkeypatch) -> None:
    # A plugin only in the Claude root: its skills auto-detect by default (no
    # symlink, no config), and discover_external=False turns it off.
    claude = tmp_path / "claude"
    _install_plugin_with_skill(claude, "ponytail", "ponytail")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [claude])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])

    assert integration.plugin_skill_dirs()  # default: auto-detected
    assert integration.plugin_skill_dirs(PluginPolicy(discover_external=False)) == []
