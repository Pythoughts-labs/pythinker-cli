from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import random
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import aiohttp
import keyring
from pydantic import SecretStr

from pythinker_code.auth import PYTHINKER_CODE_PLATFORM_ID
from pythinker_code.auth.platforms import (
    ModelInfo,
    get_platform_by_id,
    list_models,
    managed_model_key,
    managed_provider_key,
)
from pythinker_code.config import (
    Config,
    LLMModel,
    LLMProvider,
    OAuthRef,
    PythinkerAIFetchConfig,
    PythinkerAISearchConfig,
    get_config_file,
    load_config,
    save_config,
)
from pythinker_code.constant import VERSION
from pythinker_code.share import get_share_dir
from pythinker_code.thinking import apply_login_thinking_defaults
from pythinker_code.utils.aiohttp import new_client_session
from pythinker_code.utils.io import FileLockTimeoutError, file_lock
from pythinker_code.utils.logging import logger

if TYPE_CHECKING:
    from pythinker_code.soul.agent import Runtime


PYTHINKER_CODE_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
PYTHINKER_CODE_OAUTH_KEY = "oauth/pythinker-code"
DEFAULT_OAUTH_HOST = "https://auth.pythinker.com"
KEYRING_SERVICE = "pythinker-code"
REFRESH_INTERVAL_SECONDS = 60
MIN_REFRESH_THRESHOLD_SECONDS = 300
REFRESH_THRESHOLD_RATIO = 0.5
UNAUTHORIZED_REFRESH_RETRY_COOLDOWN_SECONDS = 300
_CROSS_PROCESS_LOCK_RETRIES = 5
_PERSISTENCE_LOCK_TIMEOUT_SECONDS = 5.0
_RETRYABLE_REFRESH_STATUSES = {429, 500, 502, 503, 504}
_SAFE_CREDENTIAL_KEY_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_WINDOWS_RESERVED_CREDENTIAL_BASENAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _refresh_threshold(expires_in: float) -> float:
    """Return the dynamic refresh threshold in seconds."""
    if expires_in > 0:
        return max(MIN_REFRESH_THRESHOLD_SECONDS, expires_in * REFRESH_THRESHOLD_RATIO)
    return MIN_REFRESH_THRESHOLD_SECONDS


class OAuthError(RuntimeError):
    """OAuth flow error."""


class OAuthUnauthorized(OAuthError):
    """OAuth credentials rejected."""


class OAuthPersistenceError(OAuthError):
    """OAuth credentials and configuration could not be persisted consistently."""


class _KeyringCredentialPresentError(OAuthPersistenceError):
    """Keyring deletion failed and the credential is confirmed to remain."""


class _KeyringDeleteOutcomeUnknownError(OAuthPersistenceError):
    """Keyring deletion failed and the credential state could not be verified."""


class _RetryableRefreshError(OAuthError):
    """Transient HTTP error during token refresh (5xx / 429)."""


class OAuthDeviceExpired(OAuthError):
    """Device authorization expired."""


OAuthEventKind = Literal["info", "error", "waiting", "verification_url", "success"]


@dataclass(slots=True, frozen=True)
class OAuthEvent:
    type: OAuthEventKind
    message: str
    data: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message

    @property
    def json(self) -> str:
        payload: dict[str, Any] = {"type": self.type, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return json.dumps(payload, ensure_ascii=False)


@dataclass(slots=True)
class OAuthToken:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str
    token_type: str
    expires_in: float = 0.0
    account_id: str | None = None

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> OAuthToken:
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise OAuthError("OAuth token response contained an invalid access token.")

        refresh_token = payload.get("refresh_token", "")
        if not isinstance(refresh_token, str):
            raise OAuthError("OAuth token response contained an invalid refresh token.")

        expires_in_value = payload.get("expires_in")
        if expires_in_value is None or expires_in_value == "":
            expires_in = 0.0
        else:
            if isinstance(expires_in_value, bool) or not isinstance(
                expires_in_value, (int, float, str)
            ):
                raise OAuthError("OAuth token response contained an invalid expiration lifetime.")
            try:
                expires_in = float(expires_in_value)
            except (OverflowError, ValueError):
                raise OAuthError(
                    "OAuth token response contained an invalid expiration lifetime."
                ) from None
            if not math.isfinite(expires_in) or expires_in < 0:
                raise OAuthError("OAuth token response contained an invalid expiration lifetime.")

        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + expires_in,
            scope=str(payload.get("scope") or ""),
            token_type=str(payload.get("token_type") or ""),
            expires_in=expires_in,
            account_id=str(account_id) if (account_id := payload.get("account_id")) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "account_id": self.account_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OAuthToken:
        expires_at_value = payload.get("expires_at")
        return cls(
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at=float(expires_at_value) if expires_at_value is not None else 0.0,
            scope=str(payload.get("scope") or ""),
            token_type=str(payload.get("token_type") or ""),
            expires_in=float(payload.get("expires_in") or 0),
            account_id=str(account_id) if (account_id := payload.get("account_id")) else None,
        )


@dataclass(slots=True)
class _RejectedRefreshState:
    refresh_token: str
    retry_after: float


# Process-wide tombstone for refresh tokens that the server has rejected.
#
# Intentionally module-level rather than per-OAuthManager: OAuth credentials
# are a process-wide resource (all managers in this process share the same
# credentials file), so all managers should see the same "recently rejected"
# state.  Without this, one manager's rejection would leave the persisted
# token visible to the next manager that loads from disk, and we'd re-issue
# the same dead refresh request and re-shadow a configured api_key fallback.
#
# Cross-process sharing is unnecessary — each process discovers the rejection
# independently on its first attempt, and the tombstone auto-clears when the
# on-disk refresh_token differs from the rejected one (i.e. another process
# successfully rotated, or /login atomically rewrote the file).
_REJECTED_REFRESH_TOKENS: dict[str, _RejectedRefreshState] = {}


@dataclass(slots=True)
class DeviceAuthorization:
    user_code: str
    device_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int | None
    interval: int


def _oauth_host() -> str:
    host = os.getenv("PYTHINKER_CODE_OAUTH_HOST") or os.getenv("PYTHINKER_OAUTH_HOST")
    if host is not None and not host.startswith("https://"):
        raise ValueError(f"PYTHINKER_CODE_OAUTH_HOST must use HTTPS, got: {host!r}")
    return host or DEFAULT_OAUTH_HOST


def _device_id_path() -> Path:
    return get_share_dir() / "device_id"


def _ensure_private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.warning("Failed to restrict private file permissions: {error}", error=exc)


def _device_model() -> str:
    system = platform.system()
    arch = platform.machine() or ""
    if system == "Darwin":
        version = platform.mac_ver()[0] or platform.release()
        if version and arch:
            return f"macOS {version} {arch}"
        if version:
            return f"macOS {version}"
        return f"macOS {arch}".strip()
    if system == "Windows":
        release = platform.release()
        if release == "10":
            try:
                build = sys.getwindowsversion().build  # type: ignore[attr-defined]
            except Exception:
                build = None
            if build and build >= 22000:
                release = "11"
        if release and arch:
            return f"Windows {release} {arch}"
        if release:
            return f"Windows {release}"
        return f"Windows {arch}".strip()
    if system:
        version = platform.release()
        if version and arch:
            return f"{system} {version} {arch}"
        if version:
            return f"{system} {version}"
        return f"{system} {arch}".strip()
    return "Unknown"


def _read_device_id(path: Path) -> str:
    """Read an existing device id, retrying briefly while the writer races us.

    O_CREAT|O_EXCL publishes the file empty before its content lands, so a
    loser of the creation race can observe a zero-byte file for a moment.
    """
    for _ in range(20):
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
        time.sleep(0.005)
    return ""


def get_device_id() -> str:
    path = _device_id_path()
    if path.exists() and (existing := _read_device_id(path)):
        return existing
    device_id = uuid.uuid4().hex
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Another process won the race; use its id, do not re-fire telemetry.
        # Fall back to a fresh in-memory id if the file stays empty (e.g. a
        # legacy truncated file) rather than tagging the session with "".
        return _read_device_id(path) or device_id
    try:
        os.write(fd, device_id.encode("utf-8"))
    finally:
        os.close(fd)
    # os.open mode is umask-masked; tighten defensively to 0o600.
    _ensure_private_file(path)
    from pythinker_code.telemetry import track

    track("first_launch")
    return device_id


def _ascii_header_value(value: str, *, fallback: str = "unknown") -> str:
    sanitized = "".join(ch for ch in value if ord(ch) < 128).strip()
    return sanitized or fallback


def _common_headers() -> dict[str, str]:
    device_name = platform.node() or socket.gethostname()
    device_model = _device_model()
    headers = {
        "X-Msh-Platform": "pythinker_code",
        "X-Msh-Version": VERSION,
        "X-Msh-Device-Name": device_name,
        "X-Msh-Device-Model": device_model,
        "X-Msh-Os-Version": platform.version(),
        "X-Msh-Device-Id": get_device_id(),
    }
    return {key: _ascii_header_value(value) for key, value in headers.items()}


def _credentials_dir() -> Path:
    path = get_share_dir() / "credentials"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _is_safe_legacy_credential_key(key: str) -> bool:
    if not _SAFE_CREDENTIAL_KEY_SEGMENT.fullmatch(key) or key.endswith("."):
        return False
    return key.split(".", maxsplit=1)[0].upper() not in _WINDOWS_RESERVED_CREDENTIAL_BASENAMES


def _credential_relative_path(key: str) -> Path:
    relative_key = key.removeprefix("oauth/")
    if (
        key.startswith("oauth/")
        and relative_key == relative_key.lower()
        and _is_safe_legacy_credential_key(relative_key)
    ):
        return Path(relative_key)
    encoded = key.encode(encoding="utf-8").hex()
    return Path("v2") / (encoded.rstrip("=") or "~")


def _credential_path(key: str, suffix: str) -> Path:
    relative_path = _credential_relative_path(key)
    return _credentials_dir() / relative_path.parent / f"{relative_path.name}{suffix}"


def _credentials_path(key: str) -> Path:
    return _credential_path(key, ".json")


def _credentials_lock_path(key: str) -> Path:
    return _credential_path(key, ".lock")


class _CrossProcessLock:
    """File-based lock that coordinates token refresh across pythinker-code processes.

    Uses fcntl.flock on Unix and msvcrt.locking on Windows.
    """

    def __init__(self, key: str) -> None:
        self._path = _credentials_lock_path(key)
        self._fd: int | None = None

    def _acquire(self) -> bool:
        """Acquire the lock.

        Returns ``True`` if locked, ``False`` on contention.
        Raises ``OSError`` if the lock file cannot be opened (permanent failure).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt

                # msvcrt.locking requires bytes to exist at the lock position.
                if os.fstat(self._fd).st_size == 0:
                    os.write(self._fd, b"\0")
                    os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            os.close(self._fd)
            self._fd = None
            return False

    def release(self) -> None:
        if self._fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    with suppress(OSError):
                        os.lseek(self._fd, 0, os.SEEK_SET)
                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            finally:
                with suppress(OSError):
                    os.close(self._fd)
                self._fd = None

    async def acquire_with_retry(self) -> bool:
        for _attempt in range(_CROSS_PROCESS_LOCK_RETRIES):
            try:
                if self._acquire():
                    return True
            except OSError:
                # Cannot open/create the lock file (permissions, read-only FS, etc.).
                # Permanent failure — skip backoff and fail closed.
                return False
            await asyncio.sleep(1 + random.random())
            # After waiting, re-check if the token was refreshed by the holder.
        try:
            return self._acquire()
        except OSError:
            return False

    def acquire_with_retry_sync(self) -> bool:
        """Synchronous counterpart for persistence workers already off the event loop."""
        for _attempt in range(_CROSS_PROCESS_LOCK_RETRIES):
            try:
                if self._acquire():
                    return True
            except OSError:
                return False
            time.sleep(1 + random.random())
        try:
            return self._acquire()
        except OSError:
            return False

    async def __aenter__(self) -> bool:
        return await self.acquire_with_retry()

    async def __aexit__(self, *args: object) -> None:
        self.release()


def _read_keyring_value(key: str) -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICE, key)
    except Exception as exc:
        from pythinker_code.telemetry.errors import report_handled_error

        report_handled_error(exc, site="auth.keyring.read")
        logger.warning(
            "Failed to read OAuth credentials from the system keyring ({error_type}).",
            error_type=type(exc).__name__,
        )
        raise OAuthPersistenceError(
            "Could not read OAuth credentials from the system keyring."
        ) from exc


def _load_from_keyring(key: str) -> OAuthToken | None:
    raw = _read_keyring_value(key)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OAuthPersistenceError("OAuth credentials in the system keyring are invalid.") from exc
    if not isinstance(payload, dict):
        raise OAuthPersistenceError("OAuth credentials in the system keyring are invalid.")
    payload = cast(dict[str, Any], payload)
    try:
        return OAuthToken.from_dict(payload)
    except (OverflowError, TypeError, ValueError) as exc:
        raise OAuthPersistenceError("OAuth credentials in the system keyring are invalid.") from exc


def _report_keyring_delete_error(exc: Exception) -> None:
    from pythinker_code.telemetry.errors import report_handled_error

    report_handled_error(exc, site="auth.keyring.delete")
    logger.warning(
        "Failed to remove OAuth credentials from the system keyring ({error_type}).",
        error_type=type(exc).__name__,
    )


def _delete_from_keyring(key: str) -> None:
    if _read_keyring_value(key) is None:
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, key)
    except Exception as delete_error:
        _report_keyring_delete_error(delete_error)
        try:
            remaining = _read_keyring_value(key)
        except OAuthPersistenceError as verification_error:
            failures = ExceptionGroup(
                "Keyring deletion and verification both failed.",
                [delete_error, verification_error],
            )
            raise _KeyringDeleteOutcomeUnknownError(
                "Could not verify whether OAuth credentials were removed from the system keyring."
            ) from failures
        if remaining is None:
            return
        raise _KeyringCredentialPresentError(
            "Could not remove OAuth credentials from the system keyring."
        ) from delete_error


def _load_from_file(key: str) -> OAuthToken | None:
    path = _credentials_path(key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    payload = cast(dict[str, Any], payload)
    return OAuthToken.from_dict(payload)


def _write_credential_file(key: str, data: bytes) -> None:
    path = _credentials_path(key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        written = os.write(fd, data)
        if written != len(data):
            raise OSError(f"Short write: {written}/{len(data)} bytes")
        os.fsync(fd)
        os.close(fd)
        fd = -1
        try:
            os.chmod(tmp_path, 0o600)
        except OSError as exc:
            logger.warning(
                "Failed to restrict OAuth credential file permissions: {error}", error=exc
            )
        os.replace(tmp_path, path)
    except BaseException:
        if fd >= 0:
            with suppress(OSError):
                os.close(fd)
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def _save_to_file(key: str, token: OAuthToken) -> None:
    data = json.dumps(token.to_dict(), ensure_ascii=False).encode("utf-8")
    _write_credential_file(key, data)


def _delete_from_file(key: str) -> None:
    path = _credentials_path(key)
    if path.exists():
        path.unlink()


def _credential_file_snapshot(key: str) -> bytes | None:
    try:
        return _credentials_path(key).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OAuthPersistenceError("Could not snapshot the OAuth credential file.") from exc


def _restore_credential_file_snapshot(key: str, snapshot: bytes | None) -> None:
    if snapshot is None:
        _delete_from_file(key)
    else:
        _write_credential_file(key, snapshot)


def _migrate_keyring_token(ref: OAuthRef) -> OAuthToken | None:
    file_snapshot = _credential_file_snapshot(ref.key)
    token = _load_from_keyring(ref.key)
    if token is None:
        return _load_from_file(ref.key)

    try:
        _save_to_file(ref.key, token)
    except OSError as exc:
        raise OAuthPersistenceError(
            "Could not copy OAuth credentials out of the system keyring."
        ) from exc
    try:
        _delete_from_keyring(ref.key)
    except _KeyringDeleteOutcomeUnknownError as cleanup_error:
        raise OAuthPersistenceError(
            "OAuth keyring migration cleanup could not be verified; the authoritative file copy "
            "was retained."
        ) from cleanup_error
    except _KeyringCredentialPresentError as cleanup_error:
        try:
            _restore_credential_file_snapshot(ref.key, file_snapshot)
        except OSError as rollback_error:
            failures = ExceptionGroup(
                "Keyring cleanup and credential file rollback both failed.",
                [cleanup_error, rollback_error],
            )
            raise OAuthPersistenceError(
                "OAuth keyring migration cleanup failed and file rollback also failed."
            ) from failures
        raise OAuthPersistenceError(
            "OAuth keyring migration cleanup failed; the file copy was rolled back."
        ) from cleanup_error
    return token


def load_tokens(ref: OAuthRef) -> OAuthToken | None:
    if ref.storage == "keyring":
        return _migrate_keyring_token(ref)
    return _load_from_file(ref.key)


def save_tokens(ref: OAuthRef, token: OAuthToken) -> OAuthRef:
    if ref.storage == "keyring":
        logger.warning("Keyring storage is deprecated; saving OAuth tokens to file.")
        ref = OAuthRef(storage="file", key=ref.key)
    _save_to_file(ref.key, token)
    return ref


def delete_tokens(ref: OAuthRef) -> None:
    if ref.storage == "keyring":
        _delete_from_keyring(ref.key)
    _delete_from_file(ref.key)


def restore_config_state(config: Config, snapshot: Config) -> None:
    """Restore every field of ``config`` in place from a prior deep-copy snapshot."""
    for field_name in type(config).model_fields:
        setattr(config, field_name, getattr(snapshot, field_name))


def _load_transaction_config(config: Config) -> Config:
    config_file = get_config_file()
    if config_file.exists():
        return load_config(config_file)
    return config.model_copy(deep=True)


def _restore_token_snapshot(ref: OAuthRef, token: OAuthToken | None) -> None:
    if token is not None:
        save_tokens(ref, token)
    else:
        delete_tokens(ref)


def _load_token_snapshot(ref: OAuthRef) -> OAuthToken | None:
    if ref.storage == "keyring":
        token = _load_from_keyring(ref.key)
        if token is not None:
            return token
    return _load_from_file(ref.key)


class _PersistenceMutationPhase:
    """Atomic cancellation handshake between the event loop and persistence worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel_requested = False
        self._mutation_started = False

    def begin_mutation(self) -> bool:
        with self._lock:
            if self._cancel_requested:
                return False
            self._mutation_started = True
            return True

    def request_cancel(self) -> bool:
        with self._lock:
            self._cancel_requested = True
            return self._mutation_started


@contextmanager
def _config_transaction_lock() -> Generator[None]:
    try:
        with file_lock(get_config_file(), timeout=_PERSISTENCE_LOCK_TIMEOUT_SECONDS):
            yield
    except FileLockTimeoutError as exc:
        raise OAuthPersistenceError(
            "OAuth persistence is busy; wait for the other operation and try again."
        ) from exc


@contextmanager
def _credential_transaction_locks(keys: list[str]) -> Generator[None]:
    locks: list[_CrossProcessLock] = []
    try:
        for key in sorted(set(keys)):
            try:
                lock = _CrossProcessLock(key)
                acquired = lock.acquire_with_retry_sync()
            except OSError as exc:
                raise OAuthPersistenceError(
                    "Could not acquire the OAuth credential lock; no changes were made."
                ) from exc
            if not acquired:
                raise OAuthPersistenceError(
                    "Could not acquire the OAuth credential lock; no changes were made."
                )
            locks.append(lock)
        yield
    finally:
        for lock in reversed(locks):
            lock.release()


async def _run_persistence_operation(
    operation: Callable[[_PersistenceMutationPhase], None],
) -> None:
    phase = _PersistenceMutationPhase()
    worker = asyncio.create_task(asyncio.to_thread(operation, phase))
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        phase.request_cancel()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        worker_error = None if worker.cancelled() else worker.exception()
        if worker_error is not None:
            logger.warning(
                "OAuth persistence transaction failed while caller cancellation was pending."
            )
            failures = BaseExceptionGroup(
                "OAuth persistence failed while cancellation was pending.",
                [cancellation, worker_error],
            )
            raise OAuthPersistenceError(
                "OAuth persistence failed while cancellation was pending."
            ) from failures
        raise cancellation


def _persist_login_sync(
    config: Config,
    ref: OAuthRef,
    token: OAuthToken,
    apply_config: Callable[[Config], None],
    phase: _PersistenceMutationPhase,
    *,
    replace_provider_key: str | None = None,
) -> None:
    with _config_transaction_lock():
        committed = _load_transaction_config(config)
        config_snapshot = committed.model_copy(deep=True)
        replaced_ref: OAuthRef | None = None
        if replace_provider_key is not None:
            provider = committed.providers.get(replace_provider_key)
            if (
                provider is not None
                and provider.oauth is not None
                and provider.oauth.key != ref.key
            ):
                replaced_ref = provider.oauth

        credential_keys = [ref.key]
        if replaced_ref is not None:
            credential_keys.append(replaced_ref.key)
        with _credential_transaction_locks(credential_keys):
            if not phase.begin_mutation():
                return
            previous_token = load_tokens(ref)
            replaced_token = load_tokens(replaced_ref) if replaced_ref is not None else None
            save_tokens(ref, token)
            try:
                apply_config(committed)
                save_config(committed)
            except BaseException as persistence_error:
                try:
                    _restore_token_snapshot(ref, previous_token)
                except Exception as rollback_error:
                    logger.error(
                        "Failed to roll back OAuth login persistence: {error}",
                        error=rollback_error,
                    )
                    raise OAuthPersistenceError(
                        "OAuth login persistence failed and credential rollback also failed."
                    ) from persistence_error
                raise

            if replaced_ref is not None:
                try:
                    delete_tokens(replaced_ref)
                except Exception as cleanup_error:
                    try:
                        _restore_token_snapshot(replaced_ref, replaced_token)
                    except Exception as rollback_error:
                        logger.error(
                            "Failed to restore replaced OAuth credential after cleanup failure: "
                            "{error}",
                            error=rollback_error,
                        )
                        raise OAuthPersistenceError(
                            "OAuth login credential cleanup failed and replaced credential "
                            "rollback also failed."
                        ) from cleanup_error
                    try:
                        save_config(config_snapshot)
                    except Exception as rollback_error:
                        logger.error(
                            "Failed to restore configuration after OAuth credential cleanup "
                            "failure: {error}",
                            error=rollback_error,
                        )
                        raise OAuthPersistenceError(
                            "OAuth login credential cleanup failed and configuration rollback "
                            "also failed."
                        ) from cleanup_error
                    try:
                        _restore_token_snapshot(ref, previous_token)
                    except Exception as rollback_error:
                        logger.error(
                            "Failed to restore new OAuth credential after cleanup rollback: "
                            "{error}",
                            error=rollback_error,
                        )
                        raise OAuthPersistenceError(
                            "OAuth login credential cleanup failed and new credential rollback "
                            "also failed."
                        ) from cleanup_error
                    raise OAuthPersistenceError(
                        "OAuth login credential cleanup failed; login was rolled back."
                    ) from cleanup_error
            restore_config_state(config, committed)


async def persist_login(
    config: Config,
    ref: OAuthRef,
    token: OAuthToken,
    apply_config: Callable[[Config], None],
    *,
    replace_provider_key: str | None = None,
) -> None:
    """Persist OAuth credentials and config together outside the event loop."""

    def operation(phase: _PersistenceMutationPhase) -> None:
        _persist_login_sync(
            config,
            ref,
            token,
            apply_config,
            phase,
            replace_provider_key=replace_provider_key,
        )

    await _run_persistence_operation(operation)


def _persist_logout_sync(
    config: Config,
    ref: OAuthRef | None,
    remove_config: Callable[[Config], None],
    phase: _PersistenceMutationPhase,
    *,
    provider_key: str | None = None,
) -> None:
    with _config_transaction_lock():
        committed = _load_transaction_config(config)
        snapshot = committed.model_copy(deep=True)
        credential_ref = ref
        if provider_key is not None:
            provider = committed.providers.get(provider_key)
            credential_ref = provider.oauth if provider is not None else None
        credential_keys = [credential_ref.key] if credential_ref is not None else []
        with _credential_transaction_locks(credential_keys):
            previous_token = (
                _load_token_snapshot(credential_ref) if credential_ref is not None else None
            )
            if not phase.begin_mutation():
                return
            remove_config(committed)
            save_config(committed)
            try:
                if credential_ref is not None:
                    delete_tokens(credential_ref)
            except Exception as delete_error:
                try:
                    if credential_ref is not None and previous_token is not None:
                        _restore_token_snapshot(credential_ref, previous_token)
                except Exception as credential_rollback_error:
                    restore_config_state(config, committed)
                    logger.error(
                        "Failed to restore OAuth credential after logout deletion failed "
                        "({error_type}).",
                        error_type=type(credential_rollback_error).__name__,
                    )
                    failures = ExceptionGroup(
                        "OAuth credential deletion and restoration both failed.",
                        [delete_error, credential_rollback_error],
                    )
                    raise OAuthPersistenceError(
                        "OAuth logout partially completed: configuration was removed, but "
                        "credential restoration failed."
                    ) from failures
                try:
                    save_config(snapshot)
                except Exception as rollback_error:
                    logger.error(
                        "Failed to restore configuration after OAuth credential deletion failed: "
                        "{error_type}",
                        error_type=type(rollback_error).__name__,
                    )
                    raise OAuthPersistenceError(
                        "OAuth logout failed and configuration rollback also failed."
                    ) from delete_error
                raise OAuthPersistenceError(
                    "OAuth credential deletion failed; logout was rolled back."
                ) from delete_error
            restore_config_state(config, committed)


async def persist_logout(
    config: Config,
    ref: OAuthRef | None,
    remove_config: Callable[[Config], None],
    *,
    provider_key: str | None = None,
) -> None:
    """Persist OAuth logout outside the event loop, config removal first."""

    def operation(phase: _PersistenceMutationPhase) -> None:
        _persist_logout_sync(
            config,
            ref,
            remove_config,
            phase,
            provider_key=provider_key,
        )

    await _run_persistence_operation(operation)


def _persist_config_change_sync(
    config: Config,
    apply_config: Callable[[Config], None],
    phase: _PersistenceMutationPhase,
    *,
    should_apply: Callable[[Config], bool] | None = None,
) -> None:
    with _config_transaction_lock():
        committed = _load_transaction_config(config)
        if should_apply is not None and not should_apply(committed):
            restore_config_state(config, committed)
            return
        if not phase.begin_mutation():
            return
        apply_config(committed)
        save_config(committed)
        restore_config_state(config, committed)


async def persist_config_change(config: Config, apply_config: Callable[[Config], None]) -> None:
    """Persist a config-only change atomically outside the event loop."""

    def operation(phase: _PersistenceMutationPhase) -> None:
        _persist_config_change_sync(config, apply_config, phase)

    await _run_persistence_operation(operation)


async def request_device_authorization() -> DeviceAuthorization:
    async with (
        new_client_session() as session,
        session.post(
            f"{_oauth_host().rstrip('/')}/api/oauth/device_authorization",
            data={"client_id": PYTHINKER_CODE_CLIENT_ID},
            headers=_common_headers(),
        ) as response,
    ):
        data = await response.json(content_type=None)
        status = response.status
    if status != 200:
        raise OAuthError(f"Device authorization failed: {data}")
    return DeviceAuthorization(
        user_code=str(data["user_code"]),
        device_code=str(data["device_code"]),
        verification_uri=str(data.get("verification_uri") or ""),
        verification_uri_complete=str(data["verification_uri_complete"]),
        expires_in=int(data.get("expires_in") or 0) or None,
        interval=int(data.get("interval") or 5),
    )


async def _request_device_token(auth: DeviceAuthorization) -> tuple[int, dict[str, Any]]:
    try:
        async with (
            new_client_session() as session,
            session.post(
                f"{_oauth_host().rstrip('/')}/api/oauth/token",
                data={
                    "client_id": PYTHINKER_CODE_CLIENT_ID,
                    "device_code": auth.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers=_common_headers(),
            ) as response,
        ):
            data_any: Any = await response.json(content_type=None)
            status = response.status
    except aiohttp.ClientError as exc:
        raise OAuthError("Token polling request failed.") from exc
    if not isinstance(data_any, dict):
        raise OAuthError("Unexpected token polling response.")
    data = cast(dict[str, Any], data_any)
    if status >= 500:
        raise OAuthError(f"Token polling server error: {status}.")
    return status, data


async def refresh_token(refresh_token: str, *, max_retries: int = 3) -> OAuthToken:
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with (
                new_client_session() as session,
                session.post(
                    f"{_oauth_host().rstrip('/')}/api/oauth/token",
                    data={
                        "client_id": PYTHINKER_CODE_CLIENT_ID,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    headers=_common_headers(),
                ) as response,
            ):
                status = response.status
                data: dict[str, Any]
                try:
                    data = await response.json(content_type=None)
                except (json.JSONDecodeError, aiohttp.ContentTypeError):
                    data = {}
            if status in (401, 403):
                raise OAuthUnauthorized(
                    data.get("error_description") or "Token refresh unauthorized."
                )
            if status != 200:
                desc = data.get("error_description") or f"Token refresh failed (HTTP {status})."
                if status in _RETRYABLE_REFRESH_STATUSES:
                    raise _RetryableRefreshError(desc)
                raise OAuthError(desc)
            return OAuthToken.from_response(data)
        except OAuthUnauthorized:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError, _RetryableRefreshError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                await asyncio.sleep(2**attempt)
                logger.warning(
                    "Token refresh attempt {attempt} failed, retrying: {error}",
                    attempt=attempt + 1,
                    error=exc,
                )
    raise OAuthError("Token refresh failed after retries.") from last_exc


def _select_default_model_and_thinking(models: list[ModelInfo]) -> tuple[ModelInfo, bool] | None:
    if not models:
        return None
    selected_model = models[0]
    capabilities = selected_model.capabilities
    thinking = "thinking" in capabilities or "always_thinking" in capabilities
    return selected_model, thinking


def _apply_pythinker_code_config(
    config: Config,
    *,
    models: list[ModelInfo],
    selected_model: ModelInfo,
    thinking: bool,
    oauth_ref: OAuthRef,
) -> None:
    platform = get_platform_by_id(PYTHINKER_CODE_PLATFORM_ID)
    if platform is None:
        raise OAuthError("Pythinker platform not found.")

    provider_key = managed_provider_key(platform.id)
    config.providers[provider_key] = LLMProvider(
        type="pythinker",
        base_url=platform.base_url,
        api_key=SecretStr(""),
        oauth=oauth_ref,
    )

    for key, model in list(config.models.items()):
        if model.provider == provider_key:
            del config.models[key]

    for model_info in models:
        capabilities = model_info.capabilities or None
        config.models[managed_model_key(platform.id, model_info.id)] = LLMModel(
            provider=provider_key,
            model=model_info.id,
            max_context_size=model_info.context_length,
            capabilities=capabilities,
            display_name=model_info.display_name,
        )

    config.default_model = managed_model_key(platform.id, selected_model.id)
    apply_login_thinking_defaults(config, thinking=thinking, effort="high" if thinking else "off")

    if platform.search_url:
        config.services.pythinker_ai_search = PythinkerAISearchConfig(
            base_url=platform.search_url,
            api_key=SecretStr(""),
            oauth=oauth_ref,
        )

    if platform.fetch_url:
        config.services.pythinker_ai_fetch = PythinkerAIFetchConfig(
            base_url=platform.fetch_url,
            api_key=SecretStr(""),
            oauth=oauth_ref,
        )


async def login_pythinker_code(
    config: Config, *, open_browser: bool = True
) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Login requires the default config file; restart without --config/--config-file.",
        )
        return

    platform = get_platform_by_id(PYTHINKER_CODE_PLATFORM_ID)
    if platform is None:
        yield OAuthEvent("error", "Pythinker platform is unavailable.")
        return

    auth: DeviceAuthorization
    token: OAuthToken | None = None
    while True:
        try:
            auth = await request_device_authorization()
        except Exception as exc:
            from pythinker_code.telemetry.errors import report_handled_error

            report_handled_error(exc, site="auth.oauth.device_authorize")
            yield OAuthEvent("error", f"Login failed: {exc}")
            return

        yield OAuthEvent(
            "info",
            "Please visit the following URL to finish authorization.",
        )
        yield OAuthEvent(
            "verification_url",
            f"Verification URL: {auth.verification_uri_complete}",
            data={
                "verification_url": auth.verification_uri_complete,
                "user_code": auth.user_code,
            },
        )
        if open_browser:
            try:
                from pythinker_code.utils.term import open_url_in_browser

                open_url_in_browser(auth.verification_uri_complete)
            except Exception as exc:
                logger.warning("Failed to open browser: {error}", error=exc)

        interval = max(auth.interval, 1)
        printed_wait = False
        try:
            while True:
                status, data = await _request_device_token(auth)
                if status == 200 and "access_token" in data:
                    token = OAuthToken.from_response(data)
                    if not token.refresh_token:
                        # Fail loud at login: a token saved without a refresh_token
                        # can never refresh, deferring the failure to first expiry
                        # with a far more confusing unauthorized error.
                        raise OAuthError("Token response missing refresh_token.")
                    break
                error_code = str(data.get("error") or "unknown_error")
                if error_code == "expired_token":
                    raise OAuthDeviceExpired("Device code expired.")
                error_description = str(data.get("error_description") or "")
                if not printed_wait:
                    yield OAuthEvent(
                        "waiting",
                        f"Waiting for user authorization...: {error_description.strip()}",
                        data={
                            "error": error_code,
                            "error_description": error_description,
                        },
                    )
                    printed_wait = True
                await asyncio.sleep(interval)
        except OAuthDeviceExpired:
            yield OAuthEvent("info", "Device code expired, restarting login...")
            continue
        except Exception as exc:
            from pythinker_code.telemetry.errors import report_handled_error

            report_handled_error(exc, site="auth.oauth.device_poll")
            yield OAuthEvent("error", f"Login failed: {exc}")
            return
        break

    assert token is not None

    oauth_ref = OAuthRef(storage="file", key=PYTHINKER_CODE_OAUTH_KEY)
    oauth_ref = save_tokens(oauth_ref, token)

    try:
        models = await list_models(platform, token.access_token)
    except Exception as exc:
        from pythinker_code.telemetry.errors import report_handled_error

        report_handled_error(exc, site="auth.models.fetch")
        logger.error("Failed to get models: {error}", error=exc)
        yield OAuthEvent("error", f"Failed to get models: {exc}")
        return

    if not models:
        yield OAuthEvent("error", "No models available for the selected platform.")
        return

    selection = _select_default_model_and_thinking(models)
    if selection is None:
        return
    selected_model, thinking = selection

    _apply_pythinker_code_config(
        config,
        models=models,
        selected_model=selected_model,
        thinking=thinking,
        oauth_ref=oauth_ref,
    )
    save_config(config)
    yield OAuthEvent("success", "Logged in successfully.")
    return


async def logout_pythinker_code(config: Config) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Logout requires the default config file; restart without --config/--config-file.",
        )
        return

    delete_tokens(OAuthRef(storage="keyring", key=PYTHINKER_CODE_OAUTH_KEY))
    delete_tokens(OAuthRef(storage="file", key=PYTHINKER_CODE_OAUTH_KEY))

    provider_key = managed_provider_key(PYTHINKER_CODE_PLATFORM_ID)
    if provider_key in config.providers:
        del config.providers[provider_key]

    removed_default = False
    for key, model in list(config.models.items()):
        if model.provider != provider_key:
            continue
        del config.models[key]
        if config.default_model == key:
            removed_default = True

    if removed_default:
        config.default_model = ""

    config.services.pythinker_ai_search = None
    config.services.pythinker_ai_fetch = None

    save_config(config)
    yield OAuthEvent("success", "Logged out successfully.")
    return


class OAuthManager:
    def __init__(self, config: Config) -> None:
        self._config = config
        # Cache access tokens only; refresh tokens are always read from persisted storage.
        self._access_tokens: dict[str, str] = {}
        self._refresh_lock = asyncio.Lock()
        self._migrate_oauth_storage()
        self._load_initial_tokens()

    def _iter_oauth_refs(self) -> list[OAuthRef]:
        refs: list[OAuthRef] = []
        for provider in self._config.providers.values():
            if provider.oauth:
                refs.append(provider.oauth)
        for service in (
            self._config.services.pythinker_ai_search,
            self._config.services.pythinker_ai_fetch,
        ):
            if service and service.oauth:
                refs.append(service.oauth)
        return refs

    def _migrate_oauth_storage(self) -> None:
        def has_keyring_refs(config: Config) -> bool:
            if any(
                provider.oauth and provider.oauth.storage == "keyring"
                for provider in config.providers.values()
            ):
                return True
            return any(
                service and service.oauth and service.oauth.storage == "keyring"
                for service in (
                    config.services.pythinker_ai_search,
                    config.services.pythinker_ai_fetch,
                )
            )

        def migrate_config(config: Config) -> None:
            migration_results: dict[str, bool] = {}

            def migrate_ref(ref: OAuthRef) -> OAuthRef:
                if ref.storage != "keyring":
                    return ref
                if ref.key not in migration_results:
                    migration_results[ref.key] = load_tokens(ref) is not None
                if not migration_results[ref.key]:
                    return ref
                return OAuthRef(storage="file", key=ref.key)

            refs = [
                provider.oauth
                for provider in config.providers.values()
                if provider.oauth and provider.oauth.storage == "keyring"
            ]
            refs.extend(
                service.oauth
                for service in (
                    config.services.pythinker_ai_search,
                    config.services.pythinker_ai_fetch,
                )
                if service and service.oauth and service.oauth.storage == "keyring"
            )
            with _credential_transaction_locks([ref.key for ref in refs]):
                for provider in config.providers.values():
                    if provider.oauth:
                        provider.oauth = migrate_ref(provider.oauth)

                for service in (
                    config.services.pythinker_ai_search,
                    config.services.pythinker_ai_fetch,
                ):
                    if service and service.oauth:
                        service.oauth = migrate_ref(service.oauth)

        if self._config.is_from_default_location:
            _persist_config_change_sync(
                self._config,
                migrate_config,
                _PersistenceMutationPhase(),
                should_apply=has_keyring_refs,
            )
        elif has_keyring_refs(self._config):
            migrate_config(self._config)

    def _load_initial_tokens(self) -> None:
        for ref in self._iter_oauth_refs():
            token = load_tokens(ref)
            if token and not self._should_suppress_persisted_token(ref, token):
                self._cache_access_token(ref, token)

    def _rejected_refresh_state(
        self, ref: OAuthRef, refresh_token: str | None
    ) -> _RejectedRefreshState | None:
        if not refresh_token:
            return None
        state = _REJECTED_REFRESH_TOKENS.get(ref.key)
        if state and state.refresh_token != refresh_token:
            _REJECTED_REFRESH_TOKENS.pop(ref.key, None)
            return None
        return state

    def _should_suppress_persisted_token(self, ref: OAuthRef, token: OAuthToken) -> bool:
        return self._rejected_refresh_state(ref, token.refresh_token) is not None

    def _can_retry_rejected_refresh_token(self, ref: OAuthRef, refresh_token: str | None) -> bool:
        state = self._rejected_refresh_state(ref, refresh_token)
        return state is None or time.time() >= state.retry_after

    def _mark_refresh_token_rejected(self, ref: OAuthRef, refresh_token: str) -> None:
        if not refresh_token:
            return
        _REJECTED_REFRESH_TOKENS[ref.key] = _RejectedRefreshState(
            refresh_token=refresh_token,
            retry_after=time.time() + UNAUTHORIZED_REFRESH_RETRY_COOLDOWN_SECONDS,
        )

    def _clear_rejected_refresh_token(self, ref: OAuthRef) -> None:
        _REJECTED_REFRESH_TOKENS.pop(ref.key, None)

    def _cache_access_token(self, ref: OAuthRef, token: OAuthToken) -> None:
        if not token.access_token:
            self._access_tokens.pop(ref.key, None)
            return
        self._access_tokens[ref.key] = token.access_token

    def get_cached_access_token(self, key: str) -> str | None:
        """Get a cached access token by key, or None if not available."""
        return self._access_tokens.get(key)

    def common_headers(self) -> dict[str, str]:
        return _common_headers()

    def resolve_api_key(self, api_key: SecretStr, oauth: OAuthRef | None) -> str:
        if oauth:
            token = self._access_tokens.get(oauth.key)
            if token is None:
                persisted = load_tokens(oauth)
                if persisted and not self._should_suppress_persisted_token(oauth, persisted):
                    self._cache_access_token(oauth, persisted)
                    token = self._access_tokens.get(oauth.key)
            if token:
                return token
            logger.warning(
                "OAuth ref present (key={key}) but no access token resolved; "
                "falling back to configured api_key",
                key=oauth.key,
            )
        return api_key.get_secret_value()

    def get_chatgpt_account_id(self, oauth: OAuthRef | None) -> str | None:
        """Return the ChatGPT account_id persisted alongside an OAuth token.

        Used by the `/usage` adapter for ChatGPT Codex which requires the
        `ChatGPT-Account-Id` header in addition to the bearer token.
        """
        if oauth is None:
            return None
        persisted = load_tokens(oauth)
        if persisted is None:
            return None
        account_id = getattr(persisted, "account_id", None)
        return str(account_id) if account_id else None

    def _pythinker_code_ref(self) -> OAuthRef | None:
        provider_key = managed_provider_key(PYTHINKER_CODE_PLATFORM_ID)
        provider = self._config.providers.get(provider_key)
        if provider and provider.oauth:
            return provider.oauth
        for service in (
            self._config.services.pythinker_ai_search,
            self._config.services.pythinker_ai_fetch,
        ):
            if service and service.oauth and service.oauth.key == PYTHINKER_CODE_OAUTH_KEY:
                return service.oauth
        return None

    async def ensure_fresh(
        self,
        runtime: Runtime | None = None,
        *,
        force: bool = False,
        oauth_ref: OAuthRef | None = None,
    ) -> None:
        """Load persisted tokens, cache them, and refresh if close to expiry.

        Args:
            runtime: When provided the live LLM client's API key is updated
                in-place.  Pass ``None`` for lightweight callers (e.g. title
                generation) that only need the internal cache to be current.
            force: When True, skip the expiry-threshold check and always
                attempt a refresh.  Used after receiving a 401 from the server.
            oauth_ref: When provided, refresh only this OAuth credential.
        """
        refs = [oauth_ref] if oauth_ref is not None else self._iter_oauth_refs()
        if oauth_ref is None and runtime is not None:
            llm = runtime.llm
            if llm is not None and llm.model_config is not None:
                provider = runtime.config.providers.get(llm.model_config.provider)
                refs = [provider.oauth] if provider is not None and provider.oauth else []

        for ref in refs:
            token = load_tokens(ref)
            if token is None:
                continue
            if self._should_suppress_persisted_token(ref, token):
                self._access_tokens.pop(ref.key, None)
                self._apply_access_token(runtime, ref, "")
                if not self._can_retry_rejected_refresh_token(ref, token.refresh_token):
                    if force:
                        raise OAuthUnauthorized("Refresh token was recently rejected.")
                    continue
            else:
                self._cache_access_token(ref, token)
                if token.access_token:
                    self._apply_access_token(runtime, ref, token.access_token)
            await self._refresh_tokens(ref, token, runtime, force=force)

    @asynccontextmanager
    async def refreshing(self, runtime: Runtime) -> AsyncGenerator[None]:
        stop_event = asyncio.Event()

        async def _runner() -> None:
            try:
                while True:
                    wall_before = time.time()
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=REFRESH_INTERVAL_SECONDS,
                        )
                        return
                    except TimeoutError:
                        pass
                    elapsed = time.time() - wall_before
                    force = elapsed > REFRESH_INTERVAL_SECONDS * 2
                    if force:
                        logger.info(
                            "Detected possible sleep/wake ({elapsed:.0f}s elapsed), "
                            "forcing token refresh.",
                            elapsed=elapsed,
                        )
                    try:
                        await self.ensure_fresh(runtime, force=force)
                    except Exception as exc:
                        from pythinker_code.telemetry.errors import report_handled_error

                        report_handled_error(exc, site="auth.oauth.refresh.background")
                        logger.warning(
                            "Failed to refresh OAuth token in background: {error}",
                            error=exc,
                        )
            except asyncio.CancelledError:
                pass

        await self.ensure_fresh(runtime)
        refresh_task = asyncio.create_task(_runner())
        try:
            yield
        finally:
            stop_event.set()
            refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await refresh_task

    async def _refresh_tokens(
        self,
        ref: OAuthRef,
        token: OAuthToken,
        runtime: Runtime | None,
        *,
        force: bool = False,
    ) -> None:
        # Always prefer persisted tokens before refresh to avoid stale cache
        # when multiple sessions might have already rotated the refresh token.
        persisted = load_tokens(ref)
        if persisted and not self._should_suppress_persisted_token(ref, persisted):
            self._cache_access_token(ref, persisted)
        current_token = persisted or token
        if not current_token.refresh_token:
            return
        async with self._refresh_lock:
            # Re-check persisted token inside the in-process lock.
            persisted = load_tokens(ref)
            if persisted and not self._should_suppress_persisted_token(ref, persisted):
                self._cache_access_token(ref, persisted)
            current = persisted or current_token
            if not force:
                now = time.time()
                if (
                    current.expires_at
                    and current.expires_at > now
                    and current.expires_at - now >= _refresh_threshold(current.expires_in)
                ):
                    return
            refresh_token_value = current.refresh_token
            if not refresh_token_value:
                return
            if self._should_suppress_persisted_token(
                ref, current
            ) and not self._can_retry_rejected_refresh_token(ref, refresh_token_value):
                self._access_tokens.pop(ref.key, None)
                self._apply_access_token(runtime, ref, "")
                if force:
                    raise OAuthUnauthorized("Refresh token was recently rejected.")
                return

            # Acquire cross-process file lock to coordinate with other
            # pythinker-code instances (terminal, VS Code, web).
            xlock = _CrossProcessLock(ref.key)
            acquired = await xlock.acquire_with_retry()
            if not acquired:
                logger.warning("Could not acquire cross-process lock for token refresh")
                if force:
                    raise OAuthPersistenceError(
                        "Could not acquire the OAuth credential lock for token refresh."
                    )
                return
            try:
                # Triple-check after acquiring the lock — another process
                # may have refreshed while we waited.
                locked_token = load_tokens(ref)
                if locked_token and locked_token.refresh_token != refresh_token_value:
                    self._clear_rejected_refresh_token(ref)
                    self._cache_access_token(ref, locked_token)
                    self._apply_access_token(runtime, ref, locked_token.access_token)
                    return
                if not force and locked_token:
                    remaining = locked_token.expires_at - time.time()
                    if locked_token.expires_at and remaining >= _refresh_threshold(
                        locked_token.expires_in
                    ):
                        self._clear_rejected_refresh_token(ref)
                        self._cache_access_token(ref, locked_token)
                        self._apply_access_token(runtime, ref, locked_token.access_token)
                        return

                try:
                    refreshed = await self._refresh_token_for_ref(ref, refresh_token_value)
                except OAuthUnauthorized as exc:
                    # Give a concurrent instance time to persist its rotated token.
                    await asyncio.sleep(1)
                    latest = load_tokens(ref)
                    if latest and latest.refresh_token != refresh_token_value:
                        self._clear_rejected_refresh_token(ref)
                        self._cache_access_token(ref, latest)
                        self._apply_access_token(runtime, ref, latest.access_token)
                        return
                    # delete_tokens(ref) would remove whatever the ref points
                    # to on disk right now, not "the refresh_token that just
                    # got 401".  A concurrent OAuthManager (another process,
                    # or another manager in this process — app.py:199,
                    # web/api/sessions.py:817, plugin paths) may have
                    # legitimately rotated and written a valid new token into
                    # this file between the load_tokens check above and here,
                    # and we'd wipe it.  Clearing the in-memory cache is
                    # enough; a short in-process tombstone prevents the same
                    # rejected refresh_token from being immediately retried or
                    # preferred over a configured static api_key fallback, and
                    # /login still atomically overwrites the file.
                    self._mark_refresh_token_rejected(ref, refresh_token_value)
                    self._access_tokens.pop(ref.key, None)
                    self._apply_access_token(runtime, ref, "")
                    if force:
                        raise
                    logger.warning(
                        "OAuth credentials rejected: {error}",
                        error=exc,
                    )
                    from pythinker_code.telemetry import track

                    track("oauth_refresh", success=False, reason="unauthorized")
                    return
                except Exception as exc:
                    if force:
                        raise
                    from pythinker_code.telemetry.errors import report_handled_error

                    report_handled_error(exc, site="auth.oauth.refresh")
                    logger.warning("Failed to refresh OAuth token: {error}", error=exc)
                    from pythinker_code.telemetry import track

                    track("oauth_refresh", success=False, reason="network_or_other")
                    return
                if refreshed.account_id is None:
                    refreshed.account_id = current.account_id
                if not refreshed.refresh_token:
                    refreshed.refresh_token = current.refresh_token
                if refreshed.expires_in <= 0 and current.expires_in > 0:
                    # A refresh response omitting expires_in would otherwise stamp
                    # expires_at == now, making the freshness gate refresh again on
                    # every tick; carry the previous lifetime forward instead.
                    refreshed.expires_in = current.expires_in
                    refreshed.expires_at = time.time() + current.expires_in
                self._clear_rejected_refresh_token(ref)
                save_tokens(ref, refreshed)
                self._cache_access_token(ref, refreshed)
                self._apply_access_token(runtime, ref, refreshed.access_token)
                from pythinker_code.telemetry import track

                track("oauth_refresh", success=True)
            finally:
                xlock.release()

    async def _refresh_token_for_ref(self, ref: OAuthRef, refresh_token_value: str) -> OAuthToken:
        from pythinker_code.auth.copilot import (
            GITHUB_COPILOT_OAUTH_KEY,
            refresh_copilot_token,
        )

        if ref.key == GITHUB_COPILOT_OAUTH_KEY:
            return await refresh_copilot_token(refresh_token_value)
        if ref.key == "oauth/openai-chatgpt":
            from pythinker_code.auth.openai import refresh_openai_chatgpt_token

            return await refresh_openai_chatgpt_token(refresh_token_value)
        if ref.key == "oauth/xai":
            from pythinker_code.auth.xai import refresh_xai_token

            return await refresh_xai_token(refresh_token_value)
        if ref.key.startswith("oauth/snowflake-cortex/"):
            from pythinker_code.auth.snowflake import refresh_snowflake_cortex_token

            account = ref.key.removeprefix("oauth/snowflake-cortex/")
            return await refresh_snowflake_cortex_token(account, refresh_token_value)
        return await refresh_token(refresh_token_value)

    def _apply_access_token(
        self, runtime: Runtime | None, ref: OAuthRef, access_token: str
    ) -> None:
        if runtime is None:
            return
        if runtime.llm is None or runtime.llm.model_config is None:
            return
        provider_key = runtime.llm.model_config.provider
        provider = runtime.config.providers.get(provider_key)
        if provider is None or provider.oauth != ref:
            return
        fallback_api_key = provider.api_key.get_secret_value()
        replacement = access_token or fallback_api_key
        chat_provider = cast(Any, runtime.llm.chat_provider)
        for client_attr in ("client", "_client"):
            client = getattr(chat_provider, client_attr, None)
            if client is not None and hasattr(client, "api_key"):
                client.api_key = replacement
                return


if __name__ == "__main__":
    from rich import print

    print(_common_headers())
