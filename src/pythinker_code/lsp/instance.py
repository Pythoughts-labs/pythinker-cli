"""Single LSP server lifecycle wrapper."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pythinker_host import Host

from pythinker_code.config import LspServerConfig
from pythinker_code.lsp.client import LspClient
from pythinker_code.lsp.framing import LspProtocolError
from pythinker_code.lsp.protocol import InitializeParams

LSP_ERROR_CONTENT_MODIFIED = -32801
MAX_RETRIES_FOR_TRANSIENT_ERRORS = 3
RETRY_BASE_DELAY_S = 0.5

NotificationHandler = Callable[[Any], None] | Callable[[Any], Awaitable[None]]
RequestHandler = Callable[[Any], Any] | Callable[[Any], Awaitable[Any]]


class LspState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


class LspServerInstance:
    """Manages one language-server process and its initialize handshake."""

    def __init__(
        self,
        name: str,
        config: LspServerConfig,
        host: Host,
        *,
        workspace_folder: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self._host = host
        self._workspace_folder = workspace_folder
        self._logger = logger or logging.getLogger(__name__)
        self._client = LspClient(host, logger=self._logger)
        self._client.on_crash(self._handle_client_crash)
        self._state = LspState.STOPPED
        self.start_time: datetime | None = None
        self.last_error: Exception | None = None
        self.restart_count = 0
        self._crash_recovery_count = 0

    @property
    def state(self) -> LspState:
        return self._state

    def is_healthy(self) -> bool:
        return self._state == LspState.RUNNING and self._client.is_initialized

    async def start(self) -> None:
        if self._state in (LspState.RUNNING, LspState.STARTING):
            return

        max_restarts = self.config.max_restarts
        if self._state == LspState.ERROR and self._crash_recovery_count > max_restarts:
            error = RuntimeError(
                f"LSP server '{self.name}' exceeded max crash recovery attempts ({max_restarts})"
            )
            self.last_error = error
            raise error

        try:
            self._state = LspState.STARTING
            await self._client.start(
                self.config.command,
                self.config.args,
                env=self.config.env or None,
                cwd=self._workspace_folder,
            )
            init_params = _build_initialize_params(self.config, self._workspace_folder)
            init_coro = self._client.initialize(init_params)
            await asyncio.wait_for(init_coro, timeout=self.config.startup_timeout)
            self._state = LspState.RUNNING
            self.start_time = datetime.now(tz=UTC)
            self._crash_recovery_count = 0
        except Exception as exc:
            await self._client.stop()
            self._state = LspState.ERROR
            self.last_error = exc
            if isinstance(exc, asyncio.TimeoutError):
                raise TimeoutError(
                    f"LSP server '{self.name}' timed out after {self.config.startup_timeout}s "
                    "during initialization"
                ) from exc
            raise

    async def stop(self) -> None:
        if self._state == LspState.STOPPED:
            return
        try:
            await self._client.stop()
            self._state = LspState.STOPPED
        except Exception as exc:
            self._state = LspState.ERROR
            self.last_error = exc
            raise

    async def restart(self) -> None:
        try:
            await self.stop()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to stop LSP server '{self.name}' during restart: {exc}"
            ) from exc

        self.restart_count += 1
        max_restarts = self.config.max_restarts
        if self.restart_count > max_restarts:
            error = RuntimeError(
                f"Max restart attempts ({max_restarts}) exceeded for server '{self.name}'"
            )
            self.last_error = error
            self._state = LspState.ERROR
            raise error

        await self.start()

    async def send_request(self, method: str, params: Any) -> Any:
        if not self.is_healthy():
            raise RuntimeError(
                f"Cannot send request to LSP server '{self.name}': server is {self._state}"
                + (f", last error: {self.last_error}" if self.last_error else "")
            )

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES_FOR_TRANSIENT_ERRORS + 1):
            try:
                return await self._client.send_request(method, params)
            except LspProtocolError as exc:
                last_error = exc
                if (
                    exc.code == LSP_ERROR_CONTENT_MODIFIED
                    and attempt < MAX_RETRIES_FOR_TRANSIENT_ERRORS
                ):
                    delay = RETRY_BASE_DELAY_S * (2**attempt)
                    self._logger.debug(
                        "LSP request %r to %r got ContentModified, retrying in %.1fs",
                        method,
                        self.name,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                break

        raise RuntimeError(
            f"LSP request '{method}' failed for server '{self.name}': {last_error}"
        ) from last_error

    async def send_notification(self, method: str, params: Any) -> None:
        if not self.is_healthy():
            raise RuntimeError(
                f"Cannot send notification to LSP server '{self.name}': server is {self._state}"
            )
        try:
            await self._client.send_notification(method, params)
        except Exception as exc:
            raise RuntimeError(
                f"LSP notification '{method}' failed for server '{self.name}': {exc}"
            ) from exc

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        self._client.on_notification(method, handler)

    def on_request(self, method: str, handler: RequestHandler) -> None:
        self._client.on_request(method, handler)

    def mark_crashed(self, error: Exception | None = None) -> None:
        self._state = LspState.ERROR
        self._crash_recovery_count += 1
        if error is not None:
            self.last_error = error

    def _handle_client_crash(self, error: Exception) -> None:
        # Fired from the client read loop when the process exits unexpectedly.
        # Parks the instance in ERROR so the crash cap is enforced on the next
        # ensure_started()/start() and is_healthy() stops reporting RUNNING.
        self.mark_crashed(error)
        self._logger.warning("LSP server %r crashed: %s", self.name, error)


def _build_initialize_params(config: LspServerConfig, workspace_folder: str) -> InitializeParams:
    workspace_path = Path(workspace_folder).resolve()
    workspace_uri = workspace_path.as_uri()
    return InitializeParams(
        processId=os.getpid(),
        initializationOptions=config.initialization_options
        if config.initialization_options is not None
        else {},
        workspaceFolders=[
            {
                "uri": workspace_uri,
                "name": workspace_path.name,
            }
        ],
        rootPath=str(workspace_path),
        rootUri=workspace_uri,
        capabilities={
            "workspace": {
                "configuration": False,
                "workspaceFolders": False,
            },
            "textDocument": {
                "synchronization": {
                    "dynamicRegistration": False,
                    "willSave": False,
                    "willSaveWaitUntil": False,
                    "didSave": True,
                },
                "publishDiagnostics": {
                    "relatedInformation": True,
                    "tagSupport": {"valueSet": [1, 2]},
                    "versionSupport": False,
                    "codeDescriptionSupport": True,
                    "dataSupport": False,
                },
                "hover": {
                    "dynamicRegistration": False,
                    "contentFormat": ["markdown", "plaintext"],
                },
                "definition": {
                    "dynamicRegistration": False,
                    "linkSupport": True,
                },
                "references": {"dynamicRegistration": False},
                "documentSymbol": {
                    "dynamicRegistration": False,
                    "hierarchicalDocumentSymbolSupport": True,
                },
                "callHierarchy": {"dynamicRegistration": False},
            },
            "general": {"positionEncodings": ["utf-16"]},
        },
    )
