"""mcpext-1: read-only MCP resource & prompt tools."""

from __future__ import annotations

from typing import Any

from pythinker_code.soul.toolset import MCPServerInfo, PythinkerToolset
from pythinker_code.tools.mcp_resource import InvokeMcpPrompt, ListMcpResources, ReadMcpResource


async def test_discover_optional_capability_distinguishes_absent_from_transient() -> None:
    """mcpext-1: a server that genuinely lacks resources/prompts (METHOD_NOT_FOUND) is
    expected and recorded empty quietly; any OTHER failure (transient/transport) must be
    visible (WARNING) rather than silently identical to "no capability", while still
    letting the server connect (empty list, not a propagated exception)."""
    from mcp.shared.exceptions import McpError
    from mcp.types import METHOD_NOT_FOUND, ErrorData

    from pythinker_code.soul.toolset import _discover_optional_capability
    from pythinker_code.utils.logging import logger

    records: list[tuple[str, str]] = []
    # Library logging is disabled by default; enable it so the sink sees the records.
    logger.enable("pythinker_code")
    sink_id = logger.add(
        lambda m: records.append((m.record["level"].name, m.record["message"])), level="DEBUG"
    )
    try:

        async def _absent() -> list[object]:
            raise McpError(ErrorData(code=METHOD_NOT_FOUND, message="Method not found"))

        assert await _discover_optional_capability("db", "resources", _absent) == []

        async def _transient() -> list[object]:
            raise ConnectionError("connection reset by peer")

        assert await _discover_optional_capability("db", "prompts", _transient) == []

        async def _ok() -> list[str]:
            return ["r1", "r2"]

        assert await _discover_optional_capability("db", "resources", _ok) == ["r1", "r2"]
    finally:
        logger.remove(sink_id)
        logger.disable("pythinker_code")  # restore the library default

    warnings = [msg for lvl, msg in records if lvl == "WARNING"]
    # The transient failure surfaced as a WARNING; the genuine absence did not.
    assert len(warnings) == 1
    assert "prompts" in warnings[0]


class _Resource:
    def __init__(self, uri: str, name: str = "", description: str = "", mime: str = "") -> None:
        self.uri = uri
        self.name = name
        self.description = description
        self.mimeType = mime


class _Prompt:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description


class _TextContent:
    def __init__(self, text: str) -> None:
        self.text = text
        self.mimeType = "text/plain"
        self.uri = "res://x"


class _FakeClient:
    def __init__(
        self,
        contents: list[Any] | None = None,
        raise_on_read: bool = False,
        raise_on_enter: bool = False,
    ) -> None:
        self._contents = contents or []
        self._raise = raise_on_read
        self._raise_on_enter = raise_on_enter
        self.prompt_calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakeClient:
        if self._raise_on_enter:
            raise RuntimeError("server not connected")
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def read_resource(self, uri: str) -> list[Any]:
        if self._raise:
            raise RuntimeError("boom")
        return self._contents

    async def get_prompt(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._raise:
            raise RuntimeError("boom")
        self.prompt_calls.append((name, arguments))
        return type(
            "PromptResult",
            (),
            {"messages": [type("PromptMessage", (), {"role": "user", "content": "summarize"})()]},
        )()


def _toolset_with_server(name: str, **kw: Any) -> PythinkerToolset:
    ts = PythinkerToolset()
    ts._mcp_servers[name] = MCPServerInfo(
        status="connected",
        client=kw.get("client", _FakeClient()),
        tools=[],
        resources=kw.get("resources", []),
        prompts=kw.get("prompts", []),
    )
    return ts


async def test_list_resources_and_prompts() -> None:
    ts = _toolset_with_server(
        "db",
        resources=[_Resource("res://schema", "schema", "the schema", "text/plain")],
        prompts=[_Prompt("summarize", "summarize a table")],
    )
    result = await ListMcpResources(ts)(ListMcpResources.params(server=None))

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "server: db" in result.output
    assert "res://schema" in result.output
    assert "the schema" in result.output
    assert "prompt: summarize" in result.output


async def test_list_no_servers_reports_none() -> None:
    ts = PythinkerToolset()
    result = await ListMcpResources(ts)(ListMcpResources.params(server=None))
    assert not result.is_error
    assert isinstance(result.output, str)
    assert "No MCP servers" in result.output


async def test_list_unknown_server_errors() -> None:
    ts = _toolset_with_server("db")
    result = await ListMcpResources(ts)(ListMcpResources.params(server="nope"))
    assert result.is_error
    assert result.brief == "Unknown MCP server"


async def test_read_resource_returns_untrusted_text() -> None:
    ts = _toolset_with_server("db", client=_FakeClient(contents=[_TextContent("hello schema")]))
    result = await ReadMcpResource(ts)(ReadMcpResource.params(server="db", uri="res://schema"))

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "hello schema" in result.output
    assert "untrusted_data" in result.output  # external content is wrapped


async def test_read_resource_unknown_server_errors() -> None:
    ts = _toolset_with_server("db")
    result = await ReadMcpResource(ts)(ReadMcpResource.params(server="nope", uri="x"))
    assert result.is_error
    assert result.brief == "Unknown MCP server"


async def test_read_resource_surfaces_read_failure() -> None:
    ts = _toolset_with_server("db", client=_FakeClient(raise_on_read=True))
    result = await ReadMcpResource(ts)(ReadMcpResource.params(server="db", uri="x"))
    assert result.is_error
    assert result.brief == "Resource read failed"


async def test_read_resource_from_unconnectable_server_errors() -> None:
    # A server that fails to (re)connect at read time surfaces a clean error
    # rather than crashing — covers the failed/unauthorized-server edge case.
    ts = _toolset_with_server("db", client=_FakeClient(raise_on_enter=True))
    result = await ReadMcpResource(ts)(ReadMcpResource.params(server="db", uri="x"))
    assert result.is_error
    assert result.brief == "Resource read failed"


async def test_invoke_prompt_returns_untrusted_messages() -> None:
    client = _FakeClient()
    ts = _toolset_with_server("db", client=client, prompts=[_Prompt("summarize")])

    result = await InvokeMcpPrompt(ts)(
        InvokeMcpPrompt.params(server="db", name="summarize", arguments={"table": "users"})
    )

    assert not result.is_error
    assert client.prompt_calls == [("summarize", {"table": "users"})]
    assert isinstance(result.output, str)
    assert "role: user" in result.output
    assert "summarize" in result.output
    assert "untrusted_data" in result.output


async def test_invoke_prompt_unknown_prompt_errors() -> None:
    ts = _toolset_with_server("db", prompts=[_Prompt("summarize")])

    result = await InvokeMcpPrompt(ts)(
        InvokeMcpPrompt.params(server="db", name="missing", arguments={})
    )

    assert result.is_error
    assert result.brief == "Unknown MCP prompt"
