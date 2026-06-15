"""Resolve the concrete artifacts a loaded plugin contributes.

For each artifact kind, the manifest may name explicit paths; otherwise a
convention directory under the plugin root is used (``skills/``, ``commands/``,
``agents/``, ``hooks/hooks.json``, ``.mcp.json``). All resolved paths are
constrained to the plugin root — a manifest cannot point at files outside its
own directory (path-traversal guard).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from pythinker_code.plugin.loader import LoadedPlugin

# Convention directories/files relative to a plugin root.
_SKILLS_DIR = "skills"
_COMMANDS_DIR = "commands"
_AGENTS_DIR = "agents"
_HOOKS_FILE = "hooks/hooks.json"
_MCP_FILE = ".mcp.json"


def _safe_join(root: Path, relative: str) -> Path | None:
    """Resolve *relative* under *root*, or None if it escapes the plugin root."""
    root_resolved = root.resolve()
    candidate = (root_resolved / relative).resolve()
    if candidate == root_resolved or root_resolved in candidate.parents:
        return candidate
    return None


def _resolve_dirs(plugin: LoadedPlugin, overrides: list[str], convention: str) -> list[Path]:
    """Resolve a directory-valued artifact: explicit overrides or the convention dir."""
    if overrides:
        resolved = [_safe_join(plugin.root, rel) for rel in overrides]
        return [path for path in resolved if path is not None and path.is_dir()]
    convention_dir = plugin.root / convention
    return [convention_dir] if convention_dir.is_dir() else []


def skill_dirs(plugin: LoadedPlugin) -> list[Path]:
    """Directories containing this plugin's skills."""
    return _resolve_dirs(plugin, plugin.manifest.skills, _SKILLS_DIR)


def command_dirs(plugin: LoadedPlugin) -> list[Path]:
    """Directories containing this plugin's slash-command prompt templates."""
    return _resolve_dirs(plugin, plugin.manifest.commands, _COMMANDS_DIR)


def agent_dirs(plugin: LoadedPlugin) -> list[Path]:
    """Directories containing this plugin's subagent definitions."""
    return _resolve_dirs(plugin, plugin.manifest.agents, _AGENTS_DIR)


def hooks_file(plugin: LoadedPlugin) -> Path | None:
    """Path to this plugin's hooks JSON, if it declares one.

    Inline ``hooks`` objects in the manifest are not file-backed; callers read
    :attr:`PluginManifest.hooks` directly for those. This returns only a
    convention/override *file* path that exists.
    """
    hooks = plugin.manifest.hooks
    if isinstance(hooks, str):
        path = _safe_join(plugin.root, hooks)
        return path if path is not None and path.is_file() else None
    convention = plugin.root / _HOOKS_FILE
    return convention if convention.is_file() else None


def mcp_servers(plugin: LoadedPlugin) -> dict[str, object]:
    """MCP server configs contributed by this plugin.

    Merges the manifest ``mcpServers`` map with a convention ``.mcp.json`` file
    (manifest entries win on key collision). Returns an empty dict when neither
    is present or the convention file is malformed (fail-closed: a broken file
    contributes nothing rather than crashing discovery).
    """
    servers: dict[str, object] = {}
    mcp_path = plugin.root / _MCP_FILE
    if mcp_path.is_file():
        try:
            raw = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            data = cast("dict[str, object]", raw)
            # ``.mcp.json`` may nest servers under "mcpServers" or be a bare map.
            inner = data.get("mcpServers", data)
            if isinstance(inner, dict):
                for key, value in cast("dict[str, object]", inner).items():
                    servers[key] = value
    servers.update(plugin.manifest.mcp_servers)
    return servers
