"""Native-build detection + GitHub Releases lookup helpers.

Native packages drop a sentinel file ``.pythinker-native`` next to the launched
executable. Older curl-bash onefile installs did not include that sentinel, so
runtime detection also treats a frozen ``pythinker`` executable as native.
Native installs update from GitHub Release assets instead of shelling out to
``pip`` or ``uv`` from inside the CLI.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

GITHUB_REPO = "Pythoughts-labs/pythinker-cli"
SENTINEL_FILENAME = ".pythinker-native"


def is_native_build() -> bool:
    """True iff this process is a Pythinker native install."""
    try:
        exe_path = Path(sys.executable).resolve()
    except OSError:
        return False

    # The installers deliberately drop this marker next to the launched
    # executable. Trust it even if a platform-specific launcher/wrapper does
    # not expose PyInstaller's ``sys.frozen`` flag; this keeps native installs
    # on the native update path instead of falling back to uv/pip.
    if (exe_path.parent / SENTINEL_FILENAME).is_file():
        return True

    if not getattr(sys, "frozen", False):
        return False
    return exe_path.stem.lower() == "pythinker"


def native_installer_release_url(channel: str = "latest") -> str:
    """Return the GitHub API URL for the requested release channel."""
    if channel == "latest":
        return f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    return f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{channel}"


def native_installer_asset_name(version: str) -> str:
    """Filename of the Windows installer asset attached to a Release."""
    return f"PythinkerSetup-{version}.exe"


def native_archive_asset_name(version: str) -> str | None:
    """Filename of the onefile native archive for this platform, if published."""
    target = _native_archive_target()
    if target is None:
        return None
    return f"pythinker-{version}-{target}.tar.gz"


def _native_archive_target() -> str | None:
    system = sys.platform
    machine = platform.machine().lower()
    if system.startswith("linux"):
        if machine in {"x86_64", "amd64"}:
            return "x86_64-unknown-linux-gnu"
        if machine in {"aarch64", "arm64"}:
            return "aarch64-unknown-linux-gnu"
    if system == "darwin":
        if machine in {"aarch64", "arm64"}:
            return "aarch64-apple-darwin"
        if machine in {"x86_64", "amd64"}:
            return "x86_64-apple-darwin"
    return None
