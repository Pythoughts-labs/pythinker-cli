"""Bridge installed plugins into pythinker's artifact-discovery paths.

Each collector runs one discovery pass and maps the enabled plugins to the
concrete artifact locations the rest of the app already knows how to consume.
This keeps the wiring in the host modules (``skill``, ``subagents``, ``soul``)
to a single call each. Collectors are added here as each consumer is wired.

Trust posture: external (Claude/Codex) plugins contribute executable agents,
hooks, and MCP servers, so they must not auto-activate just by being installed
for another tool. Collectors therefore default to pythinker-owned plugins only;
opting into external plugins (and per-plugin enable-state) is config-gated and
wired in a later phase.

ponytail: discovery is a bounded filesystem walk over a few roots, so each
collector re-runs it rather than sharing a cached pass. Add a session-scoped
cache only if startup profiling shows it matters.
"""

from __future__ import annotations

from pathlib import Path

from pythinker_code.plugin import artifacts
from pythinker_code.plugin.loader import discover_plugins


def plugin_skill_dirs(
    *, include_external: bool = False, is_enabled: set[str] | None = None
) -> list[Path]:
    """Skill roots contributed by enabled plugins (each a ``skills/``-style dir)."""
    enabled = discover_plugins(include_external=include_external, is_enabled=is_enabled).enabled
    dirs: list[Path] = []
    for plugin in enabled:
        dirs.extend(artifacts.skill_dirs(plugin))
    return dirs


def plugin_agent_dirs(
    *, include_external: bool = False, is_enabled: set[str] | None = None
) -> list[Path]:
    """Subagent-definition roots contributed by enabled plugins (``agents/`` dirs)."""
    enabled = discover_plugins(include_external=include_external, is_enabled=is_enabled).enabled
    dirs: list[Path] = []
    for plugin in enabled:
        dirs.extend(artifacts.agent_dirs(plugin))
    return dirs


def plugin_command_dirs(
    *, include_external: bool = False, is_enabled: set[str] | None = None
) -> list[Path]:
    """Slash-command prompt-template roots contributed by enabled plugins."""
    enabled = discover_plugins(include_external=include_external, is_enabled=is_enabled).enabled
    dirs: list[Path] = []
    for plugin in enabled:
        dirs.extend(artifacts.command_dirs(plugin))
    return dirs


def plugin_mcp_servers(
    *, include_external: bool = False, is_enabled: set[str] | None = None
) -> dict[str, object]:
    """MCP server configs contributed by enabled plugins (earlier plugins win)."""
    enabled = discover_plugins(include_external=include_external, is_enabled=is_enabled).enabled
    servers: dict[str, object] = {}
    for plugin in enabled:
        for key, value in artifacts.mcp_servers(plugin).items():
            servers.setdefault(key, value)
    return servers
