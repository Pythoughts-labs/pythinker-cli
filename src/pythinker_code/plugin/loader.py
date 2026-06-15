"""Discover and load artifact-contributing plugins across ecosystems.

A plugin root is any directory whose ``plugin.json`` is resolvable via
:func:`pythinker_code.plugin.manifest.find_plugin_manifest`. Discovery walks the
pythinker plugin cache plus the read-only Claude/Codex roots, identifies plugin
roots at any reasonable depth (marketplaces nest as
``<marketplace>/<plugin>/<version>/``), and loads each manifest.

Discovery is fail-open at the *collection* level — one malformed plugin never
aborts the scan — but fail-closed per plugin: a plugin that fails to parse is
recorded as an error and contributes nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pythinker_code.plugin.directories import (
    claude_plugin_roots,
    codex_plugin_roots,
    plugin_cache_dir,
)
from pythinker_code.plugin.manifest import (
    PluginManifest,
    PluginManifestError,
    find_plugin_manifest,
    load_plugin_manifest,
)

PluginOrigin = Literal["pythinker", "claude", "codex", "builtin"]

# Plugins nest at most a few levels under a root (marketplace/plugin/version).
_MAX_DISCOVERY_DEPTH = 4


@dataclass(frozen=True)
class LoadedPlugin:
    """A successfully loaded plugin and where it came from."""

    name: str
    root: Path
    manifest: PluginManifest
    origin: PluginOrigin
    enabled: bool = True


@dataclass(frozen=True)
class PluginLoadError:
    """A plugin root that could not be loaded."""

    root: Path
    message: str


@dataclass(frozen=True)
class PluginLoadResult:
    """Outcome of a discovery pass: loaded plugins plus per-plugin errors."""

    plugins: list[LoadedPlugin]
    errors: list[PluginLoadError]

    @property
    def enabled(self) -> list[LoadedPlugin]:
        return [p for p in self.plugins if p.enabled]


def _iter_plugin_roots(base: Path, *, max_depth: int = _MAX_DISCOVERY_DEPTH) -> Iterator[Path]:
    """Yield directories under *base* that contain a plugin manifest.

    Stops descending once a plugin root is found (no nested plugins inside a
    plugin) and skips dot-directories as descent targets (the manifest's own
    ``.claude-plugin``/``.pythinker-plugin`` dir is still inspected on the parent).
    """
    if not base.is_dir():
        return
    stack: list[tuple[Path, int]] = [(base, 0)]
    while stack:
        current, depth = stack.pop()
        if find_plugin_manifest(current) is not None:
            yield current
            continue
        if depth >= max_depth:
            continue
        try:
            children = [c for c in current.iterdir() if c.is_dir() and not c.name.startswith(".")]
        except OSError:
            continue
        for child in children:
            stack.append((child, depth + 1))


def _roots_by_origin() -> list[tuple[Path, PluginOrigin]]:
    roots: list[tuple[Path, PluginOrigin]] = [(plugin_cache_dir(), "pythinker")]
    for root in claude_plugin_roots():
        roots.append((root, "claude"))
    for root in codex_plugin_roots():
        roots.append((root, "codex"))
    return roots


def discover_plugins(
    *,
    include_external: bool = True,
    is_enabled: set[str] | None = None,
) -> PluginLoadResult:
    """Discover and load all plugins.

    Args:
        include_external: also scan the read-only Claude/Codex roots.
        is_enabled: if given, only plugins whose name is in the set are marked
            enabled (others load but are disabled). ``None`` enables all — the
            pre-marketplace default until per-scope enable-state lands.

    Later origins never override an already-loaded plugin name: the pythinker
    cache wins over Claude/Codex, so a plugin installed natively takes priority.
    """
    plugins: list[LoadedPlugin] = []
    errors: list[PluginLoadError] = []
    seen: set[str] = set()
    roots: list[tuple[Path, PluginOrigin]] = _roots_by_origin()
    if not include_external:
        roots = [pair for pair in roots if pair[1] == "pythinker"]

    for base, origin in roots:
        for plugin_root in _iter_plugin_roots(base):
            try:
                manifest = load_plugin_manifest(plugin_root)
            except PluginManifestError as exc:
                errors.append(PluginLoadError(root=plugin_root, message=str(exc)))
                continue
            if manifest.name in seen:
                continue
            seen.add(manifest.name)
            enabled = True if is_enabled is None else manifest.name in is_enabled
            plugins.append(
                LoadedPlugin(
                    name=manifest.name,
                    root=plugin_root,
                    manifest=manifest,
                    origin=origin,
                    enabled=enabled,
                )
            )
    return PluginLoadResult(plugins=plugins, errors=errors)
