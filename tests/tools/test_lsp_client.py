"""Unit tests for the hand-rolled LSP client transport."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest
from pythinker_host.local import LocalHost

from pythinker_code.lsp.client import LspClient
from pythinker_code.lsp.framing import (
    LspProtocolError,
    LspServerDown,
    read_message,
    write_message,
)
from pythinker_code.lsp.protocol import InitializeParams

_FAKE_SERVER = """
import json
import sys


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


while True:
    msg = read_msg()
    if msg is None:
        break
    if "id" in msg and "method" in msg:
        req_id = msg["id"]
        method = msg["method"]
        if method == "initialize":
            write_msg(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"capabilities": {"hoverProvider": True}},
                }
            )
        elif method == "custom/request":
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": {"value": 42}})
        elif method == "shutdown":
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": None})
        else:
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": None})
    elif msg.get("method") == "initialized":
        write_msg(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": "file:///tmp/x.py", "diagnostics": []},
            }
        )
"""

_HANG_SERVER = """
import json
import sys
import time


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


while True:
    msg = read_msg()
    if msg is None:
        break
    if "id" in msg and "method" in msg:
        req_id = msg["id"]
        method = msg["method"]
        if method == "initialize":
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": {"capabilities": {}}})
        elif method == "hang":
            while True:
                time.sleep(3600)
        elif method == "shutdown":
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": None})
"""


@pytest.fixture
def local_host() -> LocalHost:
    return LocalHost()


class TestFraming:
    @pytest.mark.asyncio
    async def test_round_trip(self) -> None:
        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            message = await read_message(reader)
            await write_message(
                writer,
                {"jsonrpc": "2.0", "id": message["id"], "result": "pong"},
            )
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        payload = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": 1}}
        await write_message(writer, payload)
        received = await read_message(reader)
        assert received == {"jsonrpc": "2.0", "id": 1, "result": "pong"}

        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()

    @pytest.mark.asyncio
    async def test_malformed_frame_raises(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"not a valid header\r\n\r\n{}")
        reader.feed_eof()
        with pytest.raises(LspProtocolError):
            await read_message(reader)

    @pytest.mark.asyncio
    async def test_eof_on_header_raises_server_down(self) -> None:
        # A dead server's stdout closes with no data: this is the process gone,
        # not a malformed frame, so it must surface as LspServerDown (so the read
        # loop fails pending requests instead of busy-spinning on LspProtocolError).
        reader = asyncio.StreamReader()
        reader.feed_eof()
        with pytest.raises(LspServerDown):
            await read_message(reader)

    @pytest.mark.asyncio
    async def test_eof_mid_body_raises_server_down(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"Content-Length: 100\r\n\r\n{}")
        reader.feed_eof()
        with pytest.raises(LspServerDown):
            await read_message(reader)


class TestLspClient:
    @pytest.mark.asyncio
    async def test_initialize_stores_capabilities(self, local_host: LocalHost) -> None:
        client = LspClient(local_host)
        await client.start(sys.executable, ["-c", _FAKE_SERVER])
        try:
            result = await client.initialize(InitializeParams(processId=123, rootUri="file:///tmp"))
            assert client.is_initialized
            assert client.capabilities is not None
            assert result.capabilities.hoverProvider is True
        finally:
            await client.stop()

    @pytest.mark.asyncio
    async def test_request_resolves_on_matching_id(self, local_host: LocalHost) -> None:
        client = LspClient(local_host)
        await client.start(sys.executable, ["-c", _FAKE_SERVER])
        try:
            await client.initialize(InitializeParams(processId=1))
            result = await client.send_request("custom/request", {"q": "x"})
            assert result == {"value": 42}
        finally:
            await client.stop()

    @pytest.mark.asyncio
    async def test_notification_reaches_handler(self, local_host: LocalHost) -> None:
        client = LspClient(local_host)
        seen: dict[str, Any] = {}
        got_notification = asyncio.Event()

        def handler(params: Any) -> None:
            seen["params"] = params
            got_notification.set()

        client.on_notification("textDocument/publishDiagnostics", handler)
        await client.start(sys.executable, ["-c", _FAKE_SERVER])
        try:
            await client.initialize(InitializeParams(processId=1))
            await asyncio.wait_for(got_notification.wait(), timeout=2.0)
            assert seen["params"]["uri"] == "file:///tmp/x.py"
        finally:
            await client.stop()

    @pytest.mark.asyncio
    async def test_process_death_fails_pending_requests(self, local_host: LocalHost) -> None:
        client = LspClient(local_host)
        await client.start(sys.executable, ["-c", _HANG_SERVER])
        await client.initialize(InitializeParams(processId=1))

        pending = asyncio.create_task(client.send_request("hang", {}))
        await asyncio.sleep(0.2)
        assert client._proc is not None
        await client._proc.kill()

        with pytest.raises(LspServerDown):
            await asyncio.wait_for(pending, timeout=2.0)

        await client.stop()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, local_host: LocalHost) -> None:
        client = LspClient(local_host)
        await client.start(sys.executable, ["-c", _FAKE_SERVER])
        await client.initialize(InitializeParams(processId=1))
        await client.stop()
        await client.stop()
        assert client._proc is None
