"""On-disk locations for the plugin/marketplace system.

pythinker owns ``~/.pythinker/plugins/``; for cross-ecosystem compatibility the
loader also reads (read-only) the Claude Code and Codex plugin roots, so plugins
already installed for those tools (e.g. a Claude ``ponytail`` plugin) contribute
their artifacts without re-installation.
"""

from __future__ import annotations

from pathlib import Path

from pythinker_code.plugin.manager import get_plugins_dir

# --- pythinker-owned state -------------------------------------------------


def known_marketplaces_file() -> Path:
    """Registry of configured marketplaces."""
    return get_plugins_dir() / "known_marketplaces.json"


def installed_plugins_file() -> Path:
    """Installation metadata for plugins installed from marketplaces."""
    return get_plugins_dir() / "installed_plugins.json"


def marketplaces_cache_dir() -> Path:
    """Cached marketplace manifests (``<name>.json`` or cloned ``<name>/``)."""
    return get_plugins_dir() / "marketplaces"


def plugin_cache_dir() -> Path:
    """Installed plugin contents, keyed ``<marketplace>/<plugin>/<version>/``."""
    return get_plugins_dir() / "cache"


def plugin_data_dir(plugin_name: str) -> Path:
    """Per-plugin persistent data directory (survives updates). Created lazily."""
    return get_plugins_dir() / "data" / plugin_name


# --- cross-ecosystem read-only roots ---------------------------------------


def claude_plugin_roots() -> list[Path]:
    """Claude Code plugin install roots (cache/local), if present."""
    base = Path.home() / ".claude" / "plugins"
    return [base / "cache", base / "local"]


def codex_plugin_roots() -> list[Path]:
    """Codex plugin install root, if present."""
    return [Path.home() / ".codex" / "plugins"]


def external_plugin_roots() -> list[Path]:
    """All read-only third-party plugin roots scanned for compatibility."""
    return claude_plugin_roots() + codex_plugin_roots()
