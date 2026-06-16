"""CLI commands for plugin management."""

from __future__ import annotations

import ipaddress
import queue
import socket
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urljoin, urlparse

import typer

from pythinker_code.plugin import PluginError

if TYPE_CHECKING:
    from pythinker_code.config import Config

cli = typer.Typer(help="Manage plugins.")

# Marketplace-based plugins (Claude/Codex compatible). Kept as a subgroup so the
# legacy subprocess-tool plugin commands above stay unchanged.
marketplace_cli = typer.Typer(help="Manage plugin marketplaces and marketplace plugins.")


def _parse_git_url(target: str) -> tuple[str, str | None, str | None]:
    """Parse a git URL into (clone_url, subpath, branch).

    Splits .git URLs at the .git boundary. For GitHub/GitLab short URLs,
    treats the first two path segments as owner/repo and the rest as subpath.
    Strips ``tree/{branch}/`` or ``-/tree/{branch}/`` prefixes from
    browser-copied URLs and returns the branch name.
    """
    # Path 1: URL contains .git followed by / or end-of-string
    idx = target.find(".git/")
    if idx == -1 and target.endswith(".git"):
        return target, None, None
    if idx != -1:
        clone_url = target[: idx + 4]  # up to and including ".git"
        rest = target[idx + 5 :]  # after ".git/"
        subpath = rest.strip("/") or None
        return clone_url, subpath, None

    # Path 2: GitHub/GitLab short URL (no .git)
    from urllib.parse import urlparse

    parsed = urlparse(target)
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        return target, None, None

    owner_repo = "/".join(segments[:2])
    clone_url = f"{parsed.scheme}://{parsed.netloc}/{owner_repo}"
    rest_segments = segments[2:]

    # GitLab uses /-/tree/{branch}/, strip leading "-"
    if rest_segments and rest_segments[0] == "-":
        rest_segments = rest_segments[1:]

    # Strip tree/{branch}/ prefix and extract branch
    branch: str | None = None
    if len(rest_segments) >= 2 and rest_segments[0] == "tree":
        branch = rest_segments[1]
        rest_segments = rest_segments[2:]

    subpath = "/".join(rest_segments) or None
    return clone_url, subpath, branch


def _extract_zip_to_plugin(zip_path: Path, tmp: Path) -> tuple[Path, Path]:
    """Extract zip_path into tmp and locate the plugin directory.

    Returns ``(plugin_dir, tmp)`` for cleanup by the caller. Rejects zip
    members whose paths escape ``tmp``. Searches the extraction root and
    one level deep for ``plugin.json``.
    """
    import shutil
    import zipfile

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                member_path = (tmp / member).resolve()
                if not member_path.is_relative_to(tmp.resolve()):
                    shutil.rmtree(tmp, ignore_errors=True)
                    typer.echo(f"Error: zip contains unsafe path: {member}", err=True)
                    raise typer.Exit(1)
            zf.extractall(tmp)
    except zipfile.BadZipFile as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        typer.echo(f"Error: invalid zip archive: {exc}", err=True)
        raise typer.Exit(1) from exc

    for candidate in [tmp] + sorted(tmp.iterdir()):
        if candidate.is_dir() and (candidate / "plugin.json").exists():
            return candidate, tmp
    dirs = [d for d in tmp.iterdir() if d.is_dir() and not d.name.startswith("_")]
    if len(dirs) == 1 and (dirs[0] / "plugin.json").exists():
        return dirs[0], tmp

    shutil.rmtree(tmp, ignore_errors=True)
    typer.echo("Error: No plugin.json found in zip", err=True)
    raise typer.Exit(1)


_MAX_PLUGIN_ZIP_REDIRECTS = 10
_MAX_PLUGIN_ZIP_SIZE = 100 * 1024 * 1024
_PLUGIN_DNS_LOOKUP_TIMEOUT_SECONDS = 5.0


def _resolve_plugin_download_host(host: str) -> list[Any]:
    """Resolve a plugin download hostname with a short timeout."""
    result_queue: queue.Queue[tuple[list[Any] | None, OSError | None]] = queue.Queue(maxsize=1)

    def _resolve() -> None:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            result_queue.put((None, exc))
        else:
            result_queue.put((list(infos), None))

    thread = threading.Thread(target=_resolve, daemon=True)
    thread.start()
    thread.join(_PLUGIN_DNS_LOOKUP_TIMEOUT_SECONDS)
    if thread.is_alive():
        raise TimeoutError(f"DNS lookup timed out for {host}")

    try:
        infos, error = result_queue.get_nowait()
    except queue.Empty as exc:
        raise TimeoutError(f"DNS lookup produced no result for {host}") from exc
    if error is not None:
        raise error
    return infos or []


def _is_disallowed_plugin_download_host(host: str | None) -> bool:
    """Return True for localhost/private IP targets blocked for plugin downloads."""
    if host is None:
        return True
    host = host.strip().strip("[]").lower()
    if host in {"localhost", "ip6-localhost", "ip6-loopback"} or host.endswith(".localhost"):
        return True

    def _is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )

    try:
        return _is_blocked(ipaddress.ip_address(host))
    except ValueError:
        pass

    # Resolve immediately before each request and reject if any resolved address is unsafe.
    # httpx still performs its own connect-time resolution, so this is a best-effort guard
    # against DNS aliases to local/private networks rather than a complete DNS-rebinding fix.
    try:
        infos = _resolve_plugin_download_host(host)
    except (OSError, TimeoutError):
        return True

    if not infos:
        return True

    for info in infos:
        address = info[4][0]
        if not isinstance(address, str):
            return True
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return True
        if _is_blocked(ip):
            return True
    return False


def _validate_plugin_download_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        typer.echo(f"Error: unsafe plugin download redirect scheme: {parsed.scheme}", err=True)
        raise typer.Exit(1)
    if _is_disallowed_plugin_download_host(parsed.hostname):
        host = parsed.hostname or "<missing>"
        typer.echo(f"Error: unsafe plugin download host: {host}", err=True)
        raise typer.Exit(1)


def _download_plugin_zip(url: str, zip_path: Path) -> None:
    """Download a plugin zip while validating each redirect target."""
    import httpx

    current_url = url
    for redirect_count in range(_MAX_PLUGIN_ZIP_REDIRECTS + 1):
        _validate_plugin_download_url(current_url)
        with httpx.stream("GET", current_url, follow_redirects=False, timeout=60.0) as resp:
            if 300 <= resp.status_code < 400 and resp.headers.get("location"):
                if redirect_count >= _MAX_PLUGIN_ZIP_REDIRECTS:
                    typer.echo("Error: plugin download exceeded redirect limit", err=True)
                    raise typer.Exit(1)
                current_url = urljoin(str(resp.url), resp.headers["location"])
                continue
            resp.raise_for_status()
            total_size = 0
            with zip_path.open("wb") as f:
                for chunk in resp.iter_bytes():
                    total_size += len(chunk)
                    if total_size > _MAX_PLUGIN_ZIP_SIZE:
                        typer.echo(
                            f"Error: plugin download exceeded size limit "
                            f"({_MAX_PLUGIN_ZIP_SIZE} bytes)",
                            err=True,
                        )
                        raise typer.Exit(1)
                    f.write(chunk)
            return


def _resolve_source(target: str) -> tuple[Path, Path | None]:
    """Resolve plugin source to (local_dir, tmp_to_cleanup).

    Returns the source directory and an optional temp directory that
    the caller must clean up after use.
    """
    import shutil
    import tempfile

    # HTTP(S) URL pointing to a .zip — download then extract.
    # Checked before the git-URL branch so GitHub/GitLab archive links
    # like .../archive/refs/heads/main.zip take this path.
    parsed = urlparse(target)
    if parsed.scheme in ("http", "https") and parsed.path.lower().endswith(".zip"):
        import httpx

        tmp = Path(tempfile.mkdtemp(prefix="pythinker-plugin-"))
        zip_path = tmp / "_download.zip"
        typer.echo(f"Downloading {target}...")
        try:
            _download_plugin_zip(target, zip_path)
        except httpx.HTTPError as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            typer.echo(f"Error: download failed: {exc}", err=True)
            raise typer.Exit(1) from exc
        except typer.Exit:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

        return _extract_zip_to_plugin(zip_path, tmp)

    # Git URL
    if target.startswith(("https://", "git@", "http://")) and (
        ".git/" in target
        or target.endswith(".git")
        or "github.com/" in target
        or "gitlab.com/" in target
    ):
        import subprocess

        clone_url, subpath, branch = _parse_git_url(target)

        tmp = Path(tempfile.mkdtemp(prefix="pythinker-plugin-"))
        typer.echo(f"Cloning {clone_url}...")
        clone_cmd = ["git", "clone", "--depth", "1"]
        if branch:
            clone_cmd += ["--branch", branch]
        clone_cmd += [clone_url, str(tmp / "repo")]
        result = subprocess.run(
            clone_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            typer.echo(
                f"Error: git clone failed: {result.stderr.strip()}",
                err=True,
            )
            raise typer.Exit(1)

        repo_root = tmp / "repo"

        if subpath:
            source = (repo_root / subpath).resolve()
            if not source.is_relative_to(repo_root.resolve()):
                shutil.rmtree(tmp, ignore_errors=True)
                typer.echo(
                    f"Error: subpath escapes repository: {subpath}",
                    err=True,
                )
                raise typer.Exit(1)
            if not source.is_dir():
                shutil.rmtree(tmp, ignore_errors=True)
                typer.echo(
                    f"Error: subpath '{subpath}' not found in repository",
                    err=True,
                )
                raise typer.Exit(1)
            if not (source / "plugin.json").exists():
                shutil.rmtree(tmp, ignore_errors=True)
                typer.echo(
                    f"Error: no plugin.json in '{subpath}'",
                    err=True,
                )
                raise typer.Exit(1)
            return source, tmp

        # No subpath — check root first
        if (repo_root / "plugin.json").exists():
            return repo_root, tmp

        # Scan one level for available plugins
        available = sorted(
            d.name for d in repo_root.iterdir() if d.is_dir() and (d / "plugin.json").exists()
        )
        if available:
            names = "\n".join(f"  - {n}" for n in available)
            typer.echo(
                f"Error: No plugin.json at repository root. "
                f"Available plugins:\n{names}\n"
                f"Use: pythinker plugin install <url>/<plugin-name>",
                err=True,
            )
        else:
            typer.echo(
                "Error: No plugin.json found in repository",
                err=True,
            )
        shutil.rmtree(tmp, ignore_errors=True)
        raise typer.Exit(1)

    p = Path(target).expanduser().resolve()

    # Zip file
    if p.is_file() and p.suffix == ".zip":
        tmp = Path(tempfile.mkdtemp(prefix="pythinker-plugin-"))
        typer.echo(f"Extracting {p.name}...")
        return _extract_zip_to_plugin(p, tmp)

    # Local directory
    if p.is_dir():
        return p, None

    typer.echo(
        f"Error: {target} is not a directory, zip file, zip URL, or git URL",
        err=True,
    )
    raise typer.Exit(1)


@cli.command("install")
def install_cmd(
    target: Annotated[
        str,
        typer.Argument(help="Plugin source: directory, .zip file, .zip URL, or git URL"),
    ],
) -> None:
    """Install a plugin and inject host configuration."""
    import shutil

    from pythinker_code.config import load_config
    from pythinker_code.constant import VERSION
    from pythinker_code.plugin.manager import get_plugins_dir, install_plugin

    source, tmp_dir = _resolve_source(target)

    try:
        config = load_config()

        from pythinker_code.auth.oauth import OAuthManager
        from pythinker_code.llm import augment_provider_with_env_vars
        from pythinker_code.plugin.manager import collect_host_values

        # Apply env var overrides (install runs outside normal startup)
        if config.default_model and config.default_model in config.models:
            model = config.models[config.default_model]
            if model.provider in config.providers:
                augment_provider_with_env_vars(
                    config.providers[model.provider], model, provider_key=model.provider
                )

        oauth = OAuthManager(config)
        host_values = collect_host_values(config, oauth)

        if not host_values.get("api_key"):
            typer.echo(
                "Warning: No LLM provider configured. "
                "Plugins requiring API key injection will fail. "
                "Run 'pythinker login' or configure a provider first.",
                err=True,
            )

        spec = install_plugin(
            source=source,
            plugins_dir=get_plugins_dir(),
            host_values=host_values,
            host_name="pythinker-code",
            host_version=VERSION,
        )
    except PluginError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        # Clean up temp directory from zip/git extraction
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    typer.echo(f"Installed plugin '{spec.name}' v{spec.version}")
    if spec.runtime:
        typer.echo(f"  runtime: host={spec.runtime.host}, version={spec.runtime.host_version}")


@cli.command("list")
def list_cmd() -> None:
    """List installed plugins."""
    from pythinker_code.plugin.manager import get_plugins_dir, list_plugins

    plugins = list_plugins(get_plugins_dir())
    if not plugins:
        typer.echo("No plugins installed.")
        return

    for p in plugins:
        status = "installed" if p.runtime else "not configured"
        typer.echo(f"  {p.name} v{p.version} ({status})")


@cli.command("remove")
def remove_cmd(
    name: Annotated[str, typer.Argument(help="Plugin name to remove")],
) -> None:
    """Remove an installed plugin."""
    from pythinker_code.plugin.manager import get_plugins_dir, remove_plugin

    try:
        remove_plugin(name, get_plugins_dir())
    except PluginError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Removed plugin '{name}'")


@cli.command("info")
def info_cmd(
    name: Annotated[str, typer.Argument(help="Plugin name")],
) -> None:
    """Show plugin details."""
    from pythinker_code.plugin import parse_plugin_json
    from pythinker_code.plugin.manager import get_plugins_dir

    plugin_json = get_plugins_dir() / name / "plugin.json"
    if not plugin_json.exists():
        typer.echo(f"Error: Plugin '{name}' not found", err=True)
        raise typer.Exit(1)

    try:
        spec = parse_plugin_json(plugin_json)
    except PluginError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Name:        {spec.name}")
    typer.echo(f"Version:     {spec.version}")
    typer.echo(f"Description: {spec.description or '(none)'}")
    typer.echo(f"Config file: {spec.config_file or '(none)'}")
    if spec.inject:
        typer.echo(f"Inject:      {', '.join(f'{k} <- {v}' for k, v in spec.inject.items())}")
    if spec.runtime:
        typer.echo(f"Runtime:     host={spec.runtime.host}, version={spec.runtime.host_version}")
    else:
        typer.echo("Runtime:     (not installed via host)")


def _config_for_toggle() -> Config:
    """Load config for enable/disable, requiring the default config location.

    enable/disable persist to the user ``config.toml``; refuse when the session
    runs from an explicit ``--config``/``--config-file`` so we never rewrite a
    file the user pointed us at ad hoc (mirrors the auth-login guard).
    """
    from pythinker_code.config import load_config

    config = load_config()
    if not config.is_from_default_location:
        typer.echo(
            "Error: enable/disable requires the default config file; "
            "restart without --config/--config-file.",
            err=True,
        )
        raise typer.Exit(1)
    return config


@cli.command("disable")
def disable_cmd(
    name: Annotated[str, typer.Argument(help="Plugin name to disable")],
) -> None:
    """Turn off a plugin (native or auto-detected) without uninstalling it."""
    from pythinker_code.config import save_config

    config = _config_for_toggle()
    if name in config.plugins.disabled:
        typer.echo(f"Plugin '{name}' is already disabled.")
        return
    config.plugins.disabled.append(name)
    save_config(config)
    typer.echo(f"Disabled plugin '{name}'.")


@cli.command("enable")
def enable_cmd(
    name: Annotated[str, typer.Argument(help="Plugin name to enable")],
) -> None:
    """Re-enable a previously disabled plugin."""
    from pythinker_code.config import save_config

    config = _config_for_toggle()
    changed = False
    if name in config.plugins.disabled:
        config.plugins.disabled.remove(name)
        changed = True
    # If a non-empty allowlist is in force, ensure the plugin is part of it.
    if config.plugins.enabled and name not in config.plugins.enabled:
        config.plugins.enabled.append(name)
        changed = True
    if not changed:
        typer.echo(f"Plugin '{name}' is already enabled.")
        return
    save_config(config)
    typer.echo(f"Enabled plugin '{name}'.")


def _default_marketplace_name(source: Any) -> str:
    """Derive a marketplace name from its source when none is given."""
    if source.source == "github" and source.repo:
        return source.repo.rstrip("/").split("/")[-1]
    if source.url:
        tail = source.url.rstrip("/").split("/")[-1]
        return tail[:-4] if tail.endswith(".git") else tail
    if source.path:
        path = Path(source.path)
        return path.stem if source.source == "file" else path.name
    return "marketplace"


@marketplace_cli.command("add")
def marketplace_add_cmd(
    source: Annotated[str, typer.Argument(help="github owner/repo, git/URL, or local path")],
    name: Annotated[str | None, typer.Option("--name", help="Marketplace name")] = None,
) -> None:
    """Register a plugin marketplace."""
    from pythinker_code.plugin.marketplace import (
        MarketplaceError,
        add_marketplace,
        parse_marketplace_input,
    )

    try:
        parsed = parse_marketplace_input(source)
        resolved_name = name or _default_marketplace_name(parsed)
        add_marketplace(resolved_name, parsed)
    except MarketplaceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Added marketplace '{resolved_name}' ({parsed.source})")


@marketplace_cli.command("remove")
def marketplace_remove_cmd(
    name: Annotated[str, typer.Argument(help="Marketplace name")],
) -> None:
    """Unregister a marketplace."""
    from pythinker_code.plugin.marketplace import remove_marketplace

    if remove_marketplace(name):
        typer.echo(f"Removed marketplace '{name}'")
    else:
        typer.echo(f"Marketplace '{name}' not found", err=True)
        raise typer.Exit(1)


@marketplace_cli.command("list")
def marketplace_list_cmd() -> None:
    """List configured marketplaces."""
    from pythinker_code.plugin.marketplace import load_known_marketplaces

    marketplaces = load_known_marketplaces()
    if not marketplaces:
        typer.echo("No marketplaces configured.")
        return
    for name, entry in sorted(marketplaces.items()):
        src = entry.source
        where = src.repo or src.url or src.path or src.source
        typer.echo(f"  {name}  ({src.source}: {where})")


@marketplace_cli.command("refresh")
def marketplace_refresh_cmd(
    name: Annotated[str, typer.Argument(help="Marketplace name to refresh")],
) -> None:
    """Re-resolve a marketplace's catalog (re-clones git sources)."""
    from pythinker_code.plugin.install import refresh_marketplace
    from pythinker_code.plugin.marketplace import MarketplaceError

    try:
        count = refresh_marketplace(name)
    except MarketplaceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Refreshed '{name}' — {count} plugin(s) available")


@marketplace_cli.command("install")
def marketplace_install_cmd(
    plugin: Annotated[str, typer.Argument(help="Plugin name, or name@marketplace")],
    marketplace: Annotated[
        str | None, typer.Argument(help="Marketplace name (omit if using name@marketplace)")
    ] = None,
) -> None:
    """Install a plugin from a configured marketplace."""
    from pythinker_code.plugin.install import install_plugin_from_marketplace
    from pythinker_code.plugin.marketplace import MarketplaceError

    if marketplace is None:
        if "@" not in plugin:
            typer.echo("Error: specify <plugin> <marketplace> or name@marketplace", err=True)
            raise typer.Exit(1)
        plugin, marketplace = plugin.rsplit("@", 1)
    try:
        record = install_plugin_from_marketplace(plugin, marketplace)
    except MarketplaceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Installed '{plugin}@{marketplace}' v{record.version} -> {record.install_path}")


@marketplace_cli.command("uninstall")
def marketplace_uninstall_cmd(
    plugin: Annotated[str, typer.Argument(help="Plugin name, or name@marketplace")],
    marketplace: Annotated[str | None, typer.Argument(help="Marketplace name")] = None,
) -> None:
    """Uninstall a marketplace plugin (removes records and cached/symlinked files)."""
    import shutil

    from pythinker_code.plugin.directories import plugin_cache_dir
    from pythinker_code.plugin.installed import load_installed_plugins, remove_install

    if marketplace is None:
        if "@" not in plugin:
            typer.echo("Error: specify <plugin> <marketplace> or name@marketplace", err=True)
            raise typer.Exit(1)
        plugin, marketplace = plugin.rsplit("@", 1)

    records = load_installed_plugins().get(f"{plugin}@{marketplace}", [])
    if not remove_install(plugin, marketplace):
        typer.echo(f"'{plugin}@{marketplace}' is not installed", err=True)
        raise typer.Exit(1)
    # Remove the on-disk install (unlink symlinks; rmtree real dirs). Constrain
    # deletions to the plugin cache root so corrupted metadata (an install_path
    # pointing elsewhere) cannot remove arbitrary user files. Symlinks are only
    # unlinked, never followed, so an external reuse target is left untouched.
    cache_root = plugin_cache_dir().resolve()
    for record in records:
        path = Path(record.install_path)
        parent = path.parent.resolve()
        if parent != cache_root and cache_root not in parent.parents:
            typer.echo(f"Warning: skipping unsafe uninstall path outside cache: {path}", err=True)
            continue
        if path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    plugin_dir = plugin_cache_dir() / marketplace / plugin
    if plugin_dir.is_dir() and not any(plugin_dir.iterdir()):
        plugin_dir.rmdir()
    typer.echo(f"Uninstalled '{plugin}@{marketplace}'")


@marketplace_cli.command("installed")
def marketplace_installed_cmd() -> None:
    """List installed marketplace plugins."""
    from pythinker_code.plugin.installed import load_installed_plugins

    plugins = load_installed_plugins()
    if not plugins:
        typer.echo("No marketplace plugins installed.")
        return
    for ident, records in sorted(plugins.items()):
        versions = ", ".join(sorted({r.version for r in records}))
        typer.echo(f"  {ident}  (v{versions})")


cli.add_typer(marketplace_cli, name="marketplace")
