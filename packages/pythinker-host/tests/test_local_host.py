from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Generator
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest

from pythinker_host import reset_current_host, set_current_host
from pythinker_host.local import LocalHost
from pythinker_host.path import HostPath


@pytest.fixture
def local_host(tmp_path: Path) -> Generator[LocalHost]:
    """Set LocalHost as the current Host and switch cwd to a temp directory."""
    local = LocalHost()
    token = set_current_host(local)
    old_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        yield local
    finally:
        os.chdir(old_cwd)
        reset_current_host(token)


def test_pathclass_gethome_and_getcwd(local_host: LocalHost):
    path_class = local_host.pathclass()
    if os.name == "nt":
        assert issubclass(path_class, PureWindowsPath)
    else:
        assert issubclass(path_class, PurePosixPath)

    assert str(local_host.gethome()) == str(Path.home())
    assert str(local_host.getcwd()) == str(Path.cwd())


async def test_chdir_and_stat(local_host: LocalHost):
    new_dir = local_host.getcwd() / "nested"
    await local_host.mkdir(new_dir)

    await local_host.chdir(new_dir)
    assert Path.cwd() == new_dir.unsafe_to_local_path()

    file_path = new_dir / "file.txt"
    await local_host.writetext(file_path, "hello world")

    stat_result = await local_host.stat(file_path)
    assert stat_result.st_size == len("hello world")


async def test_iterdir_and_glob(local_host: LocalHost):
    tmp_path = local_host.getcwd()
    await local_host.mkdir(tmp_path / "alpha")
    await local_host.writetext(tmp_path / "bravo.txt", "bravo")
    await local_host.writetext(tmp_path / "charlie.TXT", "charlie")

    entries = [entry async for entry in local_host.iterdir(tmp_path)]
    assert {entry.name for entry in entries} == {"alpha", "bravo.txt", "charlie.TXT"}
    assert all(isinstance(entry, HostPath) for entry in entries)

    matched = [entry.name async for entry in local_host.glob(tmp_path, "*.txt")]
    assert set(matched) == {"bravo.txt"}


async def test_glob_includes_hidden_files(local_host: LocalHost):
    """Glob should match dotfiles (hidden files) with * and ** patterns."""
    tmp_path = local_host.getcwd()

    # Create hidden and visible files
    await local_host.writetext(tmp_path / ".gitlab-ci.yml", "stages: [build]")
    await local_host.writetext(tmp_path / "config.yml", "key: value")
    await local_host.mkdir(tmp_path / "src")
    await local_host.mkdir(tmp_path / "src" / ".config")
    await local_host.writetext(tmp_path / "src" / ".config" / "settings.yml", "debug: true")
    await local_host.writetext(tmp_path / "src" / "main.py", "pass")

    # *.yml should match .gitlab-ci.yml
    matched = {entry.name async for entry in local_host.glob(tmp_path, "*.yml")}
    assert ".gitlab-ci.yml" in matched
    assert "config.yml" in matched

    # src/**/*.yml should find files in hidden directories
    deep_matched = [
        str(entry.relative_to(tmp_path))
        async for entry in local_host.glob(tmp_path, "src/**/*.yml")
    ]
    assert any(".config" in p for p in deep_matched)


async def test_read_write_and_append_text(local_host: LocalHost):
    tmp_path = local_host.getcwd()
    file_path = tmp_path / "note.txt"

    written = await local_host.writetext(file_path, "line1")
    assert written == len("line1")

    content = await local_host.readtext(file_path)
    assert content == "line1"

    await local_host.writetext(file_path, "\nline2", mode="a")
    lines = [line async for line in local_host.readlines(file_path)]
    assert "".join(lines) == "line1\nline2"


async def test_writetext_preserves_lf_line_endings(local_host: LocalHost):
    """writetext should not convert LF to CRLF on any platform."""
    tmp_path = local_host.getcwd()
    file_path = tmp_path / "lf.txt"

    await local_host.writetext(file_path, "hello\nworld\n")

    # Read back as binary to check actual bytes on disk
    raw = await local_host.readbytes(file_path)
    assert raw == b"hello\nworld\n", f"Expected LF line endings, got {raw!r}"


async def test_writetext_preserves_crlf_line_endings(local_host: LocalHost):
    """writetext should preserve CRLF if explicitly present in content."""
    tmp_path = local_host.getcwd()
    file_path = tmp_path / "crlf.txt"

    await local_host.writetext(file_path, "hello\r\nworld\r\n")

    raw = await local_host.readbytes(file_path)
    assert raw == b"hello\r\nworld\r\n", f"Expected CRLF preserved, got {raw!r}"


async def test_mkdir_with_parents(local_host: LocalHost):
    tmp_path = local_host.getcwd()
    nested_dir = tmp_path / "a" / "b" / "c"

    await local_host.mkdir(nested_dir, parents=True)
    assert await nested_dir.is_dir()


async def test_read_write_bytes(local_host: LocalHost):
    tmp_path = local_host.getcwd()
    file_path = tmp_path / "data.bin"
    await local_host.writebytes(file_path, b"\x00\x01\xff")
    assert await local_host.readbytes(file_path) == b"\x00\x01\xff"


def _python_code_args(code: str) -> tuple[str, str, str]:
    return sys.executable, "-c", code


async def test_exec_runs_command_and_streams(local_host: LocalHost):
    code = "import sys\nsys.stdout.write('hello\\n')\nsys.stderr.write('stderr line\\n')\n"

    process = await local_host.exec(*_python_code_args(code))

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_data, stderr_data = await asyncio.gather(process.stdout.read(), process.stderr.read())
    assert await process.wait() == 0
    assert stdout_data.decode("utf-8").strip() == "hello"
    assert stderr_data.decode("utf-8").strip() == "stderr line"


async def test_exec_runs_command_wait_before_read(local_host: LocalHost):
    code = "import sys\nsys.stdout.write('hello\\n')\nsys.stderr.write('stderr line\\n')\n"

    process = await local_host.exec(*_python_code_args(code))

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    assert await process.wait() == 0
    stdout_data, stderr_data = await asyncio.gather(process.stdout.read(), process.stderr.read())
    assert stdout_data.decode("utf-8").strip() == "hello"
    assert stderr_data.decode("utf-8").strip() == "stderr line"


async def test_exec_non_zero_exit(local_host: LocalHost):
    process = await local_host.exec(*_python_code_args("import sys; sys.exit(7)"))

    exit_code = await process.wait()
    assert exit_code == 7


async def test_exec_wait_timeout(local_host: LocalHost):
    process = await local_host.exec(*_python_code_args("import time; time.sleep(1)"))
    assert process.pid > 0

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=0.01)
    finally:
        if process.returncode is None:
            await process.kill()
        await process.wait()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group signal path")
async def test_kill_skips_signal_after_process_exit(
    local_host: LocalHost, monkeypatch: pytest.MonkeyPatch
):
    """Once a process has exited (and been reaped), its pid/pgid may be recycled
    by the OS, so kill() must not send a process-group signal."""
    import pythinker_host.local as local_module

    process = await local_host.exec(*_python_code_args("import sys; sys.exit(0)"))
    await process.wait()
    assert process.returncode is not None

    # Fail loudly if kill() reaches the signal path at all: with the returncode
    # guard it must short-circuit before even resolving the process group.
    calls: list[str] = []

    def _record_getpgid(pid: int) -> int:
        calls.append("getpgid")
        return 0

    def _record_killpg(pgid: int, sig: int) -> None:
        calls.append("killpg")

    monkeypatch.setattr(local_module.os, "getpgid", _record_getpgid)
    monkeypatch.setattr(local_module.os, "killpg", _record_killpg)

    await process.kill()

    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group signal path")
async def test_kill_signals_running_process(local_host: LocalHost, monkeypatch: pytest.MonkeyPatch):
    """A still-running process is killed via its process group."""
    import pythinker_host.local as local_module

    process = await local_host.exec(*_python_code_args("import time; time.sleep(30)"))
    assert process.returncode is None

    # os.killpg / signal.SIGKILL are POSIX-only; this test is skipped on Windows
    # but pyright still type-checks the body, so route them through Any holders
    # to keep the strict host gate green on the win32 platform stubs.
    os_any: Any = local_module.os
    signal_any: Any = signal
    real_killpg = os_any.killpg
    sent: list[int] = []

    def _record_killpg(pgid: int, sig: int) -> None:
        sent.append(sig)
        real_killpg(pgid, sig)  # actually terminate so the process is not leaked

    monkeypatch.setattr(local_module.os, "killpg", _record_killpg)

    await process.kill()
    await process.wait()

    assert sent and sent[0] == signal_any.SIGKILL


async def test_exec_windows_child_gets_hidden_console(
    local_host: LocalHost, monkeypatch: pytest.MonkeyPatch
):
    """On Windows the child must not attach to the interactive console.

    A child sharing the TUI's console can clear it or reset its modes through
    the Win32 console API (bypassing the stdio pipes), blanking the shell UI
    until the terminal is restarted. CREATE_NO_WINDOW gives the child its own
    hidden console; CREATE_NEW_PROCESS_GROUP keeps kill() semantics unchanged.
    """
    from types import SimpleNamespace

    from pythinker_host import local as local_module

    captured: dict[str, Any] = {}

    class _FakePipe:
        pass

    class _FakeProcess:
        stdin: Any = _FakePipe()
        stdout: Any = _FakePipe()
        stderr: Any = _FakePipe()

    async def _fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _FakeProcess()

    # Swap the module's `os` reference rather than mutating the global
    # `os.name`, which corrupts pathlib/pytest path handling mid-run.
    monkeypatch.setattr(local_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        local_module.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
    )

    await local_host.exec("cmd", "/c", "echo hi")

    flags = captured["creationflags"]
    assert flags & 0x00000200  # CREATE_NEW_PROCESS_GROUP
    assert flags & 0x08000000  # CREATE_NO_WINDOW
    assert "start_new_session" not in captured
