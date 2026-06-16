"""JSON-RPC LSP client over Host stdio."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from inspect import iscoroutine
from typing import Any

from pythinker_host import Host, HostProcess

from pythinker_code.lsp.framing import (
    LspProtocolError,
    LspServerDown,
    LspStartError,
    read_message,
    write_message,
)
from pythinker_code.lsp.protocol import InitializeParams, InitializeResult, ServerCapabilities

NotificationHandler = Callable[[Any], None] | Callable[[Any], Awaitable[None]]
RequestHandler = Callable[[Any], Any] | Callable[[Any], Awaitable[Any]]

_SHUTDOWN_TIMEOUT_S = 2.0


class LspClient:
    """Minimal LSP client: spawn via Host.exec, JSON-RPC over Content-Length framing."""

    def __init__(self, host: Host, *, logger: logging.Logger | None = None) -> None:
        self._host = host
        self._logger = logger or logging.getLogger(__name__)
        self._proc: HostProcess | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._read_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._stopped = False
        self._capabilities: ServerCapabilities | None = None
        self._initialized = False
        self._notification_handlers: dict[str, list[NotificationHandler]] = {}
        self._request_handlers: dict[str, RequestHandler] = {}
        self._on_crash: Callable[[Exception], None] | None = None

    @property
    def capabilities(self) -> ServerCapabilities | None:
        return self._capabilities

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def start(
        self,
        command: str,
        args: list[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        if self._proc is not None:
            raise LspStartError("LSP client already started")
        try:
            exec_env: Mapping[str, str] | None = None
            if env is not None:
                exec_env = {**os.environ, **env}
            self._proc = await self._host.exec(command, *args, env=exec_env, cwd=cwd)
        except FileNotFoundError as exc:
            raise LspStartError(f"command not found: {command}") from exc
        except OSError as exc:
            raise LspStartError(str(exc)) from exc

        self._stopped = False
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._read_task = asyncio.create_task(self._read_loop())

    async def initialize(self, params: InitializeParams) -> InitializeResult:
        result = await self.send_request(
            "initialize",
            params.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        init_result = InitializeResult.model_validate(result)
        self._capabilities = init_result.capabilities
        await self.send_notification("initialized", {})
        self._initialized = True
        return init_result

    async def send_request(self, method: str, params: Any) -> Any:
        proc = self._require_running()
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            async with self._write_lock:
                await write_message(proc.stdin, message)
        except Exception as exc:
            self._pending.pop(request_id, None)
            if not future.done():
                future.set_exception(LspServerDown(str(exc)))
            raise

        try:
            return await future
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise

    async def send_notification(self, method: str, params: Any) -> None:
        proc = self._require_running()
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        async with self._write_lock:
            await write_message(proc.stdin, message)

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        self._notification_handlers.setdefault(method, []).append(handler)

    def on_request(self, method: str, handler: RequestHandler) -> None:
        self._request_handlers[method] = handler

    def on_crash(self, handler: Callable[[Exception], None]) -> None:
        """Register a callback fired when the server exits unexpectedly (not on stop())."""
        self._on_crash = handler

    async def stop(self) -> None:
        if self._stopped:
            return

        proc = self._proc
        if proc is not None and proc.returncode is None:
            # Bound the graceful handshake: a hung server must not block teardown.
            # On timeout (or any error) we fall through to killing the process below.
            with suppress(Exception):
                await asyncio.wait_for(
                    self.send_request("shutdown", None), timeout=_SHUTDOWN_TIMEOUT_S
                )
            with suppress(Exception):
                await self.send_notification("exit", None)

        self._mark_stopped()

        if self._read_task is not None:
            self._read_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._read_task
            self._read_task = None

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None

        if proc is not None and proc.returncode is None:
            with suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(proc.wait(), timeout=0.5)
            if proc.returncode is None:
                with suppress(Exception):
                    await proc.kill()

        self._fail_all_pending(LspServerDown("LSP client stopped"))
        self._proc = None

    def _require_running(self) -> HostProcess:
        if self._proc is None or self._stopped:
            raise LspServerDown("LSP client is not running")
        if self._proc.returncode is not None:
            raise LspServerDown(f"LSP server exited with code {self._proc.returncode}")
        return self._proc

    async def _read_loop(self) -> None:
        try:
            while not self._stopped:
                proc = self._proc
                if proc is None:
                    break
                if proc.returncode is not None:
                    self._handle_server_down(
                        LspServerDown(f"LSP server exited with code {proc.returncode}")
                    )
                    break
                try:
                    message = await read_message(proc.stdout)
                except LspServerDown as exc:
                    self._handle_server_down(exc)
                    break
                except LspProtocolError as exc:
                    self._logger.warning("LSP protocol error: %s", exc)
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.debug("LSP read loop failed", exc_info=True)
            self._handle_server_down(LspServerDown(str(exc)))
        finally:
            proc = self._proc
            if proc is not None and proc.returncode is not None:
                self._handle_server_down(
                    LspServerDown(f"LSP server exited with code {proc.returncode}")
                )

    def _handle_server_down(self, exc: LspServerDown) -> None:
        # Distinguishes an unexpected server exit from an intentional stop():
        # stop() sets ``_stopped`` before cancelling the read loop, so a second
        # call here (e.g. from the finally block) is a no-op and on_crash never
        # fires for a clean shutdown.
        if self._stopped:
            return
        if self._on_crash is not None:
            with suppress(Exception):
                self._on_crash(exc)
        self._fail_all_pending(exc)
        self._mark_stopped()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message:
            if "id" in message:
                await self._handle_server_request(message)
            else:
                await self._handle_server_notification(message)
            return

        request_id = message.get("id")
        if request_id is None:
            return

        future = self._pending.pop(request_id, None)
        if future is None:
            self._logger.debug("unexpected LSP response id=%s", request_id)
            return

        if "error" in message:
            error = message["error"]
            message_text = error.get("message", "LSP request failed")
            raw_code = error.get("code")
            code = raw_code if isinstance(raw_code, int) else None
            future.set_exception(LspProtocolError(message_text, code=code))
            return

        future.set_result(message.get("result"))

    async def _handle_server_notification(self, message: dict[str, Any]) -> None:
        method = message["method"]
        params = message.get("params")
        for handler in self._notification_handlers.get(method, ()):
            try:
                result = handler(params)
                if iscoroutine(result):
                    await result
            except Exception:
                self._logger.debug("LSP notification handler failed for %s", method, exc_info=True)

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None:
            return

        method = message["method"]
        request_id = message["id"]
        params = message.get("params")
        handler = self._request_handlers.get(method)
        if handler is None:
            response: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        else:
            try:
                result = handler(params)
                if iscoroutine(result):
                    result = await result
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            except Exception as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": str(exc)},
                }

        async with self._write_lock:
            await write_message(proc.stdin, response)

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            while proc.returncode is None:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._logger.debug("lsp stderr: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.debug("LSP stderr drain failed", exc_info=True)

    def _mark_stopped(self) -> None:
        self._stopped = True
        self._initialized = False
        self._capabilities = None

    def _fail_all_pending(self, exc: BaseException) -> None:
        pending = self._pending
        self._pending = {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)
