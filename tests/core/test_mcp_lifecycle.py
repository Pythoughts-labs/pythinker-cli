"""mcpext-2: per-server MCP disconnect/reconnect/refresh."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from pythinker_code.exception import MCPRuntimeError
from pythinker_code.soul.toolset import MCPServerInfo, MCPTool, PythinkerToolset


def _runtime() -> Any:
    return SimpleNamespace(
        mcp_tools={},
        config=SimpleNamespace(
            mcp=SimpleNamespace(
                client=SimpleNamespace(startup_timeout_ms=1000, tool_call_timeout_ms=1000)
            )
        ),
        session=SimpleNamespace(dir="/tmp"),
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
