from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from shutil import which
from typing import cast

import aiohttp
import typer
from rich.text import Text

from pythinker_code.native import (
    is_native_build as _is_native_build,
)
from pythinker_code.native import (
    native_archive_asset_name,
    native_installer_asset_name,
    native_installer_release_url,
)
from pythinker_code.share import get_share_dir
from pythinker_code.ui.shell.console import console
from pythinker_code.ui.theme import get_tui_tokens as _get_tui_tokens

# Pure policy lives in a shell-free module (`update_policy`) so lightweight
# callers (e.g. `pythinker info`) can resolve auto-update status without
# importing this stack. These two primitives are used internally below; the
# public `auto_update_enabled` / `auto_update_override_reason` are imported
# directly from `update_policy` by their consumers.
from pythinker_code.update_policy import (
    auto_update_disabled as _auto_update_disabled,
)
from pythinker_code.update_policy import (
    is_running_from_source_checkout as _is_running_from_source_checkout,
)
from pythinker_code.utils.aiohttp import new_client_session
from pythinker_code.utils.logging import logger
from pythinker_code.utils.subprocess_env import get_clean_env

CHANGELOG_URL_EN = "https://github.com/Pythoughts-labs/pythinker-code/blob/main/CHANGELOG.md"
PYPI_VERSION_URL = "https://pypi.org/pypi/pythinker-code/{version}/json"
HOMEBREW_FORMULA_URL = (
    "https://raw.githubusercontent.com/Pythoughts-labs/homebrew-pythinker/"
    "main/Formula/pythinker-code.rb"
)

# Default upgrade command. `_detect_upgrade_command()` overrides this when the
# install method is recognizable from `sys.executable`.
UPGRADE_COMMAND = ["uv", "tool", "upgrade", "pythinker-code"]

LATEST_VERSION_FILE = get_share_dir() / "latest_version.txt"
LATEST_VERSION_ETAG_FILE = get_share_dir() / "latest_version.etag"
LAST_UPDATE_CHECK_FILE = get_share_dir() / "last_update_check.txt"
DISMISSED_VERSION_FILE = get_share_dir() / "dismissed_update_version.txt"
LAST_SEEN_VERSION_FILE = get_share_dir() / "last_seen_version.txt"
# ponytail: 30m throttle so a freshly-pushed release is picked up on the next
# restart instead of up to a day later. The check is unauthenticated, so each
# poll costs one of GitHub's 60 req/hr/IP budget regardless of the cached ETag
# (304s are only rate-limit-free when authenticated) — at ~2 polls/hr/startup
# that is a wide margin. A throttle miss fails safe: a transient error returns
# FAILED, which skips the mark and retries next launch.
AUTO_UPDATE_CHECK_INTERVAL_SECONDS = 30 * 60
# Backstop watchdog for a single periodic check attempt. The check's own
# per-socket timeouts already abort network stalls (and the streamed installer
# download is deliberately not capped by a total timeout so a slow link still
# completes — see `_maybe_run_native_update`), so this only prevents a
# non-network hang (stuck subprocess, trickle-forever stream) from killing the
# session-lifetime loop. Set to 2× the interval so a genuinely slow silent
# download still finishes before the watchdog fires; worst-case dead-loop
# recovery is one timeout plus one interval.
AUTO_UPDATE_CHECK_ATTEMPT_TIMEOUT_SECONDS = 2 * AUTO_UPDATE_CHECK_INTERVAL_SECONDS
PROMPT_UPDATE_REFRESH_TIMEOUT_SECONDS = 2.0
WINDOWS_UPDATE_STAGING_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
UPGRADE_COMMAND_TIMEOUT_SECONDS = 30 * 60

_UPDATE_LOCK = asyncio.Lock()
_skipped_version_this_session: str | None = None

NATIVE_INSTALLER_MARKER = "__pythinker_native_installer__"
MANAGED_CHANNEL_MARKER = "__pythinker_managed_channel__"


class UpdateIntent(Enum):
    """What an update invocation is allowed to do to the running process.

    ``CHECK`` only refreshes the latest-version cache. ``STAGE_FOR_RESTART`` is
    the only intent background tasks may use: it downloads and stages the new
    release but must never spawn an installer, run a package-manager upgrade,
    or exit the process. ``INSTALL`` is for foreground in-shell updates: inline
    package-manager upgrades are allowed, but Windows still stages for restart
    because replacing the running exe would kill the session. ``INSTALL_AND_EXIT``
    is reserved for the standalone ``pythinker update`` CLI (and the blocking
    pre-start prompt), where exiting to hand off to the installer is expected.
    """

    CHECK = auto()
    STAGE_FOR_RESTART = auto()
    INSTALL = auto()
    INSTALL_AND_EXIT = auto()


class UpdateResult(Enum):
    UPDATE_AVAILABLE = auto()
    UPDATED = auto()
    UP_TO_DATE = auto()
    FAILED = auto()
    VERIFICATION_FAILED = auto()
    UNSUPPORTED = auto()


class UpdatePromptSelection(Enum):
    UPDATE_NOW = auto()
    SKIP = auto()
    DISMISS_VERSION = auto()
    EXIT = auto()


type UpdateRunner = Callable[..., Awaitable[UpdateResult]]


def semver_tuple(version: str) -> tuple[int, int, int]:
    v = version.strip()
    if v.startswith("v"):
        v = v[1:]
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", v)
    if not match:
        return (0, 0, 0)
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3) or 0)
    return (major, minor, patch)


def _detect_upgrade_command() -> list[str]:
    """Pick the right upgrade argv based on how this interpreter was installed."""
    # Channel-managed installs (Docker/Nix/Scoop/WinGet) export PYTHINKER_MANAGED
    # so the updater emits a channel-native hint instead of shelling pip/uv.
    # Brew deliberately does NOT set it — its cellar path-sniff below is the
    # load-bearing, behavior-unchanged path.
    managed = os.environ.get("PYTHINKER_MANAGED")
    if managed:
        return [MANAGED_CHANNEL_MARKER, managed]
    exe = sys.executable.replace("\\", "/").lower()
    if "/cellar/pythinker-code/" in exe or "/homebrew/cellar/pythinker-code/" in exe:
        return ["brew", "upgrade", "pythinker-code"]
    if _is_native_build() or (_is_windows() and not _is_running_from_source_checkout()):
        return [NATIVE_INSTALLER_MARKER]
    if "/uv/tools/" in exe:
        return ["uv", "tool", "upgrade", "pythinker-code"]
    if "/pipx/venvs/" in exe:
        return ["pipx", "upgrade", "pythinker-code"]
    return [sys.executable, "-m", "pip", "install", "--upgrade", "pythinker-code"]


def _format_upgrade_command(command: list[str]) -> str:
    if _is_windows():
        return subprocess.list2cmdline(command)
    return " ".join(shlex_quote(part) for part in command)


def shlex_quote(value: str) -> str:
    if not value:
        return "''"
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _version_from_release_payload(data: object) -> str | None:
    if not isinstance(data, Mapping):
        return None
    payload = cast(Mapping[str, object], data)
    tag_name = payload.get("tag_name")
    release_name = payload.get("name")
    raw = tag_name if isinstance(tag_name, str) else release_name
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if raw.startswith("v"):
        raw = raw[1:]
    return raw if re.fullmatch(r"\d+\.\d+\.\d+", raw) else None


def _read_cached_etag() -> str | None:
    try:
        return LATEST_VERSION_ETAG_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_cached_etag(etag: str) -> None:
    try:
        LATEST_VERSION_ETAG_FILE.write_text(etag, encoding="utf-8")
    except OSError:
        logger.exception("Failed to cache release ETag:")


def _clear_cached_etag() -> None:
    with contextlib.suppress(OSError):
        LATEST_VERSION_ETAG_FILE.unlink(missing_ok=True)


async def _get_latest_version(session: aiohttp.ClientSession) -> str | None:
    """Return the latest native release version from GitHub Releases.

    Uses HTTP conditional requests (``If-None-Match`` + cached ETag) per
    GitHub's documented best practice for polling release/events endpoints:
    https://docs.github.com/en/rest/guides/best-practices-for-using-the-rest-api
    A 304 response skips re-transferring the ~50KB release payload, and the
    body is served straight from our local version cache.
    """
    headers = {"Accept": "application/vnd.github+json"}
    cached_etag = _read_cached_etag()
    if cached_etag:
        # GitHub ETags arrive quoted (e.g. `W/"a18c3bd..."`). Store and send
        # them verbatim — the surrounding quotes are part of the value.
        headers["If-None-Match"] = cached_etag
    try:
        async with session.get(native_installer_release_url(), headers=headers) as resp:
            if resp.status == 304:
                cached_version = _read_latest_version_cache()
                if cached_version:
                    return cached_version
                # State drift: etag cached but no version cache. Drop the
                # stale etag so the next call re-fetches fresh.
                _clear_cached_etag()
                return None
            resp.raise_for_status()
            new_etag = resp.headers.get("ETag")
            data = await resp.json(content_type=None)
            version = _version_from_release_payload(data)
            if version and new_etag:
                _write_cached_etag(new_etag)
            return version.strip() if version else None
    except (TimeoutError, aiohttp.ClientError):
        logger.exception("Failed to fetch latest version from GitHub Releases:")
        return None
    except Exception:
        logger.exception("Failed to parse GitHub release response:")
        return None


def format_managed_channel_notice(
    current: str,
    latest: str,
    *,
    upgrade_command: list[str] | None = None,
) -> str | None:
    """One-line channel-native upgrade hint for managed installs, or None."""
    command = upgrade_command if upgrade_command is not None else _detect_upgrade_command()
    if command[:1] != [MANAGED_CHANNEL_MARKER] or len(command) < 2:
        return None
    channel = command[1]
    return (
        f"Pythinker is managed by your {channel} channel. "
        f"Update {current} → {latest} via {channel} "
        "(rebuild/repull the image or run the channel's upgrade command)."
    )


def _should_auto_check_for_updates(now: float | None = None) -> bool:
    if _auto_update_disabled() or _is_running_from_source_checkout():
        return False
    # No isatty() guard here: this runs inside the interactive shell's event
    # loop where prompt_toolkit may have replaced sys.stdout with a non-TTY
    # wrapper, falsely suppressing the check. The shell is always interactive
    # by construction; non-interactive callers never start the shell.

    now = time.time() if now is None else now
    try:
        last_check = LAST_UPDATE_CHECK_FILE.stat().st_mtime
    except FileNotFoundError:
        return True
    except OSError:
        logger.exception("Failed to read last update-check timestamp:")
        return True
    return now - last_check >= AUTO_UPDATE_CHECK_INTERVAL_SECONDS


def _mark_auto_update_check_attempt() -> None:
    try:
        LAST_UPDATE_CHECK_FILE.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        logger.exception("Failed to write last update-check timestamp:")


async def prompt_pre_start_update(update_runner: UpdateRunner | None = None) -> None:
    """Blocking update prompt for the interactive shell.

    Runs once at startup, before the agent loop. When a newer native release
    exists, asks the user whether to update now. Accepting runs the native
    updater and exits so the user relaunches the new version; declining
    continues the current session.
    """
    from pythinker_code.constant import VERSION as current_version

    _cleanup_stale_windows_update_staging()
    if _auto_update_disabled() or _is_running_from_source_checkout():
        return
    if not sys.stdout.isatty():
        return

    latest_version = await _resolve_latest_version_for_prompt()
    if not latest_version:
        return
    if semver_tuple(latest_version) <= semver_tuple(current_version):
        return
    if _read_dismissed_version() == latest_version:
        return

    selection = await _prompt_update_selection(current_version, latest_version, allow_exit=True)
    if selection is UpdatePromptSelection.EXIT:
        raise typer.Exit(0)
    if selection is UpdatePromptSelection.DISMISS_VERSION:
        _dismiss_version(latest_version)
        return
    if selection is not UpdatePromptSelection.UPDATE_NOW:
        _skip_version_this_session(latest_version)
        return

    if update_runner is None:
        result = await do_update(print_output=True, intent=UpdateIntent.INSTALL_AND_EXIT)
    else:
        result = await update_runner(print_output=True, intent=UpdateIntent.INSTALL_AND_EXIT)
    if result is UpdateResult.UPDATED:
        # do_update() already printed "Updated successfully!" + the relaunch
        # hint. Wait for the user to acknowledge before exiting so the message
        # stays on screen instead of the process vanishing (which reads as a
        # crash) right after they chose "Update now".
        await _await_exit_acknowledgment()
        raise typer.Exit(0)


async def _await_exit_acknowledgment() -> None:
    """Block on a keypress so the update/relaunch message is readable before exit.

    Making the close user-initiated is the point: a fixed sleep would still
    close on its own and read as a crash. Runs ``input`` off the event loop;
    EOF/Ctrl-C just proceed to exit.
    """
    _t = _get_tui_tokens()
    console.print(
        f"\n[{_t.muted}]Press Enter to close Pythinker, then relaunch to use the new version.[/]"
    )
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, input)
    except (EOFError, KeyboardInterrupt):
        return


def _read_latest_version_cache() -> str | None:
    try:
        return LATEST_VERSION_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _clear_latest_version_cache() -> None:
    with contextlib.suppress(OSError):
        LATEST_VERSION_FILE.unlink(missing_ok=True)


def _read_dismissed_version() -> str | None:
    try:
        return DISMISSED_VERSION_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _dismiss_version(version: str) -> None:
    try:
        DISMISSED_VERSION_FILE.write_text(version, encoding="utf-8")
    except OSError:
        logger.exception("Failed to write dismissed update version:")


def _skip_version_this_session(version: str) -> None:
    global _skipped_version_this_session
    _skipped_version_this_session = version


def _read_last_seen_version() -> str | None:
    try:
        return LAST_SEEN_VERSION_FILE.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("Failed to read last-seen version:")
        return None


def _write_last_seen_version(version: str) -> None:
    try:
        LAST_SEEN_VERSION_FILE.write_text(version, encoding="utf-8")
    except OSError:
        logger.exception("Failed to write last-seen version:")


def _write_last_seen_version_if_absent(version: str) -> bool:
    """Create the last-seen marker only if another process has not done so."""
    try:
        with LAST_SEEN_VERSION_FILE.open("x", encoding="utf-8") as f:
            f.write(version)
        return True
    except FileExistsError:
        return False
    except OSError:
        logger.exception("Failed to create last-seen version:")
        return False


def _cached_update_available() -> str | None:
    """Return a newer cached release version after shared non-session filters.

    This intentionally ignores the per-session skip flag; callers that surface
    transient toasts apply that suppression separately, while the welcome banner
    uses this result as a session-persistent reminder.
    """
    from pythinker_code.constant import VERSION as current_version

    if _auto_update_disabled() or _is_running_from_source_checkout():
        return None
    cached = _read_latest_version_cache()
    if not cached:
        return None
    if semver_tuple(cached) <= semver_tuple(current_version):
        return None
    if _read_dismissed_version() == cached:
        return None
    return cached


def welcome_update_target() -> str | None:
    """Cached newer release version for the welcome-banner chip, or None.

    Unlike ``pending_update_notice`` this does not suppress when the user
    chose 'Skip this session' on the startup modal — the banner chip is the
    session-persistent reminder of that skip.
    """
    return _cached_update_available()


def consume_whats_new() -> str | None:
    """Return the current version string on first launch after an upgrade, else None.

    Side-effect: records the current version as 'last seen' so subsequent
    launches in the same installation return None.  No disk write in steady
    state (last_seen == current).  First-ever launch writes the baseline and
    returns None so existing installs upgrading onto this feature see nothing
    until the *next* upgrade.
    """
    if _is_running_from_source_checkout():
        return None

    from pythinker_code.constant import VERSION as current_version

    last_seen = _read_last_seen_version()
    if last_seen is None:
        # First launch — establish baseline, show nothing. Use exclusive create
        # so concurrent first launches do not both truncate/write the marker.
        if not _write_last_seen_version_if_absent(current_version):
            last_seen = _read_last_seen_version()
            if last_seen is None:
                # Repair an empty/corrupt marker left by a crashed concurrent writer.
                _write_last_seen_version(current_version)
        return None
    if last_seen == current_version:
        return None
    # Upgraded since last launch.
    _write_last_seen_version(current_version)
    return current_version


async def refresh_update_cache_if_due() -> UpdateResult | None:
    """Refresh the cached latest native release when the startup throttle allows it."""
    return await _refresh_update_cache(force=False)


async def _refresh_update_cache(*, force: bool) -> UpdateResult | None:
    if not force and not _should_auto_check_for_updates():
        return None
    try:
        result = await do_update(print_output=False, intent=UpdateIntent.CHECK)
    except Exception:
        logger.exception("Update cache refresh failed:")
        return None
    # Only throttle after a successful round-trip. Marking before the network
    # call would silently swallow update notices for 24h when the first shell
    # start happens to hit a transient network issue — the user would never
    # see the update banner until the throttle expires.
    if result is not UpdateResult.FAILED:
        _mark_auto_update_check_attempt()
    return result


async def _refresh_update_cache_for_prompt(*, force: bool) -> UpdateResult | None:
    """Refresh the startup-prompt version cache without hanging shell startup."""
    try:
        return await asyncio.wait_for(
            _refresh_update_cache(force=force),
            timeout=PROMPT_UPDATE_REFRESH_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning("Update prompt refresh timed out; using cached latest version")
        return None


async def _resolve_latest_version_for_prompt(*, force_refresh: bool = False) -> str | None:
    """Return the latest known native release for the pre-start prompt.

    The background notifier uses a 24h throttle, but the blocking startup prompt
    must not trust a cached "already current" answer forever: a release can land
    minutes after the last successful check. Revalidate missing/stale caches with
    a short timeout and GitHub's cached ETag; keep the throttle only when the
    cached version is already newer than the running version.
    """
    from pythinker_code.constant import VERSION as current_version

    cached = _read_latest_version_cache()
    cached_is_stale = cached is None or semver_tuple(cached) <= semver_tuple(current_version)
    refresh_result = await _refresh_update_cache_for_prompt(force=force_refresh or cached_is_stale)
    if refresh_result is not None:
        return _read_latest_version_cache() or cached
    return cached


def pending_update_notice() -> str | None:
    """Non-blocking cached update notice text, or None.

    Reads only the cached latest version (no network). This is used by the
    background refresher after the pre-start prompt has had first chance to
    interrupt the session. Suppressed for source checkouts, disabled
    auto-update, per-version dismissals, and session-level skips.
    """
    from pythinker_code.constant import VERSION as current_version

    cached = _cached_update_available()
    if not cached:
        return None
    if cached == _skipped_version_this_session:
        return None
    return f"Update available: {current_version} → {cached}. Run /update to install."


async def run_update_prompt(update_runner: UpdateRunner | None = None) -> UpdateResult | None:
    """Interactive ``/update`` flow: refresh, show the 3-choice modal, install.

    In-shell safe — unlike ``prompt_pre_start_update`` it does not block on raw
    ``input`` or raise ``typer.Exit``; it returns the result so the caller can
    message the user. On Windows the native installer is staged for restart
    (never launched mid-session); the next launch applies it before the
    session starts.
    """
    from pythinker_code.constant import VERSION as current_version

    if update_runner is None:
        refresh_result = await do_update(print_output=True, intent=UpdateIntent.CHECK)
    else:
        refresh_result = await update_runner(print_output=True, intent=UpdateIntent.CHECK)
    if refresh_result is UpdateResult.UP_TO_DATE:
        return UpdateResult.UP_TO_DATE
    if refresh_result is UpdateResult.FAILED:
        return UpdateResult.FAILED

    latest_version = _read_latest_version_cache()
    if not latest_version:
        console.print(f"[{_get_tui_tokens().error}]Failed to check for updates.[/]")
        return UpdateResult.FAILED
    if semver_tuple(latest_version) <= semver_tuple(current_version):
        console.print(f"[{_get_tui_tokens().success}]Already up to date.[/]")
        return UpdateResult.UP_TO_DATE

    selection = await _prompt_update_selection(current_version, latest_version)
    if selection is UpdatePromptSelection.DISMISS_VERSION:
        _dismiss_version(latest_version)
        return None
    if selection is not UpdatePromptSelection.UPDATE_NOW:
        _skip_version_this_session(latest_version)
        return None
    if update_runner is None:
        return await do_update(print_output=True, intent=UpdateIntent.INSTALL)
    return await update_runner(print_output=True, intent=UpdateIntent.INSTALL)


async def _prompt_update_selection(
    current_version: str, latest_version: str, *, allow_exit: bool = False
) -> UpdatePromptSelection:
    from prompt_toolkit.shortcuts.choice_input import ChoiceInput

    console.print(_update_prompt_text(current_version, latest_version))
    options = [
        ("update", "Update now"),
        ("skip", "Skip this session"),
        ("dismiss", "Skip until next version"),
    ]
    if allow_exit:
        options.append(("exit", "Exit Pythinker"))
    try:
        selection = await ChoiceInput(
            message="Update now?",
            options=options,
            default="update",
        ).prompt_async()
    except (EOFError, KeyboardInterrupt):
        return UpdatePromptSelection.SKIP
    if selection == "update":
        return UpdatePromptSelection.UPDATE_NOW
    if selection == "dismiss":
        return UpdatePromptSelection.DISMISS_VERSION
    if selection == "exit" and allow_exit:
        return UpdatePromptSelection.EXIT
    return UpdatePromptSelection.SKIP


def _update_prompt_text(current_version: str, latest_version: str) -> Text:
    upgrade_command = _detect_upgrade_command()
    if upgrade_command[:1] == [MANAGED_CHANNEL_MARKER]:
        update_method = (
            f"managed by {upgrade_command[1]} — update via your {upgrade_command[1]} channel"
        )
    elif upgrade_command == [NATIVE_INSTALLER_MARKER]:
        update_method = "downloads the native updater automatically"
    else:
        update_method = _format_upgrade_command(upgrade_command)
    _t = _get_tui_tokens()
    return Text.assemble(
        ("\n  ✨ ", _t.accent),
        ("Update available!", "bold"),
        (f" {current_version} -> {latest_version}", _t.muted),
        ("\n  Release notes: ", _t.muted),
        (CHANGELOG_URL_EN, f"{_t.muted} underline"),
        ("\n  Update method: ", _t.muted),
        (update_method, "bold"),
        ("\n", ""),
    )


def _is_homebrew_upgrade_command(command: list[str]) -> bool:
    return len(command) >= 3 and command[:2] == ["brew", "upgrade"]


# Homebrew >= 5 refuses to read formulas from third-party taps until the user
# runs `brew trust <tap>` (gated on $HOMEBREW_REQUIRE_TAP_TRUST). The hard
# refusal names the tap our formula lives in and is authoritative; the soft
# "Skipping <tap>" warning is also emitted for unrelated taps during
# `brew update`, so it only counts when it names our tap.
_BREW_REFUSED_UNTRUSTED_TAP_RE = re.compile(r"Refusing to load .+ from untrusted tap (\S+)")
_BREW_SKIPPED_UNTRUSTED_TAP_RE = re.compile(r"Skipping (\S+) because it is not trusted")


def _homebrew_untrusted_tap(output_lines: list[str]) -> str | None:
    """Tap named in Homebrew's untrusted-tap refusal/skip output, or None."""
    for line in output_lines:
        match = _BREW_REFUSED_UNTRUSTED_TAP_RE.search(line)
        if match:
            return match.group(1).rstrip(".")
    for line in output_lines:
        match = _BREW_SKIPPED_UNTRUSTED_TAP_RE.search(line)
        if match and "pythinker" in match.group(1):
            return match.group(1).rstrip(".")
    return None


def _can_prompt_to_trust_tap(print_output: bool) -> bool:
    """Consent to `brew trust` needs a real terminal; logs/callbacks cannot answer."""
    return print_output and sys.stdout.isatty()


async def _confirm_brew_trust(tap: str) -> bool:
    """One-keypress consent to trust the tap. Declines on EOF/interrupt."""
    from prompt_toolkit.shortcuts.choice_input import ChoiceInput

    _t = _get_tui_tokens()
    console.print(
        f"[{_t.warning}]Homebrew now requires a one-time 'brew trust' before "
        "installing from third-party taps.[/]"
    )
    try:
        selection = await ChoiceInput(
            message=f"Trust the tap {tap} and retry the update?",
            options=[
                ("trust", f"Run: brew trust {tap}"),
                ("skip", "Not now"),
            ],
            default="trust",
        ).prompt_async()
    except (EOFError, KeyboardInterrupt):
        return False
    return selection == "trust"


def _installed_homebrew_version() -> str | None:
    """Return the highest pythinker-code version Homebrew reports as installed.

    Shelling out is required: the running interpreter's own
    ``importlib.metadata`` still reports the pre-upgrade version until the
    process restarts, so it cannot confirm an in-place upgrade. ``None`` means
    "could not determine" (brew missing, formula not found, parse failure) — the
    caller treats that as inconclusive rather than a failed upgrade.
    """
    try:
        result = subprocess.run(
            ["brew", "list", "--versions", "pythinker-code"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=get_clean_env(),
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        logger.exception("Failed to read installed Homebrew version:")
        return None
    if result.returncode != 0:
        return None
    versions = re.findall(r"\d+\.\d+\.\d+", result.stdout)
    if not versions:
        return None
    return max(versions, key=semver_tuple)


def _native_update_asset_name(version: str) -> str | None:
    linux_package_kind = _installed_linux_package_kind()
    if _is_windows():
        return native_installer_asset_name(version)
    if linux_package_kind is not None:
        return _linux_package_asset_name(version, linux_package_kind)
    return native_archive_asset_name(version)


async def _release_has_asset_pair(
    session: aiohttp.ClientSession, version: str, asset_name: str
) -> bool:
    url = native_installer_release_url(channel=f"v{version}")
    try:
        async with session.get(url, headers={"Accept": "application/vnd.github+json"}) as resp:
            if resp.status != 200:
                logger.warning(
                    "GitHub release asset readiness check returned {status}", status=resp.status
                )
                return False
            payload = await resp.json(content_type=None)
    except Exception:
        logger.exception("Failed to check GitHub release asset readiness:")
        return False

    if not isinstance(payload, Mapping):
        return False
    payload_map = cast(Mapping[str, object], payload)
    assets = payload_map.get("assets")
    if not isinstance(assets, list):
        return False
    assets_list = cast(list[object], assets)
    names: set[str] = set()
    for asset_obj in assets_list:
        if not isinstance(asset_obj, Mapping):
            continue
        asset = cast(Mapping[str, object], asset_obj)
        name = asset.get("name")
        if isinstance(name, str):
            names.add(name)
    return asset_name in names and f"{asset_name}.sha256" in names


async def _pypi_version_available(session: aiohttp.ClientSession, version: str) -> bool:
    try:
        async with session.get(
            PYPI_VERSION_URL.format(version=version),
            headers={"Accept": "application/json"},
        ) as resp:
            if resp.status == 200:
                return True
            if resp.status == 404:
                return False
            logger.warning("PyPI version readiness check returned {status}", status=resp.status)
            return False
    except Exception:
        logger.exception("Failed to check PyPI version readiness:")
        return False


async def _homebrew_formula_version_available(session: aiohttp.ClientSession, version: str) -> bool:
    try:
        async with session.get(HOMEBREW_FORMULA_URL) as resp:
            if resp.status != 200:
                logger.warning(
                    "Homebrew formula readiness check returned {status}", status=resp.status
                )
                return False
            formula = await resp.text()
    except Exception:
        logger.exception("Failed to check Homebrew formula readiness:")
        return False
    return f'version "{version}"' in formula


async def _update_candidate_unavailable_reason(
    session: aiohttp.ClientSession, latest_version: str, upgrade_command: list[str]
) -> str | None:
    """Return a user-facing reason when the latest release cannot be installed yet.

    The GitHub Release is created before every downstream channel necessarily
    finishes publishing. Without these readiness gates, startup can advertise a
    version whose native asset, Homebrew formula, or PyPI wheel is still in
    flight; `/update` then either fails or exits 0 without changing anything.
    """
    if upgrade_command == [NATIVE_INSTALLER_MARKER]:
        asset_name = _native_update_asset_name(latest_version)
        if asset_name is None:
            return "No native updater asset is published for this platform."
        if not await _release_has_asset_pair(session, latest_version, asset_name):
            return (
                f"Pythinker {latest_version} is released, but {asset_name} is still "
                "publishing. Try /update again in a few minutes."
            )
        return None

    if _is_homebrew_upgrade_command(upgrade_command):
        if not await _homebrew_formula_version_available(session, latest_version):
            return (
                f"Pythinker {latest_version} is released, but the Homebrew formula is still "
                "publishing. Try /update again in a few minutes."
            )
        return None

    if not await _pypi_version_available(session, latest_version):
        return (
            f"Pythinker {latest_version} is released, but the PyPI package is still "
            "publishing. Try /update again in a few minutes."
        )
    return None


async def _fetch_native_release_asset(
    session: aiohttp.ClientSession, asset_name: str, channel: str
) -> tuple[str, str] | None:
    """Return (download_url, sha256) for a native release asset, or None on failure."""
    url = native_installer_release_url(channel=channel)
    try:
        async with session.get(url, headers={"Accept": "application/vnd.github+json"}) as resp:
            if resp.status != 200:
                logger.warning("GitHub release lookup returned {status}", status=resp.status)
                return None
            payload = await resp.json()
    except Exception:
        logger.exception("Failed to look up native release")
        return None

    download_url: str | None = None
    sha256_url: str | None = None
    for asset in payload.get("assets", []):
        name = asset.get("name", "")
        if name == asset_name:
            download_url = asset.get("browser_download_url")
        elif name == asset_name + ".sha256":
            sha256_url = asset.get("browser_download_url")
    if not download_url or not sha256_url:
        logger.warning("Native asset {name} not found on release", name=asset_name)
        return None

    try:
        async with session.get(sha256_url) as resp:
            text = (await resp.text()).strip()
    except Exception:
        logger.exception("Failed to fetch native asset sha256")
        return None
    sha = text.split()[0] if text else ""
    if len(sha) != 64:
        logger.warning("Native asset sha256 has unexpected length: {n}", n=len(sha))
        return None
    return download_url, sha


def _windows_native_installer_args() -> list[str]:
    """Installer arguments for user-initiated Windows native updates.

    Keep this transparent and boring. Hidden, encoded, or fully suppressed
    updater chains look like commodity malware to command-line heuristics.
    ``/SILENT`` still avoids the wizard, but leaves normal installer UI/errors
    visible and delegates app-closing to Inno Setup's Restart Manager.

    ``/PID`` hands the installer this process's id so its ``InitializeSetup``
    code can wait for us to fully exit before its Restart Manager scan runs.
    Without it, the scan races our teardown, finds ``pythinker.exe`` still
    holding ``_internal`` locks, and shows a spurious "could not close the
    program" dialog even though the update then succeeds.
    """
    return [
        "/SILENT",
        "/NORESTART",
        "/CURRENTUSER",
        "/CLOSEAPPLICATIONS",
        "/NORESTARTAPPLICATIONS",
        f"/PID={os.getpid()}",
    ]


def _spawn_detached_windows_installer(installer_path: Path) -> bool:
    """Run the native installer directly, without PowerShell or cmd wrappers.

    The caller exits immediately after spawning, which releases the running
    ``pythinker.exe`` handle. The installer itself handles closing/replacing
    files through Inno Setup's Restart Manager support.
    """
    if not _is_windows():
        return False
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    try:
        subprocess.Popen(
            [str(installer_path), *_windows_native_installer_args()],
            creationflags=CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError:
        logger.exception("Failed to spawn detached Windows native installer:")
        return False
    return True


def _run_native_installer(installer_path: Path) -> None:
    """Spawn the downloaded native installer and exit this process.

    ``close_fds=True`` is critical: PyInstaller's official Windows subprocess
    recipe notes that child processes otherwise inherit open file handles,
    including the handle to ``pythinker.exe`` itself. Inheriting that handle can
    keep the parent binary locked even after this process exits.
    """
    if _spawn_detached_windows_installer(installer_path):
        sys.exit(0)
    try:
        subprocess.Popen(
            [str(installer_path), *_windows_native_installer_args()],
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0),
            close_fds=True,
        )
    except Exception as exc:
        logger.exception("Failed to launch Windows native installer fallback:")
        _t = _get_tui_tokens()
        console.print(f"[{_t.error}]Failed to launch the Windows installer.[/]")
        console.print(f"[{_t.muted}]Run it manually: {installer_path}[/]")
        raise typer.Exit(1) from exc
    sys.exit(0)


async def _download_native_asset(
    session: aiohttp.ClientSession, asset_name: str, download_url: str, destination: Path
) -> UpdateResult:
    try:
        async with session.get(download_url) as resp:
            if resp.status != 200:
                logger.warning(
                    "Native asset {name} download returned {status}",
                    name=asset_name,
                    status=resp.status,
                )
                return UpdateResult.FAILED
            with destination.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    fh.write(chunk)
    except Exception:
        logger.exception("Native asset {name} download failed", name=asset_name)
        return UpdateResult.FAILED
    return UpdateResult.UPDATED


def _verify_sha256(path: Path, expected_sha: str) -> bool:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            digest.update(chunk)
    actual_sha = digest.hexdigest()
    if actual_sha != expected_sha:
        logger.error(
            "Native asset sha mismatch: expected={expected} actual={actual}",
            expected=expected_sha,
            actual=actual_sha,
        )
        return False
    return True


def _linux_package_arches() -> tuple[str, str] | None:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "amd64", "x86_64"
    if machine in {"aarch64", "arm64"}:
        return "arm64", "aarch64"
    return None


def _is_linux_system_package_executable() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        executable = Path(sys.executable).resolve()
    except OSError:
        return False
    return executable == Path("/usr/lib/pythinker/pythinker")


def _installed_linux_package_kind() -> str | None:
    """Return the native Linux package kind for the current install, if known."""
    if not _is_linux_system_package_executable():
        return None
    env = get_clean_env()
    if which("dpkg-query") is not None:
        try:
            result = subprocess.run(
                ["dpkg-query", "-W", "-f=${Status}", "pythinker-code"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.exception("Failed to query dpkg package state:")
        else:
            if result.returncode == 0 and "install ok installed" in result.stdout:
                return "deb"
    if which("rpm") is not None:
        try:
            result = subprocess.run(
                ["rpm", "-q", "pythinker-code"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.exception("Failed to query rpm package state:")
        else:
            if result.returncode == 0:
                return "rpm"
    return None


def _linux_package_asset_name(version: str, package_kind: str) -> str | None:
    arches = _linux_package_arches()
    if arches is None:
        return None
    deb_arch, rpm_arch = arches
    if package_kind == "deb":
        return f"pythinker-code_{version}_{deb_arch}.deb"
    if package_kind == "rpm":
        return f"pythinker-code-{version}.{rpm_arch}.rpm"
    return None


def _with_sudo(command: list[str]) -> list[str] | None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return command
    if which("sudo") is None:
        return None
    return ["sudo", *command]


def _linux_package_install_command(asset: Path, package_kind: str) -> list[str] | None:
    if package_kind == "deb":
        if which("apt-get") is not None:
            return _with_sudo(["apt-get", "install", "-y", str(asset)])
        if which("dpkg") is not None:
            return _with_sudo(["dpkg", "-i", str(asset)])
        return None
    if package_kind == "rpm":
        if which("dnf") is not None:
            return _with_sudo(["dnf", "install", "-y", str(asset)])
        if which("zypper") is not None:
            return _with_sudo(["zypper", "--non-interactive", "install", str(asset)])
        if which("rpm") is not None:
            return _with_sudo(["rpm", "-Uvh", str(asset)])
    return None


def _install_linux_package(asset: Path, package_kind: str) -> UpdateResult:
    command = _linux_package_install_command(asset, package_kind)
    if command is None:
        logger.warning("No native package install command available for {kind}", kind=package_kind)
        return UpdateResult.FAILED
    try:
        result = subprocess.run(command, env=get_clean_env())
    except OSError:
        logger.exception("Failed to run native package installer:")
        return UpdateResult.FAILED
    return UpdateResult.UPDATED if result.returncode == 0 else UpdateResult.FAILED


def staged_native_path() -> Path:
    """Side path next to the running executable where a downloaded native update is
    held until it is promoted into place. Deterministic so the orchestrator's smoke
    check and the exit-time promotion agree on the same file."""
    target = Path(sys.executable).resolve()
    return target.with_name(f".{target.name}.staged")


def register_staged_native_promotion() -> None:
    """Arrange for the staged native binary to replace the running executable at
    process exit.

    Swapping at exit — rather than in place mid-session — is the whole point: the
    running onefile build reads its Python archive lazily from ``sys.executable``,
    so overwriting that path while the process is alive corrupts later imports
    (``zlib.error: incorrect header check``). At exit no further imports happen, so
    the swap is safe, and the new binary goes live on the next launch — exactly what
    the "Updated → vX. Restart to apply." notice promises.

    Registering more than once (e.g. a silent update plus a manual ``/update`` in the
    same session) is harmless: the handler no-ops once the staged file has been
    promoted, so any duplicate registration just runs a second no-op.
    """
    atexit.register(_promote_staged_native_update)


def discard_staged_native_update() -> None:
    """Remove a staged binary that must not be promoted (e.g. it failed its smoke
    check). Fail-open: a leftover staged file is only ever promoted deliberately."""
    with contextlib.suppress(OSError):
        staged_native_path().unlink()


def _promote_staged_native_update() -> None:
    # ponytail: known residual windows, both far smaller than the mid-session login
    # crash this replaces — (1) a hard kill (SIGKILL) skips atexit, so promotion
    # defers to the next clean exit (self-healing); (2) a first-time lazy import
    # during the rest of interpreter shutdown, after the swap below, could read
    # stale bytes. Closing both needs a detached post-exit swapper (the deferred
    # seamless-relaunch design); not built here.
    staged = staged_native_path()
    if not staged.is_file():
        return
    target = Path(sys.executable).resolve()
    try:
        os.replace(staged, target)
    except OSError:
        # Leave the staged binary in place; a later clean exit retries the swap.
        logger.exception("Failed to promote staged native update on exit:")


def _install_native_archive(archive: Path) -> UpdateResult:
    target = Path(sys.executable).resolve()
    extract_dir = archive.parent / "extract"
    extract_dir.mkdir()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extract("pythinker", path=extract_dir, filter="data")
    except Exception:
        logger.exception("Failed to extract native archive")
        return UpdateResult.FAILED

    extracted = extract_dir / "pythinker"
    if not extracted.is_file():
        logger.error("Native archive did not contain a pythinker executable")
        return UpdateResult.FAILED

    # Stage the new binary beside the running executable instead of overwriting it
    # in place. Promotion into `target` happens at process exit (see
    # register_staged_native_promotion), keeping the live build's on-disk archive
    # intact so mid-session lazy imports never read stale bytes.
    staged = staged_native_path()
    staging_tmp = target.with_name(f".{target.name}.new-{os.getpid()}")
    try:
        shutil.copyfile(extracted, staging_tmp)
        staging_tmp.chmod(target.stat().st_mode | 0o755)
        os.replace(staging_tmp, staged)
        (target.parent / ".pythinker-native").write_text(
            "pythinker-native-build\n", encoding="utf-8"
        )
    except OSError:
        logger.exception("Failed to stage native executable:")
        with contextlib.suppress(OSError):
            staging_tmp.unlink()
        return UpdateResult.FAILED
    return UpdateResult.UPDATED


def _windows_update_staging_parent() -> Path:
    return get_share_dir() / "windows-update-staging"


def _windows_staged_manifest_path() -> Path:
    return _windows_update_staging_parent() / "staged-update.json"


@dataclass(slots=True)
class StagedWindowsUpdate:
    """A verified, ready-to-apply Windows installer staged for the next restart."""

    version: str
    installer_path: Path
    sha256: str
    created_at: float


def _write_windows_staged_manifest(update: StagedWindowsUpdate) -> bool:
    """Atomically record a staged Windows update (write temp + os.replace).

    An interrupted write can never produce a ready-to-apply state: readers only
    ever see the previous manifest or the complete new one.
    """
    manifest = _windows_staged_manifest_path()
    payload = {
        "version": update.version,
        "installer_path": str(update.installer_path),
        "sha256": update.sha256,
        "created_at": update.created_at,
        "state": "ready",
    }
    tmp = manifest.with_name(f".{manifest.name}.{os.getpid()}.tmp")
    try:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, manifest)
    except OSError:
        logger.exception("Failed to write staged Windows update manifest:")
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    return True


def read_windows_staged_update(manifest_path: Path | None = None) -> StagedWindowsUpdate | None:
    """Parse and shape-validate the staged-update manifest, or None.

    ``manifest_path`` defaults to the canonical manifest path; callers that
    have exclusively claimed a manifest (see :func:`_claim_windows_staged_manifest`)
    pass its claimed path so validation and any discard operate on the file
    they own, not a path a concurrent claimant may have already taken.

    Content validation (digest, version supersession) happens at apply time in
    :func:`apply_windows_staged_update_now`; a malformed manifest is discarded
    here so it cannot linger and be retried forever.
    """
    manifest = manifest_path if manifest_path is not None else _windows_staged_manifest_path()
    try:
        raw = manifest.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("Failed to read staged Windows update manifest:")
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        discard_windows_staged_update("manifest is not valid JSON", manifest_path=manifest)
        return None
    if not isinstance(payload, dict):
        discard_windows_staged_update("manifest has an unexpected shape", manifest_path=manifest)
        return None
    data = cast(dict[str, object], payload)
    version = data.get("version")
    installer_path = data.get("installer_path")
    sha256 = data.get("sha256")
    created_at = data.get("created_at")
    state = data.get("state")
    if (
        not isinstance(version, str)
        or not isinstance(installer_path, str)
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or not isinstance(created_at, int | float)
        or state != "ready"
    ):
        discard_windows_staged_update(
            "manifest fields are missing or malformed", manifest_path=manifest
        )
        return None
    return StagedWindowsUpdate(
        version=version,
        installer_path=Path(installer_path),
        sha256=sha256,
        created_at=float(created_at),
    )


def discard_windows_staged_update(reason: str, *, manifest_path: Path | None = None) -> None:
    """Drop the staged manifest at ``manifest_path`` (default: canonical path)
    and its installer directory. Fail closed: a stage that cannot be trusted is
    removed rather than retried."""
    logger.warning("Discarding staged Windows update: {reason}", reason=reason)
    manifest = manifest_path if manifest_path is not None else _windows_staged_manifest_path()
    payload: dict[str, object] | None = None
    try:
        parsed: object = json.loads(manifest.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            payload = cast(dict[str, object], parsed)
    except (OSError, json.JSONDecodeError):
        payload = None
    with contextlib.suppress(OSError):
        manifest.unlink(missing_ok=True)
    installer_path = payload.get("installer_path") if payload else None
    if isinstance(installer_path, str):
        installer_dir = Path(installer_path).parent
        if installer_dir.parent == _windows_update_staging_parent():
            shutil.rmtree(installer_dir, ignore_errors=True)


def _claim_windows_staged_manifest() -> Path | None:
    """Atomically claim the canonical staged-update manifest so only one
    process ever applies a given stage.

    Renaming the manifest to a per-claim path is atomic on the same
    filesystem: if two processes race to apply the same stage, only one
    ``os.rename`` succeeds — the loser sees ``FileNotFoundError`` and returns
    None. This closes the two-process race where both could otherwise pass
    validation and spawn duplicate installers. Returns None when nothing is
    staged or another process already claimed it.
    """
    manifest = _windows_staged_manifest_path()
    claimed = manifest.with_name(f"{manifest.name}.claimed-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        os.rename(manifest, claimed)
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("Failed to claim staged Windows update manifest:")
        return None
    return claimed


def apply_windows_staged_update_now() -> bool:
    """Launch the staged installer after re-validating it. True when spawned.

    Callers must exit promptly after a True return: the installer's ``/PID``
    handshake waits for this process to release the executable lock. Any
    validation failure discards the stage and returns False (fail closed) —
    startup then continues on the current version.

    Exclusively claims the manifest first (see
    :func:`_claim_windows_staged_manifest`): two Pythinker processes racing to
    apply the same stage (concurrent shell launches, or concurrent
    ``apply_on_exit`` sessions) must never both pass validation and spawn
    duplicate installers. The loser of the claim simply has nothing to apply.
    """
    from pythinker_code.constant import VERSION as current_version

    claimed_path = _claim_windows_staged_manifest()
    if claimed_path is None:
        return False
    staged = read_windows_staged_update(claimed_path)
    if staged is None:
        return False  # already discarded against claimed_path
    if semver_tuple(staged.version) <= semver_tuple(current_version):
        discard_windows_staged_update(
            f"staged version {staged.version} is not newer than {current_version}",
            manifest_path=claimed_path,
        )
        return False
    if not staged.installer_path.is_file():
        discard_windows_staged_update(
            "staged installer file is missing", manifest_path=claimed_path
        )
        return False
    if not _verify_sha256(staged.installer_path, staged.sha256):
        discard_windows_staged_update(
            "staged installer failed digest verification", manifest_path=claimed_path
        )
        return False
    if not _spawn_detached_windows_installer(staged.installer_path):
        discard_windows_staged_update(
            "staged installer could not be launched", manifest_path=claimed_path
        )
        return False
    # The installer owns the staging directory from here. We hold the only
    # reference to the claimed manifest, so dropping it is unconditionally
    # safe — no other process can have claimed the same stage, and a newer
    # concurrently-staged update lives under the canonical path untouched.
    with contextlib.suppress(OSError):
        claimed_path.unlink(missing_ok=True)
    logger.info(
        "Launched staged Windows installer for {version}; exiting to release file locks.",
        version=staged.version,
    )
    return True


def apply_staged_update_before_start() -> bool:
    """Pre-session bootstrap: apply a verified staged Windows update, if any.

    Runs before any session/runtime construction. Returns True when the
    installer was spawned and the caller must exit immediately; False continues
    normal startup (including after a discarded invalid stage — fail closed,
    never fail the launch).
    """
    if not _is_windows():
        return False
    if _auto_update_disabled() or _is_running_from_source_checkout():
        return False
    if not apply_windows_staged_update_now():
        return False
    console.print("Applying staged Pythinker update — relaunch once the installer finishes.")
    return True


_windows_apply_on_exit_armed = False


def register_windows_staged_apply_on_exit() -> None:
    """Arrange for the staged Windows installer to launch at clean process exit.

    Only used by the ``apply_on_exit`` policy. Registration is idempotent, and
    the handler re-validates the stage at fire time, so arming is safe even if
    the stage is later superseded or discarded.
    """
    global _windows_apply_on_exit_armed
    if _windows_apply_on_exit_armed:
        return
    _windows_apply_on_exit_armed = True
    atexit.register(apply_windows_staged_update_now)


def _cleanup_stale_windows_update_staging(now: float | None = None) -> None:
    if not _is_windows():
        return
    parent = _windows_update_staging_parent()
    if not parent.exists():
        return
    # Never prune the directory the current staged manifest points at — the
    # staged installer must survive until it is applied or superseded.
    staged = read_windows_staged_update()
    referenced = staged.installer_path.parent if staged is not None else None
    cutoff = (time.time() if now is None else now) - WINDOWS_UPDATE_STAGING_MAX_AGE_SECONDS
    for child in parent.glob("pythinker-update-*"):
        try:
            if child == referenced:
                continue
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            logger.exception("Failed to inspect stale Windows update staging directory:")


def _make_native_update_tmpdir() -> Path:
    import tempfile

    if _is_windows():
        _cleanup_stale_windows_update_staging()
        staging_parent = _windows_update_staging_parent()
        try:
            staging_parent.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="pythinker-update-", dir=staging_parent))
        except OSError:
            logger.exception("Failed to create Windows update staging directory; using temp:")
    return Path(tempfile.mkdtemp(prefix="pythinker-update-"))


async def _maybe_run_native_update(
    latest_version: str, channel: str = "latest", *, intent: UpdateIntent = UpdateIntent.INSTALL
) -> UpdateResult:
    """Native-build update path: download, verify, then stage or install per intent."""
    linux_package_kind = _installed_linux_package_kind()
    if linux_package_kind is not None and intent is UpdateIntent.STAGE_FOR_RESTART:
        # System-package installs need an inline (often sudo) package-manager
        # run; that is never allowed from a background task. Surface the
        # update as available instead.
        logger.info("Background update deferred: Linux package installs are foreground-only")
        return UpdateResult.UPDATE_AVAILABLE
    if _is_windows():
        asset_name = native_installer_asset_name(latest_version)
    elif linux_package_kind is not None:
        asset_name = _linux_package_asset_name(latest_version, linux_package_kind)
    else:
        asset_name = native_archive_asset_name(latest_version)
    if asset_name is None:
        logger.warning("No native updater asset is published for this platform")
        return UpdateResult.FAILED

    # No fixed `total`: a large installer on a slow link must not be aborted
    # mid-download. Bound it with per-chunk `sock_read` instead (aiohttp guidance
    # for large streamed downloads).
    timeout = aiohttp.ClientTimeout(sock_connect=10, sock_read=60)
    async with new_client_session(timeout=timeout) as session:
        fetched = await _fetch_native_release_asset(session, asset_name, channel)
        if fetched is None:
            return UpdateResult.FAILED
        download_url, expected_sha = fetched

        tmpdir = _make_native_update_tmpdir()
        # On Windows, the spawned installer must keep its staged .exe after
        # this process exits, so a later launch prunes stale staging dirs. On
        # Linux/Mac the install runs inline, so we own cleanup and must release
        # ~50-100MB of archive + extracted-binary debris from /tmp on every
        # update, success or fail.
        cleanup_tmpdir = True
        try:
            asset = tmpdir / asset_name
            download_result = await _download_native_asset(session, asset_name, download_url, asset)
            if download_result is UpdateResult.FAILED:
                return download_result

            if not _verify_sha256(asset, expected_sha):
                return UpdateResult.FAILED

            if _is_windows():
                if intent is UpdateIntent.INSTALL_AND_EXIT:
                    # Standalone `pythinker update` / pre-start prompt: hand off
                    # to the installer and exit. Flag flip before sys.exit so
                    # the finally honors it — the detached helper now owns the
                    # staging directory.
                    cleanup_tmpdir = False
                    _run_native_installer(asset)
                    return UpdateResult.UPDATED  # unreachable; sys.exit fires above
                # In-session (background or /update): stage for restart. The
                # installer runs from the pre-session bootstrap on the next
                # launch (or at clean exit under apply_on_exit); the live
                # session is never interrupted.
                staged = StagedWindowsUpdate(
                    version=latest_version,
                    installer_path=asset,
                    sha256=expected_sha,
                    created_at=time.time(),
                )
                if not _write_windows_staged_manifest(staged):
                    return UpdateResult.FAILED
                cleanup_tmpdir = False
                return UpdateResult.UPDATED
            if linux_package_kind is not None:
                return _install_linux_package(asset, linux_package_kind)
            return _install_native_archive(asset)
        finally:
            if cleanup_tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)


def _run_upgrade_command(
    command: list[str],
    *,
    print_output: bool,
    output_callback: Callable[[str], None] | None,
) -> int:
    def _emit(text: str) -> None:
        if output_callback is not None:
            output_callback(text)
        if print_output:
            console.print(text, markup=False)

    env = get_clean_env()
    if command[:1] == ["brew"]:
        # Self-upgrade hardening: pythinker is the running Homebrew formula.
        # `brew upgrade` installs the new version side-by-side, but its
        # post-upgrade cleanup would delete the in-use old Cellar version that
        # `sys.executable` resolves through — crashing the live session before the
        # user restarts. Suppress cleanup so the swap only takes effect on the next
        # launch (matching the "Restart to apply" notice). Also skip brew's
        # implicit pre-command auto-update: `_refresh_brew_metadata` already
        # refreshed the tap explicitly, so the implicit pass is only redundant
        # network/lock work during the user's session.
        env["HOMEBREW_NO_INSTALL_CLEANUP"] = "1"
        env["HOMEBREW_NO_AUTO_UPDATE"] = "1"

    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        bufsize=1,
    )

    def _drain_stdout() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            _emit(line.rstrip("\n"))

    output_thread = threading.Thread(target=_drain_stdout, daemon=True)
    output_thread.start()
    try:
        return proc.wait(timeout=UPGRADE_COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _emit(f"Upgrade command timed out after {UPGRADE_COMMAND_TIMEOUT_SECONDS} seconds.")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return 124
    finally:
        output_thread.join(timeout=5)


async def do_update(
    *,
    print_output: bool = True,
    intent: UpdateIntent = UpdateIntent.INSTALL,
    output_callback: Callable[[str], None] | None = None,
) -> UpdateResult:
    async with _UPDATE_LOCK:
        return await _do_update(
            print_output=print_output,
            intent=intent,
            output_callback=output_callback,
        )


async def _do_update(
    *,
    print_output: bool,
    intent: UpdateIntent,
    output_callback: Callable[[str], None] | None,
) -> UpdateResult:
    from pythinker_code.constant import VERSION as current_version

    _t = _get_tui_tokens()

    def _print(message: str) -> None:
        if output_callback is not None:
            output_callback(message)
        if print_output:
            console.print(message)

    timeout = aiohttp.ClientTimeout(total=15, sock_connect=5, sock_read=10)
    async with new_client_session(timeout=timeout) as session:
        logger.info("Checking for updates...")
        _print("Checking for updates...")
        latest_version = await _get_latest_version(session)
        if not latest_version:
            _print(f"[{_t.error}]Failed to check for updates.[/]")
            return UpdateResult.FAILED

        logger.debug("Latest version: {latest_version}", latest_version=latest_version)
        if semver_tuple(current_version) >= semver_tuple(latest_version):
            try:
                LATEST_VERSION_FILE.write_text(latest_version, encoding="utf-8")
            except OSError:
                logger.exception("Failed to cache latest version:")
            logger.debug("Already up to date: {current_version}", current_version=current_version)
            _print(f"[{_t.success}]Already up to date.[/]")
            return UpdateResult.UP_TO_DATE

        upgrade_command = _detect_upgrade_command()
        if upgrade_command[:1] == [MANAGED_CHANNEL_MARKER]:
            try:
                LATEST_VERSION_FILE.write_text(latest_version, encoding="utf-8")
            except OSError:
                logger.exception("Failed to cache latest version:")
            notice = format_managed_channel_notice(
                current_version,
                latest_version,
                upgrade_command=upgrade_command,
            )
            if notice:
                _print(f"[{_t.warning}]{notice}[/]")
            return UpdateResult.UPDATE_AVAILABLE
        unavailable_reason = await _update_candidate_unavailable_reason(
            session, latest_version, upgrade_command
        )
        if unavailable_reason:
            logger.info(
                "Latest version is not installable yet: {reason}", reason=unavailable_reason
            )
            # The latest GitHub Release can appear before channel-specific
            # assets/formulae/PyPI files are live. Do not cache that version;
            # otherwise the background notifier will keep advertising an
            # update that cannot be installed yet.
            _clear_latest_version_cache()
            _clear_cached_etag()
            _print(f"[{_t.warning}]{unavailable_reason}[/]")
            return UpdateResult.FAILED

    try:
        LATEST_VERSION_FILE.write_text(latest_version, encoding="utf-8")
    except OSError:
        logger.exception("Failed to cache latest version:")

    if intent is UpdateIntent.CHECK:
        logger.info(
            "Update available: current={current_version}, latest={latest_version}",
            current_version=current_version,
            latest_version=latest_version,
        )
        _print(f"[{_t.warning}]Update available: {current_version} → {latest_version}[/]")
        return UpdateResult.UPDATE_AVAILABLE

    is_native_update = upgrade_command == [NATIVE_INSTALLER_MARKER]
    if intent is UpdateIntent.STAGE_FOR_RESTART and not is_native_update:
        # Background tasks must never run package-manager upgrades (pip/uv/
        # pipx/brew) inline: those subprocesses mutate the live install and,
        # for pip-on-self, can exit the process. Surface the update instead;
        # the persistent notice points the user at /update.
        logger.info(
            "Background update deferred: {cmd} is foreground-only",
            cmd=_format_upgrade_command(upgrade_command),
        )
        _print(f"[{_t.warning}]Update available: {current_version} → {latest_version}[/]")
        return UpdateResult.UPDATE_AVAILABLE
    upgrade_command_text = (
        "native installer" if is_native_update else _format_upgrade_command(upgrade_command)
    )
    logger.info(
        "Updating from {current_version} to {latest_version} via: {cmd}",
        current_version=current_version,
        latest_version=latest_version,
        cmd=upgrade_command_text,
    )
    _print(f"Updating pythinker-code {current_version} → {latest_version}...")
    if not is_native_update:
        _print(f"[{_t.muted}]Running: {upgrade_command_text}[/]")

    if is_native_update:
        _print(f"[{_t.muted}]Downloading native installer from GitHub Releases...[/]")
        if _is_windows() and intent is UpdateIntent.INSTALL_AND_EXIT:
            _print(
                f"[{_t.warning}]Pythinker will exit after staging the installer; "
                "the signed Windows installer will continue normally.[/]"
            )
        native_result = await _maybe_run_native_update(latest_version, intent=intent)
        if native_result is UpdateResult.UPDATE_AVAILABLE:
            _print(
                f"[{_t.warning}]Auto-update disabled. "
                "Download the new installer manually from "
                "https://github.com/Pythoughts-labs/pythinker-code/releases/latest[/]"
            )
            return UpdateResult.UPDATE_AVAILABLE
        if native_result is UpdateResult.FAILED:
            _print(
                f"[{_t.error}]Native update failed. Download manually from the releases page.[/]"
            )
            return UpdateResult.FAILED
        if native_result is UpdateResult.UPDATED:
            _print(f"[{_t.success}]Updated successfully![/]")
            _print(f"[{_t.warning}]Restart Pythinker CLI to use the new version.[/]")
        return native_result

    # Brew failure diagnosis (untrusted tap, silent no-op) needs the upgrade
    # output; capture it while still forwarding every line to the caller.
    captured_output: list[str] = []

    def _run_streamed(command: list[str]) -> int:
        def _capture(line: str) -> None:
            captured_output.append(line)
            if output_callback is not None:
                output_callback(line)

        return _run_upgrade_command(command, print_output=print_output, output_callback=_capture)

    def _refresh_brew_metadata() -> None:
        # `brew upgrade <formula>` resolves against the locally-cloned tap
        # formula; a stale clone pins the old version and the upgrade silently
        # no-ops ("already installed"). Refresh the tap first. Best-effort: if
        # the refresh fails we still attempt the upgrade, and the post-upgrade
        # version check below catches a no-op.
        _print(f"[{_t.muted}]Refreshing Homebrew metadata: brew update[/]")
        try:
            # --quiet keeps the refresh from dumping the host's full outdated
            # formula/cask list (often dozens of unrelated lines) before our
            # upgrade output.
            refresh_code = _run_streamed(["brew", "update", "--quiet"])
        except OSError:
            logger.exception("brew update failed to launch:")
        else:
            if refresh_code != 0:
                logger.warning(
                    "brew update exited {code}; continuing with upgrade", code=refresh_code
                )

    def _print_brew_trust_hint(tap: str) -> None:
        _print(f"[{_t.warning}]Trust the tap once, then update:[/]")
        _print(f"  brew trust {shlex_quote(tap)}")
        _print(f"  {upgrade_command_text}")

    if _is_homebrew_upgrade_command(upgrade_command):
        _refresh_brew_metadata()

    brew_trust_attempted = False
    while True:
        try:
            returncode = _run_streamed(upgrade_command)
        except OSError as e:
            logger.exception("Upgrade failed:")
            _print(f"[{_t.error}]Upgrade failed:[/] {e}")
            _print(f"Please run manually: {upgrade_command_text}")
            return UpdateResult.FAILED

        if returncode == 0:
            break

        untrusted_tap = (
            _homebrew_untrusted_tap(captured_output)
            if _is_homebrew_upgrade_command(upgrade_command)
            else None
        )
        if untrusted_tap is None:
            _print(f"[{_t.error}]Upgrade failed. Please try running manually:[/]")
            _print(f"  {upgrade_command_text}")
            return UpdateResult.FAILED

        logger.warning("Homebrew refused untrusted tap {tap}", tap=untrusted_tap)
        _print(
            f"[{_t.error}]Upgrade failed: Homebrew refuses to load formulas "
            f"from the untrusted tap {untrusted_tap}.[/]"
        )
        if (
            not brew_trust_attempted
            and _can_prompt_to_trust_tap(print_output)
            and await _confirm_brew_trust(untrusted_tap)
        ):
            brew_trust_attempted = True
            try:
                trust_code = _run_streamed(["brew", "trust", untrusted_tap])
            except OSError:
                logger.exception("brew trust failed to launch:")
                trust_code = 1
            if trust_code == 0:
                # `brew update` skipped the untrusted tap above, so its clone
                # may still be stale — refresh again before retrying.
                captured_output.clear()
                _print(f"[{_t.muted}]Tap trusted. Retrying the upgrade...[/]")
                _refresh_brew_metadata()
                continue
            _print(f"[{_t.error}]'brew trust {untrusted_tap}' failed.[/]")
        _print_brew_trust_hint(untrusted_tap)
        return UpdateResult.FAILED

    if _is_homebrew_upgrade_command(upgrade_command):
        installed = _installed_homebrew_version()
        if installed is not None and semver_tuple(installed) < semver_tuple(latest_version):
            # brew exited 0 without changing anything (stale tap / no-op).
            # Reporting success here is the bug we are fixing: don't.
            _print(
                f"[{_t.error}]Homebrew exited cleanly but pythinker-code is "
                f"still {installed}, not {latest_version}.[/]"
            )
            untrusted_tap = _homebrew_untrusted_tap(captured_output)
            if untrusted_tap is not None:
                # The no-op happened because `brew update` skipped our
                # untrusted tap, pinning the old formula.
                _print_brew_trust_hint(untrusted_tap)
            else:
                _print(
                    f"[{_t.warning}]The Homebrew tap metadata looks stale. "
                    "Run 'brew update' and try '/update' again.[/]"
                )
            return UpdateResult.FAILED
    _print(f"[{_t.success}]Updated successfully![/]")
    _print(f"[{_t.warning}]Restart Pythinker CLI to use the new version.[/]")
    return UpdateResult.UPDATED
