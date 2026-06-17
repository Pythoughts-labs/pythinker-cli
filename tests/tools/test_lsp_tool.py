"""Tests for the LSP agent tool."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pythinker_core.tooling import ToolReturnValue
from pythinker_host.local import LocalHost
from pythinker_host.path import HostPath

from pythinker_code.agentspec import DEFAULT_AGENT_FILE, load_agent_spec
from pythinker_code.config import LspServerConfig
from pythinker_code.lsp.service import LspInitStatus, LspService
from pythinker_code.soul.agent import load_agent
from pythinker_code.tools import SkipThisTool
from pythinker_code.tools.lsp import Lsp
from pythinker_code.tools.lsp.schemas import Operation, Params
from tests.tools._untrusted import assert_wrapped

_FAKE_LSP_SERVER = """
import json
import os
import sys
from pathlib import Path

LOG = os.environ.get("LSP_TEST_LOG")
WORKSPACE = os.environ.get("LSP_WORKSPACE", "")
EMPTY = os.environ.get("LSP_EMPTY") == "1"
NO_IMPL = os.environ.get("LSP_NO_IMPL") == "1"


def sample_uri():
    return Path(WORKSPACE, "sample.py").resolve().as_uri()


def read_msg():
    content_length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        text = line.decode("ascii").rstrip("\\r\\n")
        if text == "":
            break
        if text.lower().startswith("content-length:"):
            content_length = int(text.split(":", 1)[1].strip())
    if content_length is None:
        return None
    body = sys.stdin.buffer.read(content_length)
    return json.loads(body)


def write_msg(msg):
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\\r\\n\\r\\n".encode("ascii")
    sys.stdout.buffer.write(header + body)
    sys.stdout.buffer.flush()


def log_event(name, payload):
    if not LOG:
        return
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"method": name, "params": payload}) + "\\n")


def hover_result():
    return {
        "contents": {"kind": "markdown", "value": "def sample(): pass"},
        "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
    }


def location_result():
    uri = sample_uri()
    return [{"uri": uri, "range": {"start": {"line": 1, "character": 4}, "end": {"line": 1, "character": 9}}}]

def document_symbol_result():
    return [
        {
            "name": "sample",
            "kind": 12,
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 10}},
            "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
        }
    ]


def workspace_symbol_result():
    uri = sample_uri()
    return [
        {
            "name": "sample",
            "kind": 12,
            "location": {
                "uri": uri,
                "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
            },
            "containerName": "sample.py",
        }
    ]


def call_item():
    uri = sample_uri()
    return [
        {
            "name": "sample",
            "kind": 12,
            "uri": uri,
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 10}},
            "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
        }
    ]


def incoming_call():
    source = call_item()[0]
    return [{"from": source, "fromRanges": [{"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}}]}]


def outgoing_call():
    target = call_item()[0]
    return [{"to": target, "fromRanges": [{"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}}]}]


while True:
    msg = read_msg()
    if msg is None:
        break
    if "id" in msg and "method" in msg:
        req_id = msg["id"]
        method = msg["method"]
        params = msg.get("params", {})
        if method == "initialize":
            caps = {} if NO_IMPL else {"implementationProvider": True}
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": {"capabilities": caps}})
        elif method == "shutdown":
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": None})
        elif method == "textDocument/definition":
            log_event(method, params)
            result = None if EMPTY else location_result()
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": result})
        elif method == "textDocument/references":
            log_event(method, params)
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": location_result()})
        elif method == "textDocument/hover":
            log_event(method, params)
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": hover_result()})
        elif method == "textDocument/documentSymbol":
            log_event(method, params)
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": document_symbol_result()})
        elif method == "workspace/symbol":
            log_event(method, params)
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": workspace_symbol_result()})
        elif method == "textDocument/implementation":
            log_event(method, params)
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": location_result()})
        elif method == "textDocument/prepareCallHierarchy":
            log_event(method, params)
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": call_item()})
        elif method == "callHierarchy/incomingCalls":
            log_event(method, params)
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": incoming_call()})
        elif method == "callHierarchy/outgoingCalls":
            log_event(method, params)
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": outgoing_call()})
        else:
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": None})
    elif "method" in msg:
        if msg["method"] == "exit":
            break
"""


@pytest.fixture
def local_host() -> LocalHost:
    return LocalHost()


def _tool_output_text(result: ToolReturnValue) -> str:
    assert isinstance(result.output, str)
    return result.output


def _server_config(
    *, log_file: Path, workspace: Path, empty: bool = False, no_impl: bool = False
) -> LspServerConfig:
    env = {
        "LSP_TEST_LOG": str(log_file),
        "LSP_WORKSPACE": str(workspace),
    }
    if empty:
        env["LSP_EMPTY"] = "1"
    if no_impl:
        env["LSP_NO_IMPL"] = "1"
    return LspServerConfig.model_validate(
        {
            "command": sys.executable,
            "args": ["-c", _FAKE_LSP_SERVER],
            "extensionToLanguage": {".py": "python"},
            "env": env,
            "startupTimeout": 10.0,
        }
    )


async def _setup_lsp_runtime(
    runtime, tmp_path: Path, *, empty: bool = False, no_impl: bool = False
):
    log_file = tmp_path / "lsp.log"
    runtime.config.lsp.enabled = True
    runtime.session.work_dir = HostPath(str(tmp_path))
    service = LspService.create(
        runtime,
        servers={
            "fake": _server_config(
                log_file=log_file, workspace=tmp_path, empty=empty, no_impl=no_impl
            )
        },
    )
    runtime.lsp = service
    await service.wait_for_init()
    assert service.status() == LspInitStatus.SUCCESS
    assert service.manager is not None
    return service, log_file


def _sample_file(tmp_path: Path) -> Path:
    sample = tmp_path / "sample.py"
    sample.write_text("def sample():\n    return 1\n", encoding="utf-8")
    return sample


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_method", "expected_snippet"),
    [
        (Operation.GO_TO_DEFINITION, "textDocument/definition", "Defined in"),
        (Operation.FIND_REFERENCES, "textDocument/references", "Found 1 reference"),
        (Operation.HOVER, "textDocument/hover", "Hover info at"),
        (Operation.DOCUMENT_SYMBOL, "textDocument/documentSymbol", "Document symbols"),
        (Operation.WORKSPACE_SYMBOL, "workspace/symbol", "Found 1 symbol in workspace"),
        (Operation.GO_TO_IMPLEMENTATION, "textDocument/implementation", "Defined in"),
        (
            Operation.PREPARE_CALL_HIERARCHY,
            "textDocument/prepareCallHierarchy",
            "Call hierarchy item",
        ),
        (Operation.INCOMING_CALLS, "callHierarchy/incomingCalls", "incoming call"),
        (Operation.OUTGOING_CALLS, "callHierarchy/outgoingCalls", "outgoing call"),
    ],
)
async def test_all_operations(
    runtime,
    tmp_path: Path,
    operation: Operation,
    expected_method: str,
    expected_snippet: str,
) -> None:
    service, log_file = await _setup_lsp_runtime(runtime, tmp_path)
    _sample_file(tmp_path)
    tool = Lsp(runtime)

    result = await tool(
        Params(operation=operation, file_path="sample.py", line=2, character=5),
    )

    assert not result.is_error
    output = _tool_output_text(result)
    assert expected_snippet.lower() in output.lower()
    assert_wrapped(output)

    logged = [
        json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line
    ]
    methods = [entry["method"] for entry in logged]
    assert expected_method in methods

    position_entries = [
        entry
        for entry in logged
        if entry["method"]
        in {"textDocument/definition", "textDocument/references", "textDocument/hover"}
    ]
    if position_entries:
        position = position_entries[0]["params"]["position"]
        assert position == {"line": 1, "character": 4}

    await service.shutdown()


@pytest.mark.asyncio
async def test_one_based_position_conversion(runtime, tmp_path: Path) -> None:
    service, log_file = await _setup_lsp_runtime(runtime, tmp_path)
    _sample_file(tmp_path)
    tool = Lsp(runtime)

    await tool(
        Params(operation=Operation.GO_TO_DEFINITION, file_path="sample.py", line=2, character=5),
    )

    logged = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
    assert logged["params"]["position"] == {"line": 1, "character": 4}
    await service.shutdown()


@pytest.mark.asyncio
async def test_unc_path_rejection(runtime, tmp_path: Path) -> None:
    service, _ = await _setup_lsp_runtime(runtime, tmp_path)
    tool = Lsp(runtime)

    result = await tool(
        Params(
            operation=Operation.HOVER,
            file_path="\\\\server\\share\\sample.py",
            line=1,
            character=1,
        ),
    )

    assert result.is_error
    assert "UNC" in result.message
    await service.shutdown()


@pytest.mark.asyncio
async def test_large_file_rejection(runtime, tmp_path: Path) -> None:
    service, _ = await _setup_lsp_runtime(runtime, tmp_path)
    large = tmp_path / "large.py"
    large.write_bytes(b"x" * (10_000_001))
    tool = Lsp(runtime)

    result = await tool(
        Params(operation=Operation.HOVER, file_path="large.py", line=1, character=1),
    )

    assert not result.is_error
    assert "10MB limit" in _tool_output_text(result)
    await service.shutdown()


@pytest.mark.asyncio
async def test_deferred_init_waits_then_succeeds(runtime, tmp_path: Path) -> None:
    runtime.config.lsp.enabled = True
    runtime.session.work_dir = HostPath(str(tmp_path))
    service = LspService.create(
        runtime,
        servers={
            "fake": _server_config(log_file=tmp_path / "lsp.log", workspace=tmp_path),
        },
    )
    runtime.lsp = service
    _sample_file(tmp_path)

    original_wait = service.wait_for_init

    async def tracked_wait() -> None:
        assert service.status() == LspInitStatus.PENDING
        await original_wait()

    service.wait_for_init = tracked_wait  # type: ignore[method-assign]

    tool = Lsp(runtime)
    result = await tool(
        Params(operation=Operation.HOVER, file_path="sample.py", line=1, character=5),
    )

    assert not result.is_error
    assert "Hover info" in _tool_output_text(result)
    await service.shutdown()


def test_skip_when_lsp_disabled(runtime) -> None:
    runtime.config.lsp.enabled = False
    runtime.lsp = None
    with pytest.raises(SkipThisTool):
        Lsp(runtime)


@pytest.mark.asyncio
async def test_agent_spec_loads_lsp_tool(runtime) -> None:
    runtime.config.lsp.enabled = True
    runtime.lsp = LspService(runtime, servers={})
    spec = load_agent_spec(DEFAULT_AGENT_FILE)
    assert "pythinker_code.tools.lsp:Lsp" in spec.tools

    agent = await load_agent(DEFAULT_AGENT_FILE, runtime, mcp_configs=[])
    tool_names = {tool.name for tool in agent.toolset.tools}
    assert "LSP" in tool_names


@pytest.mark.asyncio
async def test_empty_result_is_guidance_not_no_server(runtime, tmp_path: Path) -> None:
    # Server is present and serves .py, but returns null (definition not found).
    # This must surface operation-specific guidance, NOT "No LSP server available".
    service, _ = await _setup_lsp_runtime(runtime, tmp_path, empty=True)
    _sample_file(tmp_path)
    tool = Lsp(runtime)

    result = await tool(
        Params(operation=Operation.GO_TO_DEFINITION, file_path="sample.py", line=2, character=5),
    )

    assert not result.is_error
    output = _tool_output_text(result)
    assert "No definition found" in output
    assert "No LSP server available" not in output
    await service.shutdown()


@pytest.mark.asyncio
async def test_no_server_for_file_type(runtime, tmp_path: Path) -> None:
    service, _ = await _setup_lsp_runtime(runtime, tmp_path)
    other = tmp_path / "notes.txt"
    other.write_text("hello\n", encoding="utf-8")
    tool = Lsp(runtime)

    result = await tool(
        Params(operation=Operation.HOVER, file_path="notes.txt", line=1, character=1),
    )

    assert not result.is_error
    assert "No LSP server available for file type: .txt" in _tool_output_text(result)
    await service.shutdown()


@pytest.mark.asyncio
async def test_request_failure_returns_error(runtime, tmp_path: Path) -> None:
    service, _ = await _setup_lsp_runtime(runtime, tmp_path)
    _sample_file(tmp_path)
    tool = Lsp(runtime)

    async def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("server is error")

    service.manager.send_request = boom  # type: ignore[union-attr,method-assign]

    result = await tool(
        Params(operation=Operation.HOVER, file_path="sample.py", line=1, character=5),
    )

    assert result.is_error
    await service.shutdown()


@pytest.mark.asyncio
async def test_unavailable_when_init_failed(runtime, tmp_path: Path) -> None:
    runtime.config.lsp.enabled = True
    runtime.session.work_dir = HostPath(str(tmp_path))
    service = LspService(runtime, servers={})
    runtime.lsp = service
    service._status = LspInitStatus.FAILED  # noqa: SLF001
    tool = Lsp(runtime)

    result = await tool(
        Params(operation=Operation.HOVER, file_path="sample.py", line=1, character=1),
    )

    assert result.is_error
    assert "unavailable" in result.message.lower()


def test_format_result_document_symbol_hierarchical_file_count() -> None:
    from pythinker_code.tools.lsp.formatters import format_result

    symbols = [{"name": "Foo", "kind": 5, "range": {"start": {"line": 0, "character": 0}}}]
    _formatted, count, file_count = format_result("documentSymbol", symbols, None)
    assert count == 1
    assert file_count == 1


def test_format_result_document_symbol_fallback_counts_unique_files() -> None:
    from pythinker_code.tools.lsp.formatters import format_result

    # SymbolInformation[] fallback: no "range" key, locations may span files.
    symbols = [
        {"name": "Foo", "kind": 5, "location": {"uri": "file:///tmp/a.py"}},
        {"name": "Bar", "kind": 5, "location": {"uri": "file:///tmp/b.py"}},
    ]
    _formatted, count, file_count = format_result("documentSymbol", symbols, None)
    assert count == 2
    assert file_count == 2


@pytest.mark.asyncio
async def test_go_to_implementation_unsupported_server(runtime, tmp_path: Path) -> None:
    # Server omits implementationProvider from its capabilities — guard must
    # return a structured error before sending the request.
    service, log_file = await _setup_lsp_runtime(runtime, tmp_path, no_impl=True)
    _sample_file(tmp_path)
    tool = Lsp(runtime)

    result = await tool(
        Params(
            operation=Operation.GO_TO_IMPLEMENTATION, file_path="sample.py", line=2, character=5
        ),
    )

    assert result.is_error
    assert "go_to_implementation" in result.message
    assert "implementationProvider" in result.message
    assert "fake" in result.message
    # C14: the guard must short-circuit before dispatching the request.
    # The fake server logs every received method; assert no implementation
    # request was ever sent.
    logged_methods: list[str] = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = entry.get("method")
        if isinstance(method, str):
            logged_methods.append(method)
    assert "textDocument/implementation" not in logged_methods
    await service.shutdown()
