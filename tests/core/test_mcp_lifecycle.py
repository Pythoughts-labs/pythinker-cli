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
    _discover_optional_capability,
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

    # _inventory_mcp_server discovers without mutating; the caller assigns the
    # returned inventory only after the awaited call succeeds.
    async def _inventory(_server: str, _server_info: MCPServerInfo, _runtime: Any) -> Any:
        return [new_tool], [], []

    monkeypatch.setattr(toolset, "_inventory_mcp_server", _inventory)

    await toolset.refresh_mcp_server("alpha", runtime)

    assert info.tools == [new_tool]
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


@pytest.mark.asyncio
async def test_refresh_failure_preserves_live_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed refresh must not drop the server's still-working tools."""
    toolset = PythinkerToolset()
    runtime = _runtime()
    live_tool = _fake_mcp_tool("alpha", "LiveTool")
    info = MCPServerInfo(
        status="connected",
        client=cast(Any, SimpleNamespace()),
        tools=[live_tool],
        resources=[],
        prompts=[],
    )
    toolset._mcp_servers["alpha"] = info
    toolset.add(live_tool)
    runtime.mcp_tools["mcp__alpha__LiveTool"] = live_tool

    async def _failing_inventory(_server: str, _info: MCPServerInfo, _runtime: Any) -> None:
        raise RuntimeError("list_tools blew up")

    monkeypatch.setattr(toolset, "_inventory_mcp_server", _failing_inventory)

    with pytest.raises(MCPRuntimeError, match="Failed to refresh"):
        await toolset.refresh_mcp_server("alpha", runtime)

    assert toolset.find("LiveTool") is live_tool
    assert runtime.mcp_tools["mcp__alpha__LiveTool"] is live_tool
    assert info.tools == [live_tool]


@pytest.mark.asyncio
async def test_disconnect_reclaims_shadowed_tool_from_other_server() -> None:
    """Disconnecting the winning server must fall the tool name back, not orphan it."""
    toolset = PythinkerToolset()
    runtime = _runtime()
    shared_alpha = _fake_mcp_tool("alpha", "Shared")
    shared_beta = _fake_mcp_tool("beta", "Shared")
    toolset._mcp_servers["alpha"] = MCPServerInfo(
        status="connected",
        client=cast(Any, SimpleNamespace(close=AsyncMock())),
        tools=[shared_alpha],
        resources=[],
        prompts=[],
        server_config={"command": "echo"},
    )
    toolset._mcp_servers["beta"] = MCPServerInfo(
        status="connected",
        client=cast(Any, SimpleNamespace(close=AsyncMock())),
        tools=[shared_beta],
        resources=[],
        prompts=[],
        server_config={"command": "echo"},
    )
    # Configured order publishes beta last, so it wins the shared name.
    toolset._publish_connected_mcp_tools(runtime)
    assert toolset.find("Shared") is shared_beta

    await toolset.disconnect_mcp_server("beta", runtime)

    assert toolset.find("Shared") is shared_alpha
    assert runtime.mcp_tools["mcp__alpha__Shared"] is shared_alpha
    assert "mcp__beta__Shared" not in runtime.mcp_tools
    assert toolset._mcp_servers["beta"].status == "failed"


@pytest.mark.asyncio
async def test_partial_connect_publishes_connected_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that connects must be published even when a sibling fails the aggregate."""
    from fastmcp.mcp_config import MCPConfig

    toolset = PythinkerToolset()
    runtime = _runtime()
    good_tool = _fake_mcp_tool("good", "GoodTool")

    async def _connect(
        server_name: str, server_info: MCPServerInfo, _runtime: Any
    ) -> tuple[str, Exception | None]:
        if server_name == "good":
            server_info.status = "connected"
            server_info.tools = [good_tool]
            return server_name, None
        server_info.status = "failed"
        server_info.error = "boom"
        return server_name, RuntimeError("boom")

    monkeypatch.setattr(toolset, "_connect_mcp_server", _connect)
    monkeypatch.setattr(
        "pythinker_code.soul.toolset._configure_mcp_client_handlers",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("fastmcp.Client", lambda *args, **kwargs: SimpleNamespace())

    config = MCPConfig.model_validate(
        {"mcpServers": {"good": {"command": "echo"}, "bad": {"command": "echo"}}}
    )

    with pytest.raises(MCPRuntimeError, match="Failed to connect"):
        await toolset.load_mcp_tools([config], runtime, in_background=False)

    assert toolset.find("GoodTool") is good_tool
    assert runtime.mcp_tools["mcp__good__GoodTool"] is good_tool


@pytest.mark.asyncio
async def test_optional_inventory_distinguishes_method_not_found_and_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.shared.exceptions import McpError
    from mcp.types import METHOD_NOT_FOUND, ErrorData

    import pythinker_code.soul.toolset as toolset_mod

    debug_messages: list[str] = []
    warning_messages: list[str] = []

    def capture_debug(message: str, **_kwargs: object) -> None:
        debug_messages.append(message)

    def capture_warning(message: str, **_kwargs: object) -> None:
        warning_messages.append(message)

    monkeypatch.setattr(toolset_mod.logger, "debug", capture_debug)
    monkeypatch.setattr(toolset_mod.logger, "warning", capture_warning)

    async def unsupported() -> list[object]:
        raise McpError(ErrorData(code=METHOD_NOT_FOUND, message="not supported"))

    async def transient() -> list[object]:
        raise ConnectionError("inventory transport reset")

    assert await _discover_optional_capability("alpha", "resources", unsupported) == []
    assert debug_messages == ["MCP server {name} does not support {cap}"]
    assert warning_messages == []

    assert await _discover_optional_capability("alpha", "prompts", transient) == []
    assert warning_messages == [
        "MCP server {name} failed listing {cap} (transient?); treating as empty: {error}"
    ]


@pytest.mark.asyncio
async def test_list_change_storm_completes_every_refresh_without_orphan_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    entered = asyncio.Semaphore(0)
    release = asyncio.Event()
    completed: list[int] = []

    async def refresh(_server_name: str, _runtime: Any) -> None:
        index = len(completed)
        entered.release()
        await release.wait()
        completed.append(index)

    monkeypatch.setattr(toolset, "refresh_mcp_server", refresh)

    class _FakeClient:
        pass

    handler = _make_mcp_live_refresh_handler(_FakeClient(), toolset, runtime, "alpha")
    tasks = [
        asyncio.create_task(handler.on_tool_list_changed(mcp.types.ToolListChangedNotification()))
        for _ in range(5)
    ]
    for _ in tasks:
        await entered.acquire()
    release.set()
    await asyncio.gather(*tasks)

    assert len(completed) == 5
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_refresh_racing_disconnect_cannot_republish_disconnected_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolset = PythinkerToolset()
    runtime = _runtime()
    old_tool = _fake_mcp_tool("alpha", "OldTool")
    new_tool = _fake_mcp_tool("alpha", "NewTool")
    inventory_entered = asyncio.Event()
    release_inventory = asyncio.Event()
    info = MCPServerInfo(
        status="connected",
        client=cast(Any, SimpleNamespace(close=AsyncMock())),
        tools=[old_tool],
        resources=[],
        prompts=[],
        server_config={"command": "echo"},
    )
    toolset._mcp_servers["alpha"] = info
    toolset._publish_connected_mcp_tools(runtime)

    async def inventory(
        _server_name: str, _server_info: MCPServerInfo, _runtime: Any
    ) -> tuple[list[MCPTool[Any]], list[Any], list[Any]]:
        inventory_entered.set()
        await release_inventory.wait()
        return [new_tool], [], []

    monkeypatch.setattr(toolset, "_inventory_mcp_server", inventory)
    refresh_task = asyncio.create_task(toolset.refresh_mcp_server("alpha", runtime))
    await inventory_entered.wait()
    await toolset.disconnect_mcp_server("alpha", runtime)
    release_inventory.set()
    await refresh_task

    assert info.status == "failed"
    assert toolset.find("OldTool") is None
    assert toolset.find("NewTool") is None
    assert runtime.mcp_tools == {}


@pytest.mark.asyncio
async def test_rebuild_rolls_back_after_partial_publication_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolset = PythinkerToolset()
    runtime = _runtime()
    original = _fake_mcp_tool("original", "Original")
    alpha = _fake_mcp_tool("alpha", "Alpha")
    beta = _fake_mcp_tool("beta", "Beta")
    toolset.add(original)
    runtime.mcp_tools["mcp__original__Original"] = original
    toolset._mcp_servers = {
        "alpha": MCPServerInfo(
            status="connected",
            client=cast(Any, SimpleNamespace()),
            tools=[alpha],
            resources=[],
            prompts=[],
        ),
        "beta": MCPServerInfo(
            status="connected",
            client=cast(Any, SimpleNamespace()),
            tools=[beta],
            resources=[],
            prompts=[],
        ),
    }
    original_register = toolset._register_mcp_tools

    def fail_second_server(server_name: str, tools: list[MCPTool[Any]]) -> None:
        if server_name == "beta":
            raise RuntimeError("publication failed")
        original_register(server_name, tools)

    monkeypatch.setattr(toolset, "_register_mcp_tools", fail_second_server)

    with pytest.raises(RuntimeError, match="publication failed"):
        toolset._rebuild_published_mcp_tools(runtime)

    assert toolset.find("Original") is original
    assert toolset.find("Alpha") is None
    assert toolset.find("Beta") is None
    assert runtime.mcp_tools == {"mcp__original__Original": original}


@pytest.mark.asyncio
async def test_hung_connect_reports_timeout_without_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolset = PythinkerToolset()
    runtime = _runtime()
    runtime.config.mcp.client.startup_timeout_ms = 0
    info = MCPServerInfo(
        status="pending",
        client=cast(Any, SimpleNamespace()),
        tools=[],
        resources=[],
        prompts=[],
    )

    async def hung_inventory(
        _server_name: str, _server_info: MCPServerInfo, _runtime: Any
    ) -> tuple[list[MCPTool[Any]], list[Any], list[Any]]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(toolset, "_inventory_mcp_server", hung_inventory)

    server_name, error = await toolset._connect_mcp_server("alpha", info, runtime)

    assert server_name == "alpha"
    assert isinstance(error, TimeoutError)
    assert info.status == "failed"
    assert info.error is not None and "startup timed out" in info.error
    assert info.tools == []


@pytest.mark.asyncio
async def test_duplicate_server_name_connects_once_with_last_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastmcp.mcp_config import MCPConfig

    toolset = PythinkerToolset()
    runtime = _runtime()
    connected: list[str] = []

    async def connect(
        server_name: str, server_info: MCPServerInfo, _runtime: Any
    ) -> tuple[str, Exception | None]:
        connected.append(server_name)
        server_info.status = "connected"
        return server_name, None

    monkeypatch.setattr(toolset, "_connect_mcp_server", connect)
    monkeypatch.setattr(
        "pythinker_code.soul.toolset._configure_mcp_client_handlers",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("fastmcp.Client", lambda *_args, **_kwargs: SimpleNamespace())
    first = MCPConfig.model_validate({"mcpServers": {"alpha": {"command": "first-command"}}})
    second = MCPConfig.model_validate({"mcpServers": {"alpha": {"command": "second-command"}}})

    await toolset.load_mcp_tools([first, second], runtime, in_background=False)

    assert connected == ["alpha"]
    assert tuple(toolset.mcp_servers) == ("alpha",)
    assert toolset.mcp_servers["alpha"].server_config.command == "second-command"
