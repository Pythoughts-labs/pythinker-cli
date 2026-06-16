"""Recommend marketplace LSP plugins when a file extension has no installed server."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pythinker_code.config import Config, LspConfig
from pythinker_code.plugin.installed import load_installed_plugins, plugin_identifier
from pythinker_code.plugin.manifest import MarketplaceEntry
from pythinker_code.plugin.marketplace import (
    MarketplaceError,
    load_known_marketplaces,
    resolve_local_marketplace,
)
from pythinker_code.utils.logging import logger

MAX_IGNORED_COUNT = 5

OFFICIAL_MARKETPLACE_NAMES = frozenset(
    {
        "pythinker-code-marketplace",
        "pythinker-code-plugins",
        "pythinker-plugins-official",
        "pythoughts-marketplace",
        "pythoughts-plugins",
        "agent-skills",
        "life-sciences",
        "knowledge-work-plugins",
    }
)


@dataclass(frozen=True)
class LspPluginRecommendation:
    plugin_id: str
    plugin_name: str
    marketplace_name: str
    description: str | None
    is_official: bool
    extensions: list[str]
    command: str


@dataclass(frozen=True)
class _LspInfo:
    extensions: frozenset[str]
    command: str


def is_official_marketplace(name: str) -> bool:
    return name.lower() in OFFICIAL_MARKETPLACE_NAMES


def is_lsp_recommendations_disabled(config: LspConfig) -> bool:
    return (
        config.recommendation_disabled or config.recommendation_ignored_count >= MAX_IGNORED_COUNT
    )


def is_plugin_installed(plugin_id: str) -> bool:
    return plugin_id in load_installed_plugins()


def is_binary_installed(command: str) -> bool:
    binary = command.split()[0] if " " in command and not command.startswith("/") else command
    return shutil.which(binary) is not None


def _extract_from_server_config_record(server_configs: dict[str, object]) -> _LspInfo | None:
    extensions: set[str] = set()
    command: str | None = None
    for config in server_configs.values():
        if not isinstance(config, dict):
            continue
        config_d = cast("dict[str, object]", config)
        if command is None:
            cmd_raw = config_d.get("command")
            if isinstance(cmd_raw, str):
                command = cmd_raw
        ext_mapping = config_d.get("extensionToLanguage")
        if isinstance(ext_mapping, dict):
            for ext in cast("dict[str, object]", ext_mapping):
                extensions.add(ext.lower())
    if not command or not extensions:
        return None
    return _LspInfo(extensions=frozenset(extensions), command=command)


def _extract_lsp_info_from_manifest(
    lsp_servers: str | dict[str, object] | list[str | dict[str, object]] | None,
) -> _LspInfo | None:
    if lsp_servers is None:
        return None
    if isinstance(lsp_servers, str):
        return None
    if isinstance(lsp_servers, list):
        for item in lsp_servers:
            if isinstance(item, str):
                continue
            info = _extract_from_server_config_record(item)
            if info is not None:
                return info
        return None
    return _extract_from_server_config_record(lsp_servers)


def _lsp_plugins_from_marketplaces() -> dict[str, tuple[MarketplaceEntry, str, _LspInfo, bool]]:
    result: dict[str, tuple[MarketplaceEntry, str, _LspInfo, bool]] = {}
    for marketplace_name, entry in load_known_marketplaces().items():
        try:
            manifest = resolve_local_marketplace(entry.source)
        except MarketplaceError as exc:
            logger.debug(
                "Skipping marketplace {name} for LSP recommendation: {error}",
                name=marketplace_name,
                error=exc,
            )
            continue
        is_official = is_official_marketplace(marketplace_name)
        for plugin_entry in manifest.plugins:
            if plugin_entry.lsp_servers is None:
                continue
            lsp_info = _extract_lsp_info_from_manifest(plugin_entry.lsp_servers)
            if lsp_info is None:
                continue
            plugin_id = plugin_identifier(plugin_entry.name, marketplace_name)
            result[plugin_id] = (plugin_entry, marketplace_name, lsp_info, is_official)
    return result


def get_matching_lsp_plugins(
    file_path: str | Path,
    config: Config | LspConfig,
) -> list[LspPluginRecommendation]:
    """Return installable LSP plugin recommendations for *file_path*."""
    lsp_config = config.lsp if isinstance(config, Config) else config
    if is_lsp_recommendations_disabled(lsp_config):
        return []

    ext = Path(file_path).suffix.lower()
    if not ext:
        return []

    never_plugins = set(lsp_config.recommendation_never)
    installed_plugins = load_installed_plugins()
    all_lsp_plugins = _lsp_plugins_from_marketplaces()
    matching: list[tuple[MarketplaceEntry, str, _LspInfo, bool, str]] = []

    for plugin_id, (entry, marketplace_name, lsp_info, is_official) in all_lsp_plugins.items():
        if ext not in lsp_info.extensions:
            continue
        if plugin_id in never_plugins:
            continue
        if plugin_id in installed_plugins:
            continue
        matching.append((entry, marketplace_name, lsp_info, is_official, plugin_id))

    with_binary = [item for item in matching if is_binary_installed(item[2].command)]
    with_binary.sort(key=lambda item: (not item[3], item[4]))

    return [
        LspPluginRecommendation(
            plugin_id=plugin_id,
            plugin_name=entry.name,
            marketplace_name=marketplace_name,
            description=entry.description or None,
            is_official=is_official,
            extensions=sorted(lsp_info.extensions),
            command=lsp_info.command,
        )
        for entry, marketplace_name, lsp_info, is_official, plugin_id in with_binary
    ]


def add_to_never_suggest(config: LspConfig, plugin_id: str) -> LspConfig:
    if plugin_id in config.recommendation_never:
        return config
    return config.model_copy(
        update={"recommendation_never": [*config.recommendation_never, plugin_id]}
    )


def increment_ignored_count(config: LspConfig) -> LspConfig:
    return config.model_copy(
        update={"recommendation_ignored_count": config.recommendation_ignored_count + 1}
    )


def reset_ignored_count(config: LspConfig) -> LspConfig:
    if config.recommendation_ignored_count == 0:
        return config
    return config.model_copy(update={"recommendation_ignored_count": 0})
