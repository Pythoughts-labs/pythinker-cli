from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pythinker_code.constant import VERSION as CURRENT_VERSION
from pythinker_code.native import is_native_build
from pythinker_code.share import get_share_dir
from pythinker_code.ui.shell.console import console
from pythinker_code.ui.theme import get_tui_tokens as _get_tui_tokens
from pythinker_code.utils.logging import logger
from pythinker_code.utils.subprocess_env import get_clean_env

if TYPE_CHECKING:
    from pythinker_code.ui.shell.update import (
        StagedWindowsUpdate,
        UpdateIntent,
        UpdateResult,
    )

UPDATE_STATUS_FILE = get_share_dir() / "update_status.json"
UPDATE_LOG_FILE = get_share_dir() / "update.log"
UPDATE_LOCK_FILE = get_share_dir() / "update.lock"
UPDATE_LAST_SUCCESS_FILE = get_share_dir() / "update_last_success.json"

_LOCK_STALE_AFTER_SECONDS = 2 * 60 * 60
_LOCK_MALFORMED_GRACE_SECONDS = 60
_SMOKE_CHECK_TIMEOUT_SECONDS = 10

SMOKE_CHECK_FAILED_PREFIX = "Updated, but smoke check did not pass: "


class UpdateJobState(StrEnum):
    IDLE = "idle"
    CHECKING = "checking"
    AVAILABLE = "available"
    RUNNING = "running"
    UPDATED = "updated"
    UP_TO_DATE = "up_to_date"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


@dataclass(slots=True)
class UpdateJobStatus:
    job_id: str
    state: UpdateJobState
    started_at: float | None
    finished_at: float | None
    current_version: str | None
    target_version: str | None
    result: str | None
    message: str | None
    log_path: str
    pid: int | None
    source: str = "unknown"


@dataclass(slots=True)
class UpdateLock:
    path: Path

    def release(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _chmod_owner_only(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_parent(path)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd: int | None = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
        _chmod_owner_only(path)
    except BaseException:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cast(dict[str, Any], payload) if isinstance(payload, dict) else None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # Windows has no POSIX signal-0 probe; avoid os.kill() because it can
        # deliver a real signal. Let lock age decide staleness on Windows.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Be conservative on platforms where signal-0 probing is limited.
        return True
    return True


def _lock_payload_is_stale(payload: dict[str, Any] | None, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    if payload is None:
        return False
    started_at = _optional_float(payload.get("started_at"))
    age = now - started_at if started_at is not None else _LOCK_STALE_AFTER_SECONDS + 1
    if age > _LOCK_STALE_AFTER_SECONDS:
        return True
    parsed_pid = _optional_int(payload.get("pid"))
    if parsed_pid is None:
        return age > 60
    return not _pid_exists(parsed_pid)


def _lock_file_age_seconds(*, now: float | None = None) -> float | None:
    now = time.time() if now is None else now
    try:
        return now - UPDATE_LOCK_FILE.stat().st_mtime
    except OSError:
        return None


def clear_stale_update_lock() -> bool:
    if not UPDATE_LOCK_FILE.exists():
        return False
    payload = _read_json(UPDATE_LOCK_FILE)
    if payload is None:
        age = _lock_file_age_seconds()
        if age is None or age <= _LOCK_MALFORMED_GRACE_SECONDS:
            return False
    elif not _lock_payload_is_stale(payload):
        return False
    with contextlib.suppress(OSError):
        UPDATE_LOCK_FILE.unlink()
        return True
    return False


def acquire_update_lock(*, source: str = "unknown") -> UpdateLock | None:
    _ensure_parent(UPDATE_LOCK_FILE)
    payload = {
        "pid": os.getpid(),
        "source": source,
        "started_at": time.time(),
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    for _ in range(2):
        try:
            fd = os.open(UPDATE_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if clear_stale_update_lock():
                continue
            return None
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(encoded)
        except OSError:
            with contextlib.suppress(OSError):
                os.close(fd)
                UPDATE_LOCK_FILE.unlink()
            raise
        return UpdateLock(UPDATE_LOCK_FILE)
    return None


def write_update_status(status: UpdateJobStatus) -> None:
    payload = asdict(status)
    payload["state"] = status.state.value
    _atomic_write_json(UPDATE_STATUS_FILE, payload)


def read_update_status() -> UpdateJobStatus | None:
    payload = _read_json(UPDATE_STATUS_FILE)
    if payload is None:
        return None
    try:
        state = UpdateJobState(str(payload.get("state") or UpdateJobState.IDLE.value))
    except ValueError:
        state = UpdateJobState.FAILED
    return UpdateJobStatus(
        job_id=str(payload.get("job_id") or ""),
        state=state,
        started_at=_optional_float(payload.get("started_at")),
        finished_at=_optional_float(payload.get("finished_at")),
        current_version=_optional_str(payload.get("current_version")),
        target_version=_optional_str(payload.get("target_version")),
        result=_optional_str(payload.get("result")),
        message=_optional_str(payload.get("message")),
        log_path=str(payload.get("log_path") or UPDATE_LOG_FILE),
        pid=_optional_int(payload.get("pid")),
        source=str(payload.get("source") or "unknown"),
    )


def update_restart_pending(status: UpdateJobStatus | None, target_version: str | None) -> bool:
    return (
        status is not None
        and status.state is UpdateJobState.UPDATED
        and status.target_version == target_version
        and status.pid == os.getpid()
        and not (status.message and status.message.startswith(SMOKE_CHECK_FAILED_PREFIX))
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_float(value: object) -> float | None:
    if not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _optional_int(value: object) -> int | None:
    if not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def append_update_log(message: str) -> None:
    _ensure_parent(UPDATE_LOG_FILE)
    try:
        from pythinker_code.feedback import redact_text

        fd: int | None = os.open(
            UPDATE_LOG_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fd = None
                fh.write(redact_text(message).rstrip("\n") + "\n")
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        _chmod_owner_only(UPDATE_LOG_FILE)
    except OSError:
        logger.exception("Failed to append update log:")


def read_update_log_tail(max_lines: int = 80) -> list[str]:
    if max_lines <= 0:
        return []
    try:
        lines = UPDATE_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-max_lines:]


def _result_state(result: object) -> UpdateJobState:
    from pythinker_code.ui.shell.update import UpdateResult

    if result is UpdateResult.UPDATE_AVAILABLE:
        return UpdateJobState.AVAILABLE
    if result is UpdateResult.UPDATED:
        return UpdateJobState.UPDATED
    if result is UpdateResult.UP_TO_DATE:
        return UpdateJobState.UP_TO_DATE
    if result is UpdateResult.UNSUPPORTED:
        return UpdateJobState.UNSUPPORTED
    return UpdateJobState.FAILED


def _read_target_version() -> str | None:
    from pythinker_code.ui.shell.update import LATEST_VERSION_FILE

    try:
        return LATEST_VERSION_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _new_status(
    *,
    job_id: str,
    state: UpdateJobState,
    source: str,
    started_at: float | None,
    finished_at: float | None = None,
    result: str | None = None,
    message: str | None = None,
) -> UpdateJobStatus:
    return UpdateJobStatus(
        job_id=job_id,
        state=state,
        started_at=started_at,
        finished_at=finished_at,
        current_version=CURRENT_VERSION,
        target_version=_read_target_version(),
        result=result,
        message=message,
        log_path=str(UPDATE_LOG_FILE),
        pid=os.getpid(),
        source=source,
    )


async def run_update_job(
    *,
    print_output: bool = True,
    intent: UpdateIntent | None = None,
    source: str = "cli",
) -> UpdateResult:
    from pythinker_code.ui.shell.update import UpdateIntent, UpdateResult, do_update

    if intent is None:
        intent = UpdateIntent.CHECK

    lock = acquire_update_lock(source=source)
    if lock is None:
        message = "Another Pythinker update is already running."
        job_id = f"blocked-{uuid.uuid4().hex}"
        append_update_log(f"{job_id}: {message}")
        if print_output:
            console.print(f"[{_get_tui_tokens().warning}]{message}[/]")
        return UpdateResult.FAILED

    job_id = uuid.uuid4().hex
    started_at = time.time()
    state = UpdateJobState.CHECKING if intent is UpdateIntent.CHECK else UpdateJobState.RUNNING
    append_update_log(f"\n=== pythinker update {job_id} started ({source}) ===")
    write_update_status(
        _new_status(job_id=job_id, state=state, source=source, started_at=started_at)
    )

    try:
        try:
            result = await do_update(
                print_output=print_output,
                intent=intent,
                output_callback=append_update_log,
            )
        except SystemExit:
            message = "Update helper was launched; Pythinker is exiting to finish the update."
            append_update_log(message)
            write_update_status(
                _new_status(
                    job_id=job_id,
                    state=UpdateJobState.RUNNING,
                    source=source,
                    started_at=started_at,
                    finished_at=None,
                    message=message,
                )
            )
            raise

        reported_result = result
        final_state = _result_state(result)
        message = result.name.replace("_", " ").lower()
        if result is UpdateResult.UPDATED and intent is not UpdateIntent.CHECK:
            staged_windows = _pending_windows_staged_update()
            if staged_windows is not None:
                # Windows stages an installer, not a swappable binary: running
                # `--version` here would exercise the OLD executable and falsely
                # certify the stage. The stage is digest-verified at staging
                # time and re-verified at apply time, so report exactly that.
                message = (
                    f"Update {staged_windows.version} staged (digest verified); "
                    "applied before the next launch."
                )
                append_update_log(message)
                _write_last_success(job_id=job_id, message=message)
            else:
                smoke_ok, smoke_message = await _run_smoke_check_with_retry(
                    target_version=_read_target_version()
                )
                append_update_log(smoke_message)
                if smoke_ok:
                    message = smoke_message
                    _write_last_success(job_id=job_id, message=message)
                    _finalize_native_staging(promote=True)
                else:
                    message = f"{SMOKE_CHECK_FAILED_PREFIX}{smoke_message}"
                    reported_result = UpdateResult.VERIFICATION_FAILED
                    final_state = _result_state(reported_result)
                    # Never promote a staged binary that can't even print --version.
                    _finalize_native_staging(promote=False)
                    # The install step has already printed its own success line;
                    # leaving the screen at "Updated successfully!" while the
                    # recorded state is VERIFICATION_FAILED would misreport the
                    # outcome (the footer keeps the update notice for the same
                    # reason). Surface the failure where the success was shown.
                    if print_output:
                        console.print(f"[{_get_tui_tokens().warning}]{message}[/]")

        write_update_status(
            _new_status(
                job_id=job_id,
                state=final_state,
                source=source,
                started_at=started_at,
                finished_at=time.time(),
                result=reported_result.name,
                message=message,
            )
        )
        return reported_result
    except Exception as exc:
        message = f"Update failed: {exc}"
        append_update_log(message)
        write_update_status(
            _new_status(
                job_id=job_id,
                state=UpdateJobState.FAILED,
                source=source,
                started_at=started_at,
                finished_at=time.time(),
                result=UpdateResult.FAILED.name,
                message=message,
            )
        )
        raise
    finally:
        lock.release()


def _pending_windows_staged_update() -> StagedWindowsUpdate | None:
    """The staged Windows update, or None off-Windows / when nothing is staged."""
    from pythinker_code.ui.shell.update import (
        _is_windows,  # pyright: ignore[reportPrivateUsage]
        read_windows_staged_update,
    )

    if not _is_windows():
        return None
    return read_windows_staged_update()


def _write_last_success(*, job_id: str, message: str) -> None:
    try:
        _atomic_write_json(
            UPDATE_LAST_SUCCESS_FILE,
            {
                "job_id": job_id,
                "finished_at": time.time(),
                "message": message,
            },
        )
    except OSError:
        logger.exception("Failed to write last successful update marker:")


def _smoke_check_command() -> list[str]:
    if is_native_build():
        # A native update is staged beside the running exe, not swapped in place,
        # so validate the staged binary — checking the still-running old exe would
        # prove nothing about the update.
        from pythinker_code.ui.shell.update import staged_native_path

        staged = staged_native_path()
        exe = str(staged) if staged.is_file() else sys.executable
        return [exe, "--version"]
    brew_exe = _homebrew_linked_executable()
    if brew_exe is not None:
        return [str(brew_exe), "--version"]
    return [sys.executable, "-P", "-m", "pythinker_code", "--version"]


def _homebrew_linked_executable() -> Path | None:
    """The stable brew-linked launcher for a Homebrew install, or None otherwise.

    `brew upgrade` installs the new keg side-by-side and repoints
    ``<prefix>/opt/pythinker-code``; the running interpreter still lives in the
    OLD keg, so smoke-checking ``sys.executable`` would certify the pre-upgrade
    install (and pass with the old version). Returns the opt-linked launcher
    path even if it does not exist — a missing launcher after an upgrade is a
    smoke-check failure, not a reason to fall back to the old binary.
    """
    exe = sys.executable.replace("\\", "/")
    marker = "/cellar/pythinker-code/"
    idx = exe.lower().find(marker)
    if idx < 0:
        return None
    return Path(exe[:idx]) / "opt" / "pythinker-code" / "bin" / "pythinker"


def _finalize_native_staging(*, promote: bool) -> None:
    """After the smoke check, either arm the staged native binary for exit-time
    promotion (it ran) or discard it (it failed). No-op for non-native installs and
    when nothing was staged."""
    if not is_native_build():
        return
    from pythinker_code.ui.shell.update import (
        discard_staged_native_update,
        register_staged_native_promotion,
        staged_native_path,
    )

    if not staged_native_path().is_file():
        return
    if promote:
        register_staged_native_promotion()
    else:
        discard_staged_native_update()


def _smoke_check_cwd() -> Path:
    try:
        return Path(sys.executable).resolve().parent
    except OSError:
        return Path.home()


def _smoke_check_env() -> dict[str, str]:
    env = get_clean_env()
    env["PYTHONSAFEPATH"] = "1"
    env.pop("PYTHONPATH", None)
    return env


_SMOKE_CHECK_ATTEMPTS = 3
_SMOKE_CHECK_RETRY_DELAY_SECONDS = 1.0


async def _run_smoke_check_with_retry(target_version: str | None) -> tuple[bool, str]:
    """Run the post-install smoke check, retrying transient failures.

    Package managers can report success moments before the launcher they manage
    is repointed at the new install (observed with Homebrew's ``opt`` symlink):
    an immediate probe then exercises the OLD binary, reports the old version,
    and records a false ``VERIFICATION_FAILED``. A short retry window absorbs
    that race; a real bad install still fails every attempt. The subprocess
    probe runs off the event loop so a slow/hung binary cannot stall the shell.
    """
    smoke_ok, smoke_message = False, "Smoke check did not run."
    for attempt in range(1, _SMOKE_CHECK_ATTEMPTS + 1):
        smoke_ok, smoke_message = await asyncio.to_thread(
            run_post_install_smoke_check, target_version=target_version
        )
        if smoke_ok or attempt == _SMOKE_CHECK_ATTEMPTS:
            break
        append_update_log(
            f"Smoke check attempt {attempt}/{_SMOKE_CHECK_ATTEMPTS} failed "
            f"({smoke_message}); retrying in {_SMOKE_CHECK_RETRY_DELAY_SECONDS:g}s..."
        )
        await asyncio.sleep(_SMOKE_CHECK_RETRY_DELAY_SECONDS)
    return smoke_ok, smoke_message


def run_post_install_smoke_check(target_version: str | None = None) -> tuple[bool, str]:
    command = _smoke_check_command()
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SMOKE_CHECK_TIMEOUT_SECONDS,
            env=_smoke_check_env(),
            cwd=_smoke_check_cwd(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Smoke check could not run: {exc}"

    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        detail = output.splitlines()[0] if output else f"exit code {result.returncode}"
        return False, f"Smoke check failed: {detail}"
    if not output or not any(ch.isdigit() for ch in output):
        return False, "Smoke check did not report a version."
    first_line = output.splitlines()[0]
    if target_version is not None:
        from pythinker_code.ui.shell.update import semver_tuple

        reported = re.search(r"\d+\.\d+\.\d+", first_line)
        if reported is None:
            return False, f"Smoke check did not report a parseable version: {first_line}"
        if semver_tuple(reported.group(0)) != semver_tuple(target_version):
            # The upgraded binary must identify as the target release; matching
            # the OLD version means the check exercised the pre-upgrade install
            # (or the upgrade silently no-oped).
            return False, (
                f"Smoke check reported {reported.group(0)}, expected {target_version}: {first_line}"
            )
    return True, f"Smoke check passed: {first_line}"


async def prompt_pre_start_update_job() -> None:
    from pythinker_code.ui.shell.update import prompt_pre_start_update

    async def _runner(*, print_output: bool, intent: UpdateIntent) -> UpdateResult:
        return await run_update_job(print_output=print_output, intent=intent, source="startup")

    await prompt_pre_start_update(update_runner=_runner)
