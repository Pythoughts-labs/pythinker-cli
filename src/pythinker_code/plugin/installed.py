"""Installed-plugin registry (``installed_plugins.json``, schema v2).

Tracks which plugins are installed from marketplaces, keyed by the
``name@marketplace`` identifier, each with one or more scoped install records.
Mirrors the reference's v2 layout. I/O is fail-soft and atomic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from pythinker_code.plugin.directories import installed_plugins_file
from pythinker_code.utils.logging import logger

INSTALLED_SCHEMA_VERSION = 2


class InstalledRecord(BaseModel):
    """One scoped installation of a plugin."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    scope: str = "user"
    install_path: str = Field(alias="installPath")
    version: str = "unknown"
    installed_at: str | None = Field(default=None, alias="installedAt")
    last_updated: str | None = Field(default=None, alias="lastUpdated")
    git_commit_sha: str | None = Field(default=None, alias="gitCommitSha")


def plugin_identifier(name: str, marketplace: str) -> str:
    """Canonical ``name@marketplace`` identifier."""
    return f"{name}@{marketplace}"


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_installed_plugins() -> dict[str, list[InstalledRecord]]:
    """Load the install registry, skipping malformed records (fail-soft)."""
    path = installed_plugins_file()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read installed_plugins.json: {error}", error=exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    plugins_raw = cast("dict[str, Any]", raw).get("plugins", {})
    if not isinstance(plugins_raw, dict):
        return {}
    result: dict[str, list[InstalledRecord]] = {}
    for ident, records in cast("dict[str, Any]", plugins_raw).items():
        if not isinstance(records, list):
            continue
        parsed: list[InstalledRecord] = []
        for record in cast("list[Any]", records):
            try:
                parsed.append(InstalledRecord.model_validate(record))
            except Exception as exc:
                logger.warning("Skipping invalid install record for {id}: {e}", id=ident, e=exc)
        if parsed:
            result[ident] = parsed
    return result


def save_installed_plugins(plugins: dict[str, list[InstalledRecord]]) -> None:
    """Persist the install registry atomically in v2 layout."""
    payload = {
        "version": INSTALLED_SCHEMA_VERSION,
        "plugins": {
            ident: [r.model_dump(by_alias=True, exclude_none=True) for r in records]
            for ident, records in plugins.items()
        },
    }
    _atomic_write_json(installed_plugins_file(), payload)


def record_install(name: str, marketplace: str, record: InstalledRecord) -> None:
    """Add or replace an install record for ``name@marketplace`` in its scope."""
    plugins = load_installed_plugins()
    ident = plugin_identifier(name, marketplace)
    existing = [r for r in plugins.get(ident, []) if r.scope != record.scope]
    plugins[ident] = [*existing, record]
    save_installed_plugins(plugins)


def remove_install(name: str, marketplace: str, *, scope: str | None = None) -> bool:
    """Remove install records for a plugin (optionally only one scope).

    Returns True if anything was removed.
    """
    plugins = load_installed_plugins()
    ident = plugin_identifier(name, marketplace)
    if ident not in plugins:
        return False
    if scope is None:
        del plugins[ident]
    else:
        kept = [r for r in plugins[ident] if r.scope != scope]
        if len(kept) == len(plugins[ident]):
            return False
        if kept:
            plugins[ident] = kept
        else:
            del plugins[ident]
    save_installed_plugins(plugins)
    return True
