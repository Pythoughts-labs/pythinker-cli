"""Shared PromptLifecycle test double for prompt-completion tests."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, override

from pythinker_code.ui.shell.prompting.lifecycle import PromptLifecycle


class RecordingLifecycle(PromptLifecycle):
    """PromptLifecycle that records created tasks so tests can await them."""

    def __init__(self) -> None:
        super().__init__()
        self.created: list[asyncio.Task[None]] = []

    @override
    def create_task(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = super().create_task(coro)
        self.created.append(task)
        return task

    async def drain(self) -> None:
        if self.created:
            await asyncio.gather(*self.created, return_exceptions=True)
