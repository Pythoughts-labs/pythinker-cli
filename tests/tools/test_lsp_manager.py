"""Unit tests for LSP server manager and instance lifecycle."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pythinker_host.local import LocalHost

from pythinker_code.config import LspServerConfig
from pythinker_code.lsp.framing import LspServerDown, LspStartError
from pythinker_code.lsp.instance import LspServerInstance, LspState
from pythinker_code.lsp.manager import LspServerManager

_RECORDING_SERVER = """
import json
import os
import sys

LOG = os.environ.get("LSP_TEST_LOG")


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
        fh.write(json.dumps({"event": name, "payload": payload}) + "\\n")


while True:
    msg = read_msg()
    if msg is None:
        break
    if "id" in msg and "method" in msg:
        req_id = msg["id"]
        method = msg["method"]
        if method == "initialize":
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": {"capabilities": {}}})
        elif method == "shutdown":
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": None})
        else:
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": None})
    elif "method" in msg:
        log_event(msg["method"], msg.get("params", {}))
        if msg["method"] == "exit":
            break
"""

_RETRY_SERVER = """
import json
import sys

STATE = {"attempts": 0}


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
        elif method == "flaky/request":
            STATE["attempts"] += 1
            if STATE["attempts"] < 3:
                write_msg(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32801, "message": "content modified"},
                    }
                )
            else:
                write_msg({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})
        elif method == "shutdown":
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": None})
        else:
            write_msg({"jsonrpc": "2.0", "id": req_id, "result": None})
    elif msg.get("method") == "exit":
        break
"""


@pytest.fixture
def local_host() -> LocalHost:
    return LocalHost()


def _server_config(*, ext: str, language: str, log_file: Path | None = None) -> LspServerConfig:
    env: dict[str, str] = {}
    if log_file is not None:
        env["LSP_TEST_LOG"] = str(log_file)
    return LspServerConfig.model_validate(
        {
            "command": sys.executable,
            "args": ["-c", _RECORDING_SERVER],
            "extensionToLanguage": {ext: language},
            "env": env,
            "maxRestarts": 2,
            "startupTimeout": 10.0,
        }
    )


class TestLspServerManager:
    @pytest.mark.asyncio
    async def test_extension_routing_first_match_wins(
        self, local_host: LocalHost, tmp_path: Path
    ) -> None:
        manager = LspServerManager(
            local_host,
            {
                "pyright": _server_config(ext=".py", language="python"),
                "typescript": _server_config(ext=".ts", language="typescript"),
                "typescript_dup": LspServerConfig.model_validate(
                    {
                        "command": sys.executable,
                        "args": ["-c", _RECORDING_SERVER],
                        "extensionToLanguage": {".ts": "typescript"},
                    }
                ),
            },
            workspace_folder=str(tmp_path),
        )
        await manager.initialize()

        py_server = manager.server_for_file("foo.py")
        ts_server = manager.server_for_file("bar.ts")
        assert py_server is not None and py_server.name == "pyright"
        assert ts_server is not None and ts_server.name == "typescript"

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_open_change_fallback_and_did_save(
        self, local_host: LocalHost, tmp_path: Path
    ) -> None:
        manager = LspServerManager(
            local_host,
            {"pyright": _server_config(ext=".py", language="python")},
            workspace_folder=str(tmp_path),
        )
        await manager.initialize()
        server = manager.server_for_file("sample.py")
        assert server is not None

        sent: list[str] = []
        original_send = server.send_notification

        async def track_send(method: str, params: Any) -> None:
            sent.append(method)
            await original_send(method, params)

        server.send_notification = track_send  # type: ignore[method-assign]

        target = tmp_path / "sample.py"
        target.write_text("x = 1\n", encoding="utf-8")

        await manager.change_file(str(target), "x = 2\n")
        assert manager.is_file_open(str(target))
        assert "textDocument/didOpen" in sent

        await manager.change_file(str(target), "x = 3\n")
        assert "textDocument/didChange" in sent

        await manager.save_file(str(target))
        assert "textDocument/didSave" in sent

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_did_change_versions_increment_monotonically(
        self, local_host: LocalHost, tmp_path: Path
    ) -> None:
        import json

        log_file = tmp_path / "events.jsonl"
        manager = LspServerManager(
            local_host,
            {"pyright": _server_config(ext=".py", language="python", log_file=log_file)},
            workspace_folder=str(tmp_path),
        )
        await manager.initialize()

        target = tmp_path / "sample.py"
        target.write_text("x = 1\n", encoding="utf-8")

        await manager.open_file(str(target), "x = 1\n")
        await manager.change_file(str(target), "x = 2\n")
        await manager.change_file(str(target), "x = 3\n")
        await manager.shutdown()

        versions = [
            event["payload"]["textDocument"]["version"]
            for event in (
                json.loads(line)
                for line in log_file.read_text(encoding="utf-8").splitlines()
                if line
            )
            if event["event"] == "textDocument/didChange"
        ]
        assert versions == [2, 3]

    @pytest.mark.asyncio
    async def test_restart_clears_open_doc_state(
        self, local_host: LocalHost, tmp_path: Path
    ) -> None:
        manager = LspServerManager(
            local_host,
            {"pyright": _server_config(ext=".py", language="python")},
            workspace_folder=str(tmp_path),
        )
        await manager.initialize()
        target = tmp_path / "sample.py"
        target.write_text("x = 1\n", encoding="utf-8")

        await manager.open_file(str(target), "x = 1\n")
        assert manager.is_file_open(str(target))

        # Stop the underlying instance, then park it in ERROR — the state a
        # mid-session crash leaves behind. ensure_started then spawns a fresh
        # process, which has no open documents, so the manager must forget the
        # stale open-file state instead of skipping didOpen on the new process.
        server = manager.server_for_file(str(target))
        assert server is not None
        await server.stop()
        server.mark_crashed(RuntimeError("crash"))
        assert server.state == LspState.ERROR

        assert await manager.ensure_started(str(target)) is not None
        assert not manager.is_file_open(str(target))

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_isolates_failing_server(
        self, local_host: LocalHost, tmp_path: Path
    ) -> None:
        manager = LspServerManager(
            local_host,
            {
                "good": _server_config(ext=".py", language="python"),
                "also_good": _server_config(ext=".js", language="javascript"),
            },
            workspace_folder=str(tmp_path),
        )
        await manager.initialize()

        good = manager.server_for_file("a.py")
        bad = manager.server_for_file("b.js")
        assert good is not None and bad is not None
        await good.start()
        await bad.start()

        bad.stop = AsyncMock(side_effect=RuntimeError("stop failed"))  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="Failed to stop"):
            await manager.shutdown()

        assert manager.all_servers() == {}

    @pytest.mark.asyncio
    async def test_per_server_init_failure_isolated(
        self, local_host: LocalHost, tmp_path: Path
    ) -> None:
        manager = LspServerManager(
            local_host,
            {
                "bad": LspServerConfig.model_validate(
                    {"command": "", "extensionToLanguage": {".rs": "rust"}}
                ),
                "good": _server_config(ext=".py", language="python"),
            },
            workspace_folder=str(tmp_path),
        )
        await manager.initialize()

        assert manager.server_for_file("x.py") is not None
        assert manager.server_for_file("x.rs") is None

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_crash_cap_parks_at_error(self, local_host: LocalHost, tmp_path: Path) -> None:
        instance = LspServerInstance(
            "broken",
            LspServerConfig.model_validate(
                {
                    "command": sys.executable,
                    "args": ["-c", "import sys; sys.exit(1)"],
                    "extensionToLanguage": {".py": "python"},
                    "maxRestarts": 2,
                }
            ),
            local_host,
            workspace_folder=str(tmp_path),
        )

        with pytest.raises((LspStartError, LspServerDown, RuntimeError, OSError)):
            await instance.start()
        assert instance.state == LspState.ERROR

        for _ in range(2):
            with pytest.raises((LspStartError, LspServerDown, RuntimeError, OSError)):
                await instance.restart()

        with pytest.raises(RuntimeError, match="Max restart attempts"):
            await instance.restart()
        assert instance.state == LspState.ERROR

    @pytest.mark.asyncio
    async def test_unexpected_crash_parks_instance_at_error(
        self, local_host: LocalHost, tmp_path: Path
    ) -> None:
        # A server that exits after the first request simulates a mid-session
        # crash. The client read loop must notify the instance (on_crash), which
        # parks it in ERROR so the crash cap is enforced and is_healthy() is False.
        crash_server = (
            "import json, sys\n"
            "def read_msg():\n"
            "    cl = None\n"
            "    while True:\n"
            "        line = sys.stdin.buffer.readline()\n"
            "        if not line:\n"
            "            return None\n"
            "        t = line.decode('ascii').rstrip('\\r\\n')\n"
            "        if t == '':\n"
            "            break\n"
            "        if t.lower().startswith('content-length:'):\n"
            "            cl = int(t.split(':', 1)[1].strip())\n"
            "    if cl is None:\n"
            "        return None\n"
            "    return json.loads(sys.stdin.buffer.read(cl))\n"
            "def write_msg(m):\n"
            "    b = json.dumps(m, separators=(',', ':')).encode('utf-8')\n"
            "    sys.stdout.buffer.write(f'Content-Length: {len(b)}\\r\\n\\r\\n'.encode('ascii') + b)\n"
            "    sys.stdout.buffer.flush()\n"
            "m = read_msg()\n"
            "write_msg({'jsonrpc': '2.0', 'id': m['id'], 'result': {'capabilities': {}}})\n"
            "read_msg()\n"  # 'initialized' notification
            "sys.exit(1)\n"  # crash before serving any request
        )
        instance = LspServerInstance(
            "crasher",
            LspServerConfig.model_validate(
                {
                    "command": sys.executable,
                    "args": ["-c", crash_server],
                    "extensionToLanguage": {".py": "python"},
                }
            ),
            local_host,
            workspace_folder=str(tmp_path),
        )
        await instance.start()
        assert instance.state == LspState.RUNNING

        with pytest.raises((LspServerDown, RuntimeError)):
            await instance.send_request("textDocument/hover", {})

        for _ in range(20):
            if instance.state == LspState.ERROR:
                break
            await asyncio.sleep(0.05)
        assert instance.state == LspState.ERROR
        assert not instance.is_healthy()

        await instance.stop()

    @pytest.mark.asyncio
    async def test_transient_error_retries_then_succeeds(
        self, local_host: LocalHost, tmp_path: Path
    ) -> None:
        instance = LspServerInstance(
            "flaky",
            LspServerConfig.model_validate(
                {
                    "command": sys.executable,
                    "args": ["-c", _RETRY_SERVER],
                    "extensionToLanguage": {".py": "python"},
                }
            ),
            local_host,
            workspace_folder=str(tmp_path),
        )
        await instance.start()
        try:
            result = await instance.send_request("flaky/request", {})
            assert result == {"ok": True}
        finally:
            await instance.stop()

    @pytest.mark.asyncio
    async def test_workspace_configuration_shim(
        self, local_host: LocalHost, tmp_path: Path
    ) -> None:
        from pythinker_code.lsp.manager import _workspace_configuration_handler

        assert _workspace_configuration_handler({"items": [{}, {}]}) == [None, None]
