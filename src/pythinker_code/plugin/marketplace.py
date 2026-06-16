"""Marketplace registry: state, source parsing, and local resolution.

Mirrors the reference (``blackbox/pythinker-src`` ``utils/plugins``): a
*marketplace* is a named catalog of plugins. Configured marketplaces are tracked
in ``known_marketplaces.json`` as ``{name: {source, installLocation,
lastUpdated, autoUpdate}}``; each ``source`` is a discriminated union
(github/git/url/file/directory/npm).

State I/O is fail-soft (a corrupt entry is skipped, never crashes discovery) and
writes are atomic (temp + ``os.replace``) so a concurrent reader never sees a
torn file. Network/git fetching lives in a separate phase; this module covers
state and local (file/directory) sources.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from pythinker_code.plugin.directories import known_marketplaces_file, marketplaces_cache_dir
from pythinker_code.plugin.manifest import (
    MarketplaceManifest,
    find_marketplace_manifest,
    load_marketplace_manifest,
)
from pythinker_code.utils.logging import logger

MarketplaceSourceKind = Literal["github", "git", "url", "file", "directory", "npm"]


class MarketplaceError(Exception):
    """Raised for an invalid marketplace source or unresolvable marketplace."""


class MarketplaceSource(BaseModel):
    """Where a marketplace's manifest comes from."""

    model_config = ConfigDict(extra="ignore")

    source: MarketplaceSourceKind
    repo: str | None = None  # github "owner/repo"
    url: str | None = None  # git/url
    path: str | None = None  # file/directory (absolute)
    ref: str | None = None  # git ref / branch
    package: str | None = None  # npm


class KnownMarketplaceEntry(BaseModel):
    """One configured marketplace in ``known_marketplaces.json``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    source: MarketplaceSource
    install_location: str | None = Field(default=None, alias="installLocation")
    last_updated: str | None = Field(default=None, alias="lastUpdated")
    auto_update: bool = Field(default=False, alias="autoUpdate")


def parse_marketplace_input(raw: str) -> MarketplaceSource:
    """Parse a user-supplied marketplace string into a typed source.

    Mirrors the reference rules: SSH/git URLs, ``github.com`` URLs and
    ``owner/repo`` shorthand → git/github; ``.git`` or ``/_git/`` → git; other
    http(s) → url; local ``.json`` file → file; local dir → directory. Raises
    :class:`MarketplaceError` for a missing path or an unrecognized input.
    """
    trimmed = raw.strip()
    if not trimmed:
        raise MarketplaceError("Empty marketplace source")

    # SSH form: user@host:path(.git)?(#ref)?
    import re

    ssh = re.match(r"^([a-zA-Z0-9._-]+@[^:]+:.+?(?:\.git)?)(#(.+))?$", trimmed)
    if ssh:
        return MarketplaceSource(source="git", url=ssh.group(1), ref=ssh.group(3))

    if trimmed.startswith(("http://", "https://")):
        frag = re.match(r"^([^#]+)(#(.+))?$", trimmed)
        url = frag.group(1) if frag else trimmed
        ref = frag.group(3) if frag else None
        if url.endswith(".git") or "/_git/" in url:
            return MarketplaceSource(source="git", url=url, ref=ref)
        if re.match(r"^https?://(www\.)?github\.com/[^/]+/[^/]+", url):
            git_url = url if url.endswith(".git") else f"{url}.git"
            return MarketplaceSource(source="git", url=git_url, ref=ref)
        return MarketplaceSource(source="url", url=url, ref=ref)

    if trimmed.startswith(("./", "../", "/", "~")):
        resolved = Path(trimmed).expanduser().resolve()
        if not resolved.exists():
            raise MarketplaceError(f"Path does not exist: {resolved}")
        if resolved.is_file():
            if resolved.suffix != ".json":
                raise MarketplaceError(f"Marketplace file must be .json: {resolved}")
            return MarketplaceSource(source="file", path=str(resolved))
        if resolved.is_dir():
            return MarketplaceSource(source="directory", path=str(resolved))
        raise MarketplaceError(f"Cannot use path: {resolved}")

    # Shorthand owner/repo[(#|@)ref] -> github
    if "/" in trimmed and not trimmed.startswith("@"):
        if ":" in trimmed:
            raise MarketplaceError(f"Unrecognized marketplace source: {trimmed}")
        m = re.match(r"^([^#@]+)(?:[#@](.+))?$", trimmed)
        repo = m.group(1) if m else trimmed
        ref = m.group(2) if m else None
        return MarketplaceSource(source="github", repo=repo, ref=ref)

    raise MarketplaceError(f"Unrecognized marketplace source: {trimmed}")


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (temp file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_known_marketplaces() -> dict[str, KnownMarketplaceEntry]:
    """Load configured marketplaces, skipping any malformed entry (fail-soft)."""
    path = known_marketplaces_file()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read known_marketplaces.json: {error}", error=exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, KnownMarketplaceEntry] = {}
    for name, entry in cast("dict[str, Any]", raw).items():
        try:
            result[name] = KnownMarketplaceEntry.model_validate(entry)
        except Exception as exc:
            logger.warning("Skipping invalid marketplace '{name}': {error}", name=name, error=exc)
    return result


def save_known_marketplaces(marketplaces: dict[str, KnownMarketplaceEntry]) -> None:
    """Persist the marketplace registry atomically."""
    payload = {
        name: entry.model_dump(by_alias=True, exclude_none=True)
        for name, entry in marketplaces.items()
    }
    _atomic_write_json(known_marketplaces_file(), payload)


def add_marketplace(name: str, source: MarketplaceSource, *, auto_update: bool = False) -> None:
    """Register (or replace) a marketplace by name."""
    marketplaces = load_known_marketplaces()
    install_location = str(marketplaces_cache_dir() / name)
    marketplaces[name] = KnownMarketplaceEntry(
        source=source,
        installLocation=install_location,
        autoUpdate=auto_update,
    )
    save_known_marketplaces(marketplaces)


def remove_marketplace(name: str) -> bool:
    """Unregister a marketplace. Returns True if it existed."""
    marketplaces = load_known_marketplaces()
    if name not in marketplaces:
        return False
    del marketplaces[name]
    save_known_marketplaces(marketplaces)
    return True


def resolve_local_marketplace(source: MarketplaceSource) -> MarketplaceManifest:
    """Load a marketplace manifest from a local file/directory source.

    git/url sources require fetching (a later phase); calling this on them raises.
    """
    if source.source == "file":
        if not source.path:
            raise MarketplaceError("file marketplace source missing path")
        return load_marketplace_manifest(Path(source.path))
    if source.source == "directory":
        if not source.path:
            raise MarketplaceError("directory marketplace source missing path")
        manifest_path = find_marketplace_manifest(Path(source.path))
        if manifest_path is None:
            raise MarketplaceError(f"No marketplace.json under {source.path}")
        return load_marketplace_manifest(manifest_path)
    raise MarketplaceError(f"Source '{source.source}' requires fetching, not local resolution")
