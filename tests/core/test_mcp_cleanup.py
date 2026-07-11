"""mcpext-3: toolset cleanup isolates a hung or failing MCP client close."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

import pythinker_code.soul.toolset as toolset_mod
from pythinker_code.soul.toolset import MCPServerInfo, PythinkerToolset


def _info(client: object) -> MCPServerInfo:
    return MCPServerInfo(
        status="connected", client=cast(Any, client), tools=[], resources=[], prompts=[]
    )


class _GoodClient:
    def __init__(self, closed: list[str], tag: str) -> None:
        self._closed = closed
        self._tag = tag

    async def close(self) -> None:
        self._closed.append(self._tag)


class _BadClient:
    async def close(self) -> None:
        raise RuntimeError("close failed")


class _HangClient:
    async def close(self) -> None:
        await asyncio.sleep(10)


async def test_cleanup_isolates_failing_close() -> None:
    closed: list[str] = []
    ts = PythinkerToolset()
    ts._mcp_servers["bad"] = _info(_BadClient())
    ts._mcp_servers["good"] = _info(_GoodClient(closed, "good"))

    await ts.cleanup()  # must not raise despite the failing client

    assert closed == ["good"]


async def test_cleanup_times_out_hung_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(toolset_mod, "_MCP_CLOSE_TIMEOUT_S", 0.05)
    closed: list[str] = []
    ts = PythinkerToolset()
    ts._mcp_servers["hang"] = _info(_HangClient())
    ts._mcp_servers["good"] = _info(_GoodClient(closed, "good"))

    await asyncio.wait_for(ts.cleanup(), timeout=2.0)  # completes fast despite the hang

    assert closed == ["good"]


async def test_cleanup_cancels_background_load_before_closing_clients() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    closed: list[str] = []
    ts = PythinkerToolset()
    ts._mcp_servers["alpha"] = _info(_GoodClient(closed, "alpha"))

    async def load() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    loading = asyncio.create_task(load())
    ts._mcp_loading_task = loading
    await entered.wait()

    await ts.cleanup()

    assert loading.cancelled()
    assert cancelled.is_set()
    assert closed == ["alpha"]
