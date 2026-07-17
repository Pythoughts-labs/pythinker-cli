"""Pure auto-update policy resolution.

Kept dependency-light on purpose: it imports nothing from the interactive shell
stack (no ``aiohttp``, no console, no share-directory initialization). That lets
lightweight entry points such as ``pythinker info`` report auto-update status
without importing ``pythinker_code.ui.shell.update`` — which would pull in heavy
dependencies and create the share directory as an import side effect.

``pythinker_code.ui.shell.update`` re-exports these so existing call sites and
tests keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pythinker_code.config import AutoUpdateMode, Config


def auto_update_disabled() -> bool:
    """True when the hard kill-switch env var disables auto-update entirely."""
    from pythinker_code.utils.envvar import get_env_bool

    return get_env_bool("PYTHINKER_CLI_NO_AUTO_UPDATE")


def is_running_from_source_checkout() -> bool:
    """Return true when invoked from this repository via ``uv run``/editable source.

    In that mode PyPI can legitimately have a newer released version than the
    checkout's local ``pyproject.toml`` version. Showing the normal upgrade
    banner is noisy and suggests replacing the developer checkout.
    """
    try:
        import pythinker_code

        package_path = Path(pythinker_code.__file__).resolve()
    except (ImportError, AttributeError, OSError):
        return False

    for parent in package_path.parents:
        pyproject = parent / "pyproject.toml"
        git_dir = parent / ".git"
        if pyproject.exists() and git_dir.exists():
            try:
                text = pyproject.read_text(encoding="utf-8")
            except OSError:
                return False
            return 'name = "pythinker-code"' in text or "name = 'pythinker-code'" in text
    return False


def resolve_auto_update_mode(config: Config) -> AutoUpdateMode:
    """Effective startup auto-update policy after external overrides.

    Precedence (highest first):
    1. ``PYTHINKER_CLI_NO_AUTO_UPDATE`` (the hard kill-switch) → ``OFF``.
    2. Source checkout → ``OFF``.
    3. Otherwise → ``config.auto_update``.

    Managed channels (Docker/Nix/Scoop/WinGet) are *not* special-cased here:
    they may resolve to a download mode, but ``_do_update`` returns
    ``UPDATE_AVAILABLE`` and emits a channel hint instead of touching the
    binary, so they never get a background install regardless of this result.
    """
    from pythinker_code.config import AutoUpdateMode

    if auto_update_disabled() or is_running_from_source_checkout():
        return AutoUpdateMode.OFF
    return config.auto_update


def auto_update_enabled(config: Config) -> bool:
    """Whether startup may download-and-stage a newer release in the background.

    True for the ``download`` and ``apply_on_exit`` modes when no external
    override forces the policy off. No mode installs mid-session; "enabled"
    means staged-for-restart, not live replacement.
    """
    from pythinker_code.config import AutoUpdateMode

    return resolve_auto_update_mode(config) in (
        AutoUpdateMode.DOWNLOAD,
        AutoUpdateMode.APPLY_ON_EXIT,
    )


def auto_update_override_reason() -> str | None:
    """Reason auto-update is force-disabled regardless of ``config.auto_update``.

    These overrides sit *above* the config field in :func:`auto_update_enabled`'s
    precedence, so toggling the setting cannot change the effective behavior while
    one is in effect. Returns ``None`` when the config field is the deciding
    factor (the normal case) — callers use that to decide whether a settings
    toggle is live or merely cosmetic.
    """
    if auto_update_disabled():
        return "disabled by PYTHINKER_CLI_NO_AUTO_UPDATE"
    if is_running_from_source_checkout():
        return "disabled for source checkouts"
    return None
