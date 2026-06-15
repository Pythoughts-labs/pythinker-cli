"""Plugin and marketplace manifest schemas (Claude/Codex/pythinker compatible).

A *plugin* is a directory that contributes reusable artifacts — skills, slash
commands, agents, hooks, MCP servers — to the agent. Its manifest may live in
``.pythinker-plugin/``, ``.claude-plugin/``, or ``.codex-plugin/`` (or at the
plugin root), all named ``plugin.json``. The schema is a superset of the three
ecosystems; unknown fields are ignored so forward-compatible manifests still load.

This module is pure data + validation. Discovery, loading, and artifact
extraction live in :mod:`pythinker_code.plugin.loader` and
:mod:`pythinker_code.plugin.artifacts`. The legacy subprocess-tool ``PluginSpec``
in :mod:`pythinker_code.plugin` is unrelated and stays as-is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Manifest directory names tried in priority order, then the plugin root itself.
MANIFEST_DIRS: tuple[str, ...] = (".pythinker-plugin", ".claude-plugin", ".codex-plugin")
PLUGIN_MANIFEST = "plugin.json"
MARKETPLACE_MANIFEST = "marketplace.json"


class PluginManifestError(Exception):
    """Raised when a plugin/marketplace manifest is missing or malformed."""


class Author(BaseModel):
    """Plugin/marketplace author. Accepts a bare string or an object."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    email: str | None = None
    url: str | None = None


def _coerce_author(value: Any) -> Any:
    """Allow ``author`` to be a plain string (treated as the name)."""
    if isinstance(value, str):
        return {"name": value}
    return value


def _as_str_list(value: Any) -> list[str]:
    """Normalize a ``str | list[str] | None`` artifact path field to a list.

    The object-map form (``{name: {source: ...}}``) some Claude manifests use is
    not modeled here; convention-dir fallback in the loader still finds those
    artifacts, so we drop the explicit override rather than fail.
    ponytail: add object-map parsing only when a real plugin needs it.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in cast("list[object]", value) if isinstance(item, str)]
    return []


class PluginManifest(BaseModel):
    """Parsed ``plugin.json`` for an artifact-contributing plugin."""

    model_config = ConfigDict(extra="ignore")

    name: str
    version: str = ""
    description: str = ""
    author: Author | None = None
    homepage: str | None = None
    repository: str | None = None
    license: str | None = None
    keywords: list[str] = Field(default_factory=list)

    # Artifact path overrides (relative to the plugin root). An empty list means
    # "use the convention directory" (skills/, commands/, agents/, hooks/).
    commands: list[str] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    output_styles: list[str] = Field(default_factory=list, alias="outputStyles")

    # Hooks: inline object or path(s); resolved by artifacts.py. Kept raw and
    # narrowed at use sites (object, not Any, so it stays fully typed).
    hooks: object = None
    # MCP servers: name -> server config (or a path/ref resolved later).
    mcp_servers: dict[str, object] = Field(default_factory=dict, alias="mcpServers")

    # Plugin dependencies: "name" or "name@marketplace".
    dependencies: list[str] = Field(default_factory=list)

    @field_validator("author", mode="before")
    @classmethod
    def _author(cls, v: Any) -> Any:
        return _coerce_author(v)

    @field_validator("commands", "agents", "skills", "output_styles", mode="before")
    @classmethod
    def _paths(cls, v: Any) -> list[str]:
        return _as_str_list(v)


class MarketplaceEntry(BaseModel):
    """One plugin listed in a marketplace manifest."""

    model_config = ConfigDict(extra="ignore")

    name: str
    version: str = ""
    description: str = ""
    author: Author | None = None
    # Plugin source: a relative path string ("./plugins/foo") or a source object.
    source: object = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    strict: bool = True

    @field_validator("author", mode="before")
    @classmethod
    def _author(cls, v: Any) -> Any:
        return _coerce_author(v)


class MarketplaceMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str = ""
    description: str = ""
    plugin_root: str | None = Field(default=None, alias="pluginRoot")


class MarketplaceManifest(BaseModel):
    """Parsed ``marketplace.json``: a curated catalog of plugins."""

    model_config = ConfigDict(extra="ignore")

    name: str
    owner: Author | None = None
    metadata: MarketplaceMetadata = Field(default_factory=MarketplaceMetadata)
    # pyright strict flags list-of-model + default_factory as partially unknown;
    # same pydantic pattern handled at plugin/__init__.py PluginSpec.tools.
    plugins: list[MarketplaceEntry] = Field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]

    @field_validator("owner", mode="before")
    @classmethod
    def _owner(cls, v: Any) -> Any:
        return _coerce_author(v)


def find_plugin_manifest(plugin_root: Path) -> Path | None:
    """Return the path to a plugin's ``plugin.json``, or None if absent.

    Tries ``.pythinker-plugin/``, ``.claude-plugin/``, ``.codex-plugin/`` then the
    plugin root, so plugins authored for any of the three ecosystems load.
    """
    for manifest_dir in MANIFEST_DIRS:
        candidate = plugin_root / manifest_dir / PLUGIN_MANIFEST
        if candidate.is_file():
            return candidate
    root_manifest = plugin_root / PLUGIN_MANIFEST
    return root_manifest if root_manifest.is_file() else None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginManifestError(f"Failed to read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PluginManifestError(f"Manifest must be a JSON object: {path}")
    return cast("dict[str, Any]", data)


def load_plugin_manifest(plugin_root: Path) -> PluginManifest:
    """Locate and parse a plugin's manifest. Raises :class:`PluginManifestError`."""
    manifest_path = find_plugin_manifest(plugin_root)
    if manifest_path is None:
        raise PluginManifestError(f"No {PLUGIN_MANIFEST} found under {plugin_root}")
    data = _load_json(manifest_path)
    if "name" not in data:
        raise PluginManifestError(f"Missing required field 'name' in {manifest_path}")
    try:
        return PluginManifest.model_validate(data)
    except Exception as exc:  # pydantic ValidationError -> typed manifest error
        raise PluginManifestError(f"Invalid plugin manifest {manifest_path}: {exc}") from exc


def find_marketplace_manifest(root: Path) -> Path | None:
    """Return the path to a marketplace's ``marketplace.json``, or None."""
    for manifest_dir in MANIFEST_DIRS:
        candidate = root / manifest_dir / MARKETPLACE_MANIFEST
        if candidate.is_file():
            return candidate
    root_manifest = root / MARKETPLACE_MANIFEST
    return root_manifest if root_manifest.is_file() else None


def load_marketplace_manifest(path: Path) -> MarketplaceManifest:
    """Parse a marketplace manifest from an exact file path."""
    data = _load_json(path)
    if "name" not in data:
        raise PluginManifestError(f"Missing required field 'name' in {path}")
    try:
        return MarketplaceManifest.model_validate(data)
    except Exception as exc:
        raise PluginManifestError(f"Invalid marketplace manifest {path}: {exc}") from exc
