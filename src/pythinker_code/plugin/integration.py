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

import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from pythinker_code.hooks.config import HOOK_EVENT_TYPES, HookDef
from pythinker_code.plugin import artifacts
from pythinker_code.plugin.directories import plugin_data_dir
from pythinker_code.plugin.loader import LoadedPlugin, discover_plugins
from pythinker_code.plugin.options import UserConfigError, substitute_user_config_vars
from pythinker_code.plugin.policy import PluginPolicy, current_plugin_policy
from pythinker_code.utils.logging import logger


def _enabled_plugins(policy: PluginPolicy | None, *, include_external: bool) -> list[LoadedPlugin]:
    """Discover the plugins this session activates under *policy* (or the active one)."""
    pol = policy if policy is not None else current_plugin_policy()
    enabled_set = set(pol.enabled) if pol.enabled is not None else None
    plugins = discover_plugins(include_external=include_external, is_enabled=enabled_set).enabled
    if pol.disabled:
        plugins = [plugin for plugin in plugins if plugin.name not in pol.disabled]
    return plugins


def _safe_external(policy: PluginPolicy | None) -> bool:
    """Whether safe (model-invoked) artifacts may come from external plugins."""
    pol = policy if policy is not None else current_plugin_policy()
    return pol.discover_external


def _exec_external(policy: PluginPolicy | None) -> bool:
    """Whether executable (auto-running) artifacts may come from external plugins."""
    pol = policy if policy is not None else current_plugin_policy()
    return pol.discover_external and pol.external_exec


def _safe_artifact_dirs(
    policy: PluginPolicy | None, extract: Callable[[LoadedPlugin], list[Path]]
) -> list[Path]:
    """Collect a safe (model-invoked) artifact's dirs across enabled plugins."""
    dirs: list[Path] = []
    for plugin in _enabled_plugins(policy, include_external=_safe_external(policy)):
        dirs.extend(extract(plugin))
    return dirs


def plugin_skill_dirs(policy: PluginPolicy | None = None) -> list[Path]:
    """Skill roots contributed by enabled plugins (each a ``skills/``-style dir)."""
    return _safe_artifact_dirs(policy, artifacts.skill_dirs)


def plugin_agent_dirs(policy: PluginPolicy | None = None) -> list[Path]:
    """Subagent-definition roots contributed by enabled plugins (``agents/`` dirs)."""
    return _safe_artifact_dirs(policy, artifacts.agent_dirs)


def plugin_command_dirs(policy: PluginPolicy | None = None) -> list[Path]:
    """Slash-command prompt-template roots contributed by enabled plugins."""
    return _safe_artifact_dirs(policy, artifacts.command_dirs)


def _expand_plugin_vars(text: str, plugin: LoadedPlugin) -> str:
    """Expand ``${...PLUGIN_ROOT}`` / ``${...PLUGIN_DATA}`` to this plugin's dirs.

    Both the Claude (``CLAUDE_*``) and pythinker (``PYTHINKER_*``) spellings are
    accepted for cross-ecosystem compatibility.

    Trust boundary: the paths are interpolated unquoted, mirroring the reference
    (which runs hooks via a shell too). Safe because native plugin roots are
    sanitized at install (``_SAFE_NAME``) so they cannot hold shell metacharacters,
    and external (Claude/Codex) hooks/MCP only run when ``external_exec`` is opted
    in. Do not add shlex.quote here: it would diverge and break ``${ROOT}/x --flag``.
    """
    root = str(plugin.root)
    data = str(plugin_data_dir(plugin.name))
    return (
        text.replace("${CLAUDE_PLUGIN_ROOT}", root)
        .replace("${PYTHINKER_PLUGIN_ROOT}", root)
        .replace("${CLAUDE_PLUGIN_DATA}", data)
        .replace("${PYTHINKER_PLUGIN_DATA}", data)
    )


def _map_strings(value: object, transform: Callable[[str], str]) -> object:
    """Apply *transform* to every string in a nested config value."""
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, list):
        return [_map_strings(item, transform) for item in cast("list[object]", value)]
    if isinstance(value, dict):
        return {
            key: _map_strings(val, transform)
            for key, val in cast("dict[str, object]", value).items()
        }
    return value


def _plugin_options(plugin: LoadedPlugin, policy: PluginPolicy | None) -> dict[str, object]:
    """User-config values configured for *plugin* (empty if none)."""
    pol = policy if policy is not None else current_plugin_policy()
    return pol.options.get(plugin.name, {})


def plugin_mcp_servers(policy: PluginPolicy | None = None) -> dict[str, object]:
    """MCP server configs contributed by enabled plugins (earlier plugins win).

    Executable artifact: external plugins contribute only when ``external_exec``.
    ``${...PLUGIN_ROOT}``/``${...PLUGIN_DATA}`` are expanded so a plugin can point
    at its own bundled server, and ``${user_config.KEY}`` is filled from config. A
    server referencing an unconfigured option is skipped (fail-soft), not run blank.
    """
    servers: dict[str, object] = {}
    for plugin in _enabled_plugins(policy, include_external=_exec_external(policy)):
        options = _plugin_options(plugin, policy) if plugin.manifest.user_config else None
        for key, value in artifacts.mcp_servers(plugin).items():
            expanded = _map_strings(value, partial(_expand_plugin_vars, plugin=plugin))
            if options is not None:
                try:
                    expanded = _map_strings(
                        expanded, partial(substitute_user_config_vars, values=options)
                    )
                except UserConfigError as exc:
                    logger.warning(
                        "Skipping MCP server {key} from {plugin}: unconfigured user_config {error}",
                        key=key,
                        plugin=plugin.name,
                        error=exc,
                    )
                    continue
            servers.setdefault(key, expanded)
    return servers


def _hooks_payload(plugin: LoadedPlugin) -> dict[str, Any] | None:
    """Return a plugin's raw hooks mapping (inline manifest object or hooks.json)."""
    inline = plugin.manifest.hooks
    if isinstance(inline, dict):
        return cast("dict[str, Any]", inline)
    hooks_path = artifacts.hooks_file(plugin)
    if hooks_path is None:
        return None
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Skipping unreadable plugin hooks {path}: {error}", path=hooks_path, error=exc
        )
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


def _translate_hook_defs(
    plugin: LoadedPlugin, payload: dict[str, Any], options: dict[str, object] | None
) -> list[HookDef]:
    """Translate a Claude-style hooks mapping into pythinker ``HookDef`` entries.

    Shape: ``{event: [{matcher, hooks: [{type: command, command, timeout}]}]}``,
    optionally wrapped in a top-level ``"hooks"`` key. Only ``command`` hooks are
    supported; ``${...PLUGIN_ROOT}``/``${...PLUGIN_DATA}`` expand to the plugin's
    dirs and ``${user_config.KEY}`` is filled from *options* (a hook referencing an
    unconfigured option is skipped). Malformed entries are skipped, never fatal.
    """
    raw = payload.get("hooks", payload)
    if not isinstance(raw, dict):
        return []
    defs: list[HookDef] = []
    for event, groups in cast("dict[str, Any]", raw).items():
        if event not in HOOK_EVENT_TYPES or not isinstance(groups, list):
            continue
        for group in cast("list[Any]", groups):
            if not isinstance(group, dict):
                continue
            group_d = cast("dict[str, Any]", group)
            matcher = group_d.get("matcher", "")
            entries = group_d.get("hooks", [])
            if not isinstance(entries, list):
                continue
            for entry in cast("list[Any]", entries):
                if not isinstance(entry, dict):
                    continue
                entry_d = cast("dict[str, Any]", entry)
                command = entry_d.get("command")
                if entry_d.get("type", "command") != "command" or not isinstance(command, str):
                    continue
                expanded = _expand_plugin_vars(command, plugin)
                if options is not None:
                    try:
                        expanded = substitute_user_config_vars(expanded, options)
                    except UserConfigError as exc:
                        logger.warning(
                            "Skipping {event} hook in {plugin}: unconfigured user_config {error}",
                            event=event,
                            plugin=plugin.name,
                            error=exc,
                        )
                        continue
                timeout = entry_d.get("timeout")
                try:
                    defs.append(
                        HookDef(
                            event=cast("Any", event),
                            command=expanded,
                            matcher=matcher if isinstance(matcher, str) else "",
                            **({"timeout": timeout} if isinstance(timeout, int) else {}),
                        )
                    )
                except ValidationError as exc:
                    logger.warning(
                        "Skipping invalid plugin hook in {plugin}: {error}",
                        plugin=plugin.name,
                        error=exc,
                    )
    return defs


def plugin_hook_defs(policy: PluginPolicy | None = None) -> list[HookDef]:
    """Lifecycle hooks contributed by enabled plugins, as pythinker ``HookDef``s.

    Executable artifact: external plugins contribute only when ``external_exec``.
    """
    defs: list[HookDef] = []
    for plugin in _enabled_plugins(policy, include_external=_exec_external(policy)):
        payload = _hooks_payload(plugin)
        if payload is not None:
            options = _plugin_options(plugin, policy) if plugin.manifest.user_config else None
            defs.extend(_translate_hook_defs(plugin, payload, options))
    return defs
