"""Install a plugin from a configured marketplace into the versioned cache.

Resolves the marketplace manifest (local file/directory, or fetched git/url),
finds the named plugin entry, materializes its source into
``cache/<marketplace>/<plugin>/<version>/``, and records the install. Plugin and
marketplace names are sanitized before they touch the filesystem, and copied
sources are confined to the marketplace root (no ``../`` escape).

ponytail: git uses a shallow ``git clone`` subprocess (works offline against a
local repo path); npm/pip plugin sources are not yet supported and raise a clear
error rather than silently doing nothing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from datetime import UTC
from pathlib import Path
from typing import cast

from pythinker_code.plugin.dependency import parse_plugin_identifier
from pythinker_code.plugin.directories import (
    external_installed_plugin_dirs,
    external_marketplace_dirs,
    marketplaces_cache_dir,
    plugin_cache_dir,
)
from pythinker_code.plugin.installed import (
    InstalledRecord,
    load_installed_plugins,
    plugin_identifier,
    record_install,
    remove_install,
)
from pythinker_code.plugin.manifest import (
    MarketplaceEntry,
    MarketplaceManifest,
    PluginManifestError,
    find_marketplace_manifest,
    find_plugin_manifest,
    load_marketplace_manifest,
    load_plugin_manifest,
)
from pythinker_code.plugin.marketplace import (
    KnownMarketplaceEntry,
    MarketplaceError,
    MarketplaceSource,
    load_known_marketplaces,
    resolve_local_marketplace,
    save_known_marketplaces,
)
from pythinker_code.utils.logging import logger

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_GIT_TIMEOUT_S = 120
# Allowed git transports. Excludes exec-capable transports (``ext::``, ``fd::``)
# so a marketplace-controlled URL cannot smuggle a command, and excludes the
# plaintext ``http://``/``git://`` transports (unauthenticated, tamperable in
# transit — a supply-chain risk for executable plugin content). ``file://`` is
# permitted for local/offline clones.
_SAFE_GIT_URL = re.compile(r"^(https://|ssh://|git@|file://)")


def _safe_name(name: str, kind: str) -> str:
    """Reject names that are unsafe as a path segment."""
    if not _SAFE_NAME.match(name) or name in {".", ".."}:
        raise MarketplaceError(f"Unsafe {kind} name: {name!r}")
    return name


def _git_clone(url: str, ref: str | None, dest: Path) -> None:
    """Shallow-clone *url* into *dest*. Raises :class:`MarketplaceError` on failure.

    Hardened against argv flag-smuggling and exec-capable transports: the ref may
    not begin with ``-``, the URL must use an allowed scheme, and ``--`` ends
    option parsing before the positional ``url``/``dest`` reach git.
    """
    if not _SAFE_GIT_URL.match(url):
        raise MarketplaceError(f"Unsafe or unsupported git URL: {url!r}")
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        if ref.startswith("-"):
            raise MarketplaceError(f"Unsafe git ref: {ref!r}")
        cmd += ["--branch", ref]
    cmd += ["--", url, str(dest)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MarketplaceError(f"git clone failed for {url}: {exc}") from exc
    if result.returncode != 0:
        raise MarketplaceError(f"git clone failed for {url}: {result.stderr.strip()}")


def _remove_path(path: Path) -> None:
    """Remove a file, symlink, or directory, surfacing failures.

    ``shutil.rmtree`` refuses symlinks, so a leftover symlink at a cache dest
    would survive an ``ignore_errors`` cleanup and then break the next clone/copy.
    Handle symlinks explicitly and raise (don't silently ignore) on failure.
    """
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
    except OSError as exc:
        raise MarketplaceError(f"Could not remove existing path {path}: {exc}") from exc


def _symlink(dest: Path, target: Path) -> None:
    """Point *dest* at *target* via a directory symlink (replacing any existing dest)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.exists():
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest, ignore_errors=True)
        else:
            dest.unlink(missing_ok=True)
    dest.symlink_to(target, target_is_directory=True)


def _reuse_external_plugin(
    marketplace_name: str, plugin_name: str, dest_parent: Path
) -> Path | None:
    """If Claude/Codex already has this plugin, symlink to it instead of fetching.

    Returns the symlinked dest dir (named for the reused version) or None when no
    external install exists. Avoids duplicating plugin contents on disk.
    """
    for external in external_installed_plugin_dirs(marketplace_name, plugin_name):
        if find_plugin_manifest(external) is None:
            continue
        dest = dest_parent / external.name  # reuse the external version label
        _symlink(dest, external)
        logger.info("Reusing external plugin install via symlink: {target}", target=external)
        return dest
    return None


def _marketplace_repo_url(source: MarketplaceSource) -> str:
    if source.source == "github":
        if not source.repo:
            raise MarketplaceError("github marketplace source missing repo")
        return f"https://github.com/{source.repo}.git"
    if source.source == "git":
        if not source.url:
            raise MarketplaceError("git marketplace source missing url")
        return source.url
    raise MarketplaceError(f"Cannot fetch marketplace source '{source.source}'")


def _load_marketplace(name: str, entry: KnownMarketplaceEntry) -> tuple[MarketplaceManifest, Path]:
    """Return (manifest, marketplace_root) for a configured marketplace."""
    source = entry.source
    if source.source in ("file", "directory"):
        manifest = resolve_local_marketplace(source)
        if source.source == "file":
            root = Path(source.path).parent if source.path else Path()
        else:
            root = Path(source.path or "")
        return manifest, root
    if source.source in ("github", "git"):
        dest = marketplaces_cache_dir() / _safe_name(name, "marketplace")
        # No redundancy: reuse a Claude/Codex clone of this marketplace if present.
        external = external_marketplace_dirs(name)
        if external:
            _symlink(dest, external[0])
        else:
            _remove_path(dest)
            _git_clone(_marketplace_repo_url(source), source.ref, dest)
        manifest_path = find_marketplace_manifest(dest)
        if manifest_path is None:
            raise MarketplaceError(f"No marketplace.json in fetched marketplace '{name}'")
        return load_marketplace_manifest(manifest_path), dest
    raise MarketplaceError(f"Unsupported marketplace source '{source.source}'")


def _find_entry(manifest: MarketplaceManifest, plugin_name: str) -> MarketplaceEntry:
    for entry in manifest.plugins:
        if entry.name == plugin_name:
            return entry
    raise MarketplaceError(f"Plugin '{plugin_name}' not found in marketplace '{manifest.name}'")


def _materialize_plugin_source(
    entry: MarketplaceEntry, marketplace_root: Path, dest: Path
) -> str | None:
    """Copy/clone the plugin's source into *dest*. Returns a git SHA when known."""
    source = entry.source
    if isinstance(source, str):
        # Relative path under the marketplace repo root.
        resolved = (marketplace_root / source).resolve()
        root_resolved = marketplace_root.resolve()
        if resolved != root_resolved and root_resolved not in resolved.parents:
            raise MarketplaceError(f"Plugin source escapes marketplace root: {source}")
        if not resolved.is_dir():
            raise MarketplaceError(f"Plugin source not found: {resolved}")
        shutil.copytree(resolved, dest, ignore=shutil.ignore_patterns(".git"))
        return None
    if isinstance(source, dict):
        source_d = cast("dict[str, object]", source)
        kind = source_d.get("source")
        if kind in ("github", "git"):
            url = (
                f"https://github.com/{source_d.get('repo')}.git"
                if kind == "github"
                else source_d.get("url")
            )
            if not url:
                raise MarketplaceError("git plugin source missing url/repo")
            if not isinstance(url, str):
                raise MarketplaceError("git plugin source url must be a string")
            ref = source_d.get("ref")
            tmp = Path(tempfile.mkdtemp(prefix="pythinker-plugin-"))
            try:
                _git_clone(url, ref if isinstance(ref, str) else None, tmp / "repo")
                shutil.copytree(tmp / "repo", dest, ignore=shutil.ignore_patterns(".git"))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return None
        raise MarketplaceError(f"Unsupported plugin source kind: {kind!r}")
    raise MarketplaceError(f"Plugin '{entry.name}' has no usable source")


def resolve_marketplace_catalog(name: str) -> MarketplaceManifest:
    """Load a configured marketplace's catalog (fetching git/url sources)."""
    marketplaces = load_known_marketplaces()
    if name not in marketplaces:
        raise MarketplaceError(f"Marketplace not configured: {name}")
    manifest, _root = _load_marketplace(name, marketplaces[name])
    return manifest


def refresh_marketplace(name: str) -> int:
    """Re-resolve a marketplace and stamp its ``lastUpdated``. Returns plugin count."""
    from datetime import datetime

    marketplaces = load_known_marketplaces()
    if name not in marketplaces:
        raise MarketplaceError(f"Marketplace not configured: {name}")
    manifest, _root = _load_marketplace(name, marketplaces[name])
    marketplaces[name].last_updated = datetime.now(UTC).isoformat()
    save_known_marketplaces(marketplaces)
    return len(manifest.plugins)


def install_plugin_from_marketplace(
    plugin_name: str, marketplace_name: str, *, scope: str = "user"
) -> InstalledRecord:
    """Install ``plugin_name`` from ``marketplace_name`` into the versioned cache.

    Raises :class:`MarketplaceError` if the marketplace is not configured, the
    plugin is absent, or its source cannot be materialized.
    """
    plugin_name = _safe_name(plugin_name, "plugin")
    marketplace_name = _safe_name(marketplace_name, "marketplace")

    known = load_known_marketplaces()
    if marketplace_name not in known:
        raise MarketplaceError(f"Marketplace not configured: {marketplace_name}")

    # No redundancy: if Claude/Codex already has this plugin on disk, symlink to
    # it and skip resolving/fetching the marketplace entirely.
    reused = _reuse_external_plugin(
        marketplace_name, plugin_name, plugin_cache_dir() / marketplace_name / plugin_name
    )
    if reused is not None:
        record = InstalledRecord(scope=scope, installPath=str(reused), version=reused.name)
        record_install(plugin_name, marketplace_name, record)
        return record

    manifest, marketplace_root = _load_marketplace(marketplace_name, known[marketplace_name])
    installed: dict[str, InstalledRecord] = {}
    # Records materialized so far, oldest first. If dependency resolution fails
    # partway through (e.g. a cross-marketplace dep), every plugin already written
    # to disk/registry is rolled back so a failed install never leaves partial state.
    materialized: list[tuple[str, InstalledRecord]] = []
    try:
        _install_with_deps(
            plugin_name,
            marketplace_name,
            manifest,
            marketplace_root,
            scope,
            installed,
            [],
            materialized,
        )
    except Exception:
        _rollback_installs(marketplace_name, materialized)
        raise
    return installed[plugin_name]


def _rollback_installs(
    marketplace_name: str, materialized: list[tuple[str, InstalledRecord]]
) -> None:
    """Undo partially completed installs (best-effort), newest first.

    Runs while unwinding a failed install, so a cleanup error must not mask the
    original failure — each step is logged and continued rather than raised.
    """
    for name, record in reversed(materialized):
        ident = plugin_identifier(name, marketplace_name)
        try:
            _remove_path(Path(record.install_path))
            remove_install(name, marketplace_name, scope=record.scope)
        except Exception as exc:
            logger.warning(
                "Could not fully roll back partial install of {id}: {error}",
                id=ident,
                error=exc,
            )


def _materialize_and_record(
    plugin_name: str,
    marketplace_name: str,
    manifest: MarketplaceManifest,
    marketplace_root: Path,
    scope: str,
) -> InstalledRecord:
    """Fetch one plugin's source into the versioned cache and record the install."""
    entry = _find_entry(manifest, plugin_name)
    version = _safe_name(entry.version or "unknown", "version")

    dest = plugin_cache_dir() / marketplace_name / plugin_name / version
    _remove_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        sha = _materialize_plugin_source(entry, marketplace_root, dest)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)  # don't leave a half-written plugin
        raise

    record = InstalledRecord(scope=scope, installPath=str(dest), version=version, gitCommitSha=sha)
    record_install(plugin_name, marketplace_name, record)
    logger.info(
        "Installed plugin {id} -> {dest}",
        id=plugin_identifier(plugin_name, marketplace_name),
        dest=dest,
    )
    return record


def _installed_manifest_deps(install_path: Path) -> list[str]:
    """Read a just-installed plugin's normalized ``dependencies`` (fail-soft)."""
    try:
        return load_plugin_manifest(install_path).dependencies
    except PluginManifestError:
        return []


def _install_with_deps(
    plugin_name: str,
    marketplace_name: str,
    manifest: MarketplaceManifest,
    marketplace_root: Path,
    scope: str,
    installed: dict[str, InstalledRecord],
    in_progress: list[str],
    materialized: list[tuple[str, InstalledRecord]],
) -> None:
    """Install a plugin and its transitive dependencies from the same marketplace.

    Dependencies are resolved from each plugin's own ``plugin.json`` after it is
    materialized (marketplace entries don't carry them). Cross-marketplace
    dependencies are blocked (install them from their own marketplace first), and
    cycles are detected via the active install path. Already-installed
    dependencies are skipped.
    """
    if plugin_name in installed:
        return
    if plugin_name in in_progress:
        cycle = " -> ".join([*in_progress, plugin_name])
        raise MarketplaceError(f"Plugin dependency cycle: {cycle}")

    in_progress.append(plugin_name)
    record = _materialize_and_record(
        plugin_name, marketplace_name, manifest, marketplace_root, scope
    )
    materialized.append((plugin_name, record))
    for dep in _installed_manifest_deps(Path(record.install_path)):
        dep_name, dep_marketplace = parse_plugin_identifier(dep)
        if dep_marketplace is not None and dep_marketplace != marketplace_name:
            raise MarketplaceError(
                f"Cross-marketplace dependency '{dep}' of '{plugin_name}' is not allowed; "
                f"install it from '{dep_marketplace}' first."
            )
        dep_name = _safe_name(dep_name, "plugin")
        if dep_name in installed or plugin_identifier(dep_name, marketplace_name) in (
            load_installed_plugins()
        ):
            continue
        _install_with_deps(
            dep_name,
            marketplace_name,
            manifest,
            marketplace_root,
            scope,
            installed,
            in_progress,
            materialized,
        )
    in_progress.remove(plugin_name)
    installed[plugin_name] = record
