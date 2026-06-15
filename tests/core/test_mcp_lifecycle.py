"""mcpext-2: per-server MCP disconnect/reconnect/refresh."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from pythinker_code.exception import MCPRuntimeError
from pythinker_code.soul.toolset import (
    MCPServerInfo,
    MCPTool,
    PythinkerToolset,
    _make_mcp_live_refresh_handler,
)


def _runtime() -> Any:
    from pathlib import Path

    return SimpleNamespace(
        mcp_tools={},
        config=SimpleNamespace(
            mcp=SimpleNamespace(
                client=SimpleNamespace(startup_timeout_ms=1000, tool_call_timeout_ms=1000)
            )
        ),
        session=SimpleNamespace(dir=Path("/tmp")),
    )


def _fake_mcp_tool(server: str, name: str) -> MCPTool[Any]:
    mcp_tool = cast(Any, SimpleNamespace(name=name, description="", inputSchema={}))
    client = cast(Any, SimpleNamespace())
    return MCPTool(server, mcp_tool, client, runtime=_runtime())


@pytest.mark.asyncio
async def test_disconnect_unregisters_tools_and_marks_disconnected() -> None:
    toolset = PythinkerToolset()
    runtime = _runtime()
    tool = _fake_mcp_tool("alpha", "ToolA")
    toolset._mcp_servers["alpha"] = MCPServerInfo(
        status="connected",
        client=cast(Any, SimpleNamespace(close=AsyncMock())),
        tools=[tool],
        resources=[],
        prompts=[],
        server_config={"command": "echo"},
    )
    toolset.add(tool)
    runtime.mcp_tools["mcp__alpha__ToolA"] = tool

    await toolset.disconnect_mcp_server("alpha", runtime)

    assert toolset.find("ToolA") is None
    assert "mcp__alpha__ToolA" not in runtime.mcp_tools
    assert toolset._mcp_servers["alpha"].status == "failed"
    assert toolset._mcp_servers["alpha"].error == "disconnected"


@pytest.mark.asyncio
async def test_refresh_relists_tools_for_connected_server(monkeypatch: pytest.MonkeyPatch) -> None:
    toolset = PythinkerToolset()
    runtime = _runtime()
    old_tool = _fake_mcp_tool("alpha", "OldTool")
    new_tool = _fake_mcp_tool("alpha", "NewTool")
    info = MCPServerInfo(
        status="connected",
        client=cast(Any, SimpleNamespace()),
        tools=[old_tool],
        resources=[],
        prompts=[],
    )
    toolset._mcp_servers["alpha"] = info
    toolset.add(old_tool)
    runtime.mcp_tools["mcp__alpha__OldTool"] = old_tool

    async def _inventory(_server: str, server_info: MCPServerInfo, _runtime: Any) -> None:
        server_info.tools = [new_tool]

    monkeypatch.setattr(toolset, "_inventory_mcp_server", _inventory)

    await toolset.refresh_mcp_server("alpha", runtime)

    assert toolset.find("OldTool") is None
    assert toolset.find("NewTool") is new_tool
    assert runtime.mcp_tools["mcp__alpha__NewTool"] is new_tool


@pytest.mark.asyncio
async def test_reconnect_requires_stored_config() -> None:
    toolset = PythinkerToolset()
    runtime = _runtime()
    toolset._mcp_servers["alpha"] = MCPServerInfo(
        status="failed",
        client=cast(Any, SimpleNamespace(close=AsyncMock())),
        tools=[],
        resources=[],
        prompts=[],
        server_config=None,
    )

    with pytest.raises(MCPRuntimeError, match="stored config"):
        await toolset.reconnect_mcp_server("alpha", runtime)


@pytest.mark.asyncio
async def test_tool_list_changed_triggers_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp.types

    toolset = PythinkerToolset()
    runtime = _runtime()
    toolset._mcp_servers["alpha"] = MCPServerInfo(
        status="connected",
        client=cast(Any, SimpleNamespace()),
        tools=[],
        resources=[],
        prompts=[],
    )
    refreshed: list[str] = []

    async def _refresh(server_name: str, _runtime: Any) -> None:
        refreshed.append(server_name)

    monkeypatch.setattr(toolset, "refresh_mcp_server", _refresh)

    class _FakeClient:
        pass

    handler = _make_mcp_live_refresh_handler(_FakeClient(), toolset, runtime, "alpha")

    await handler.on_tool_list_changed(mcp.types.ToolListChangedNotification())

    assert refreshed == ["alpha"]


@pytest.mark.asyncio
async def test_disconnect_close_timeout_surfaces_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolset = PythinkerToolset()
    runtime = _runtime()
    info = MCPServerInfo(
        status="connected",
        client=cast(Any, SimpleNamespace(close=AsyncMock())),
        tools=[],
        resources=[],
        prompts=[],
        server_config={"command": "echo"},
    )
    toolset._mcp_servers["alpha"] = info
    monkeypatch.setattr("pythinker_code.soul.toolset._MCP_CLOSE_TIMEOUT_S", 0.01)

    async def _slow_close() -> None:
        await asyncio.sleep(1)

    info.client.close = AsyncMock(side_effect=_slow_close)

    await toolset.disconnect_mcp_server("alpha", runtime)

    assert info.status == "failed"
    assert info.error is not None
    assert "timed out" in info.error


@pytest.mark.asyncio
async def test_reconnect_raises_classified_error(monkeypatch: pytest.MonkeyPatch) -> None:
    toolset = PythinkerToolset()
    runtime = _runtime()
    info = MCPServerInfo(
        status="failed",
        client=cast(Any, SimpleNamespace(close=AsyncMock())),
        tools=[],
        resources=[],
        prompts=[],
        server_config={"command": "missing-binary"},
    )
    toolset._mcp_servers["alpha"] = info

    async def _connect(
        _server_name: str, server_info: MCPServerInfo, _runtime: Any
    ) -> tuple[str, Exception | None]:
        server_info.status = "failed"
        server_info.error = "command not found: missing-binary — check the server command/path"
        return "alpha", FileNotFoundError("missing-binary")

    monkeypatch.setattr(toolset, "_connect_mcp_server", _connect)
    monkeypatch.setattr(
        "pythinker_code.soul.toolset._configure_mcp_client_handlers",
        lambda *args, **kwargs: None,
    )

    class _FakeClient:
        pass

    monkeypatch.setattr("fastmcp.Client", lambda *args, **kwargs: _FakeClient())

    with pytest.raises(MCPRuntimeError, match="command not found"):
        await toolset.reconnect_mcp_server("alpha", runtime)
