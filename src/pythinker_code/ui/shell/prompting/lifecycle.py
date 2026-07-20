"""Awaited lifetime management for prompt-session background resources."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from pythinker_code.utils.logging import logger

AsyncCloser = Callable[[], Awaitable[None]]


class PromptLifecycle:
    """Own tasks and asynchronous cleanup belonging to one prompt session."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []
        self._closers: list[tuple[str, AsyncCloser]] = []
        self._closed = False
        self._close_complete = asyncio.Event()

    def create_task(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Create lifecycle-owned work, or explicitly refuse it after shutdown."""
        if self._closed:
            coro.close()
            raise RuntimeError("prompt lifecycle is closed")
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    def register_closer(self, name: str, async_closer: AsyncCloser) -> None:
        """Register an async resource closer to run during shutdown."""
        if self._closed:
            raise RuntimeError(f"prompt lifecycle is closed; cannot register closer {name!r}")
        self._closers.append((name, async_closer))

    async def aclose(self) -> None:
        """Cancel all work, await it, then close resources in reverse order."""
        if self._closed:
            await self._close_complete.wait()
            return
        self._closed = True
        try:
            for task in self._tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*self._tasks, return_exceptions=True)
            for task, result in zip(self._tasks, results, strict=True):
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    logger.warning(
                        "Prompt lifecycle task failed during shutdown: task={} error={!r}",
                        task.get_name(),
                        result,
                    )

            for name, closer in reversed(self._closers):
                try:
                    await closer()
                except asyncio.CancelledError:
                    continue
                except Exception as exc:
                    logger.warning(
                        "Prompt lifecycle resource failed during shutdown: resource={} error={!r}",
                        name,
                        exc,
                    )
        finally:
            self._close_complete.set()
