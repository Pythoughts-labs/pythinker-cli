"""Session-scoped LSP facade with lazy async initialization."""

from __future__ import annotations

import asyncio
import contextlib
from enum import StrEnum
from typing import TYPE_CHECKING

from pythinker_host import get_current_host

from pythinker_code.lsp.diagnostics import DiagnosticRegistry, wire_publish_diagnostics_handlers
from pythinker_code.lsp.instance import LspState
from pythinker_code.lsp.manager import LspServerManager
from pythinker_code.utils.logging import logger

if TYPE_CHECKING:
    from pythinker_code.config import LspServerConfig
    from pythinker_code.soul.agent import Runtime


class LspInitStatus(StrEnum):
    NOT_STARTED = "not_started"
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class LspService:
    """Owns one LspServerManager per agent session."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        servers: dict[str, LspServerConfig] | None = None,
    ) -> None:
        self._runtime = runtime
        self._servers = servers or {}
        self._manager: LspServerManager | None = None
        self._diagnostics = DiagnosticRegistry()
        self._status = LspInitStatus.NOT_STARTED
        self._init_error: Exception | None = None
        self._init_task: asyncio.Task[None] | None = None
        self._init_event = asyncio.Event()
        self._generation = 0

    @classmethod
    def create(
        cls,
        runtime: Runtime,
        *,
        servers: dict[str, LspServerConfig] | None = None,
    ) -> LspService:
        service = cls(runtime, servers=servers)
        service._kickoff_init()
        return service

    @property
    def manager(self) -> LspServerManager | None:
        if self._status == LspInitStatus.FAILED:
            return None
        return self._manager

    @property
    def diagnostics(self) -> DiagnosticRegistry:
        return self._diagnostics

    def status(self) -> LspInitStatus:
        return self._status

    def is_connected(self) -> bool:
        if self._status == LspInitStatus.FAILED or self._manager is None:
            return False
        servers = self._manager.all_servers()
        if not servers:
            return False
        return any(instance.state != LspState.ERROR for instance in servers.values())

    async def wait_for_init(self) -> None:
        if self._status in (LspInitStatus.SUCCESS, LspInitStatus.FAILED):
            return
        if self._status == LspInitStatus.NOT_STARTED:
            return
        await self._init_event.wait()

    async def change_file(self, path: str, content: str) -> None:
        manager = self.manager
        if manager is None:
            return
        await manager.change_file(path, content)

    async def save_file(self, path: str) -> None:
        manager = self.manager
        if manager is None:
            return
        await manager.save_file(path)

    async def reinitialize(self, *, servers: dict[str, LspServerConfig] | None = None) -> None:
        # Cancel any in-flight init before replacing the event/manager. Otherwise
        # the stale task's finally can set the old event (waking callers parked in
        # wait_for_init on a future that never completes) and _kickoff_init would
        # return early while PENDING.
        if self._init_task is not None and not self._init_task.done():
            self._init_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._init_task
        if servers is not None:
            self._servers = servers
        if self._manager is not None:
            await self._manager.shutdown()
            self._manager = None
        self._status = LspInitStatus.NOT_STARTED
        self._init_error = None
        self._init_event = asyncio.Event()
        self._kickoff_init()

    async def shutdown(self) -> None:
        if self._init_task is not None and not self._init_task.done():
            self._init_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._init_task
        if self._manager is not None:
            await self._manager.shutdown()
            self._manager = None
        self._diagnostics.clear_all()
        self._status = LspInitStatus.NOT_STARTED
        self._init_error = None
        self._init_event.set()
        self._generation += 1

    def _kickoff_init(self) -> None:
        if self._status in (LspInitStatus.PENDING, LspInitStatus.SUCCESS):
            return
        self._status = LspInitStatus.PENDING
        self._init_event.clear()
        generation = self._generation + 1
        self._generation = generation
        self._init_task = asyncio.create_task(self._run_init(generation))

    async def _run_init(self, generation: int) -> None:
        try:
            workspace = str(self._runtime.work_dir)
            manager = LspServerManager(
                get_current_host(),
                self._servers,
                workspace_folder=workspace,
            )
            await manager.initialize()
            if generation != self._generation:
                await manager.shutdown()
                return
            self._manager = manager
            wire_publish_diagnostics_handlers(self._diagnostics, manager.all_servers())
            self._status = LspInitStatus.SUCCESS
        except Exception as exc:
            if generation != self._generation:
                return
            self._status = LspInitStatus.FAILED
            self._init_error = exc
            self._manager = None
            logger.error("Failed to initialize LSP service: {err}", err=exc)
        finally:
            if generation == self._generation:
                self._init_event.set()
