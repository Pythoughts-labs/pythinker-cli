"""Load LSP server configs from installed plugins."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from pythinker_code.config import LspServerConfig
from pythinker_code.plugin.directories import plugin_data_dir
from pythinker_code.plugin.integration import (  # pyright: ignore[reportPrivateUsage]
    _enabled_plugins,
    _exec_external,
    _expand_plugin_vars,
    _plugin_options,
)
from pythinker_code.plugin.loader import LoadedPlugin
from pythinker_code.plugin.options import UserConfigError, substitute_user_config_vars
from pythinker_code.plugin.policy import PluginPolicy, current_plugin_policy
from pythinker_code.utils.logging import logger

_LSP_FILE = ".lsp.json"
_ENV_VAR = re.compile(r"\$\{([^}]+)\}")


def _safe_join(root: Path, relative: str) -> Path | None:
    root_resolved = root.resolve()
    candidate = (root_resolved / relative).resolve()
    if candidate == root_resolved or root_resolved in candidate.parents:
        return candidate
    return None


def _expand_env_vars(text: str) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` using the process environment."""

    def _replace(match: re.Match[str]) -> str:
        var_content = match.group(1)
        var_name, _, default = var_content.partition(":-")
        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        if default:
            return default
        return match.group(0)

    return _ENV_VAR.sub(_replace, text)


def _parse_server_configs(raw: object, *, plugin: str, source: str) -> dict[str, LspServerConfig]:
    """Parse a name -> config map from JSON data."""
    if not isinstance(raw, dict):
        logger.warning(
            "Skipping LSP config {source} from {plugin}: expected object",
            source=source,
            plugin=plugin,
        )
        return {}
    servers: dict[str, LspServerConfig] = {}
    for name, config in cast("dict[str, object]", raw).items():
        if not isinstance(config, dict):
            logger.warning(
                "Skipping LSP server {name} from {plugin}: invalid config",
                name=name,
                plugin=plugin,
            )
            continue
        try:
            servers[name] = LspServerConfig.model_validate(config)
        except ValidationError as exc:
            logger.warning(
                "Skipping LSP server {name} from {plugin}: {error}",
                name=name,
                plugin=plugin,
                error=exc,
            )
    return servers


def _load_lsp_json_file(path: Path, *, plugin: str, source: str) -> dict[str, LspServerConfig]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Skipping unreadable LSP config {source} from {plugin}: {error}",
            source=source,
            plugin=plugin,
            error=exc,
        )
        return {}
    return _parse_server_configs(raw, plugin=plugin, source=source)


def _load_from_manifest_declaration(
    declaration: str | dict[str, object] | list[str | dict[str, object]],
    plugin: LoadedPlugin,
) -> dict[str, LspServerConfig]:
    servers: dict[str, LspServerConfig] = {}
    declarations = declaration if isinstance(declaration, list) else [declaration]
    for decl in declarations:
        if isinstance(decl, str):
            validated = _safe_join(plugin.root, decl)
            if validated is None:
                logger.warning(
                    "Skipping LSP path traversal in {plugin}: {path}",
                    plugin=plugin.name,
                    path=decl,
                )
                continue
            if not validated.is_file():
                logger.warning(
                    "Skipping missing LSP config file in {plugin}: {path}",
                    plugin=plugin.name,
                    path=decl,
                )
                continue
            servers.update(
                _load_lsp_json_file(validated, plugin=plugin.name, source=decl),
            )
        else:
            for name, config in decl.items():
                if not isinstance(config, dict):
                    logger.warning(
                        "Skipping inline LSP server {name} from {plugin}: invalid config",
                        name=name,
                        plugin=plugin.name,
                    )
                    continue
                try:
                    servers[name] = LspServerConfig.model_validate(config)
                except ValidationError as exc:
                    logger.warning(
                        "Skipping inline LSP server {name} from {plugin}: {error}",
                        name=name,
                        plugin=plugin.name,
                        error=exc,
                    )
    return servers


def _load_plugin_lsp_servers(plugin: LoadedPlugin) -> dict[str, LspServerConfig]:
    """Load raw LSP server configs from ``.lsp.json`` and ``manifest.lspServers``."""
    servers: dict[str, LspServerConfig] = {}
    lsp_path = plugin.root / _LSP_FILE
    if lsp_path.is_file():
        servers.update(_load_lsp_json_file(lsp_path, plugin=plugin.name, source=_LSP_FILE))
    declaration = plugin.manifest.lsp_servers
    if declaration is not None:
        servers.update(_load_from_manifest_declaration(declaration, plugin))
    return servers


def _resolve_server_config(
    config: LspServerConfig,
    plugin: LoadedPlugin,
    *,
    options: dict[str, object] | None,
) -> LspServerConfig | None:
    def resolve_value(text: str) -> str:
        resolved = _expand_plugin_vars(text, plugin)
        if options is not None:
            resolved = substitute_user_config_vars(resolved, options)
        return _expand_env_vars(resolved)

    try:
        command = resolve_value(config.command)
        args = [resolve_value(arg) for arg in config.args]
        env: dict[str, str] = {
            "PYTHINKER_PLUGIN_ROOT": str(plugin.root),
            "PYTHINKER_PLUGIN_DATA": str(plugin_data_dir(plugin.name)),
        }
        for key, value in config.env.items():
            if key in ("PYTHINKER_PLUGIN_ROOT", "PYTHINKER_PLUGIN_DATA"):
                env[key] = value
            else:
                env[key] = resolve_value(value)
    except UserConfigError as exc:
        logger.warning(
            "Skipping LSP server from {plugin}: unconfigured user_config {error}",
            plugin=plugin.name,
            error=exc,
        )
        return None

    return config.model_copy(update={"command": command, "args": args, "env": env})


def _scope_server_name(plugin_name: str, server_name: str) -> str:
    return f"plugin:{plugin_name}:{server_name}"


def plugin_lsp_servers(policy: PluginPolicy | None = None) -> dict[str, LspServerConfig]:
    """LSP server configs contributed by enabled plugins (earlier plugins win).

    Executable artifact: external plugins contribute only when ``external_exec``.
    Server names are scoped as ``plugin:<plugin>:<server>`` to avoid collisions.
    """
    pol = policy if policy is not None else current_plugin_policy()
    servers: dict[str, LspServerConfig] = {}
    for plugin in _enabled_plugins(pol, include_external=_exec_external(pol)):
        try:
            raw_servers = _load_plugin_lsp_servers(plugin)
            if not raw_servers:
                continue
            options = _plugin_options(plugin, pol) if plugin.manifest.user_config else None
            for name, config in raw_servers.items():
                resolved = _resolve_server_config(config, plugin, options=options)
                if resolved is None:
                    continue
                scoped = _scope_server_name(plugin.name, name)
                servers.setdefault(scoped, resolved)
        except Exception as exc:
            logger.warning(
                "Skipping LSP servers from plugin {plugin}: {error}",
                plugin=plugin.name,
                error=exc,
            )
    return servers
