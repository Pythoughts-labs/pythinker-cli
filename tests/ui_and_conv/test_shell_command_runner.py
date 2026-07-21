from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pythinker_host import Host, get_current_host
from pythinker_host.path import HostPath

import pythinker_code.ui.shell as shell_module
from pythinker_code.soul import Soul
from pythinker_code.ui.shell.command_runner import ShellCommandRunner
from pythinker_code.ui.shell.components.render_utils import render_plain
from pythinker_code.utils.environment import Environment


def _environment(shell_path: str = "/bin/sh") -> Environment:
    return Environment(
        os_kind="macOS",
        os_arch="test",
        os_version="test",
        shell_name="sh",
        shell_path=HostPath(shell_path),
    )


def _runner(
    *, shell_path: str = "/bin/sh", output_limit_bytes: int = 1024 * 1024
) -> ShellCommandRunner:
    return ShellCommandRunner(
        get_current_host(),
        environment=_environment(shell_path),
        output_limit_bytes=output_limit_bytes,
    )


def test_runner_rejects_negative_output_limit() -> None:
    with pytest.raises(ValueError):
        ShellCommandRunner(
            get_current_host(),
            environment=_environment(),
            output_limit_bytes=-1,
        )


@pytest.mark.asyncio
async def test_runner_reaps_process_when_stdin_close_fails() -> None:
    """A failure after spawn (here stdin.close()) must still terminate/reap the child."""

    class _FailingStdin:
        def close(self) -> None:
            raise BrokenPipeError("stdin already closed")

    class _FakeProcess:
        def __init__(self) -> None:
            self.stdin = _FailingStdin()
            self.killed = False
            self.reaped = False

        async def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.reaped = True
            return 0

    process = _FakeProcess()

    class _FakeHost:
        async def exec(self, *_argv: str, env: dict[str, str] | None = None) -> _FakeProcess:
            return process

    runner = ShellCommandRunner(cast(Host, _FakeHost()), environment=_environment())

    with pytest.raises(BrokenPipeError):
        await runner.run("echo hi")

    # The spawned child was terminated and reaped rather than leaked.
    assert process.killed is True
    assert process.reaped is True


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX shell")
async def test_runner_preserves_quoting_and_pipelines() -> None:
    result = await _runner().run("printf '%s\\n' 'hello world' | tr 'a-z' 'A-Z'")

    assert result.stdout == "HELLO WORLD\n"
    assert result.stderr == ""
    assert result.returncode == 0


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX shell")
async def test_runner_supports_redirection(tmp_path: Path) -> None:
    destination = tmp_path / "redirected output.txt"
    command = f"printf redirected > {shlex.quote(str(destination))}"

    result = await _runner().run(command)
    encoding = "utf-8"

    assert destination.read_text(encoding=encoding) == "redirected"
    assert result.stdout == ""
    assert result.returncode == 0


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX shell")
async def test_runner_captures_stderr_and_nonzero_exit() -> None:
    result = await _runner().run("printf problem >&2; exit 7")

    assert result.stdout == ""
    assert result.stderr == "problem"
    assert result.returncode == 7
    rendered = shell_module._format_local_shell_output(
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
    )
    assert rendered is not None
    assert render_plain(rendered).splitlines() == ["problem", "exit 7"]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX shell")
async def test_runner_reports_no_output() -> None:
    result = await _runner().run(":")

    assert result.stdout == ""
    assert result.stderr == ""
    assert result.returncode == 0
    rendered = shell_module._format_local_shell_output(
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
    )
    assert rendered is not None
    assert render_plain(rendered).strip() == "(No output)"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX shell")
async def test_runner_truncates_stdout_and_stderr_separately() -> None:
    command = "head -c 2048 /dev/zero | tr '\\0' o; head -c 2048 /dev/zero | tr '\\0' e >&2"
    result = await _runner(output_limit_bytes=1024).run(command)

    assert result.truncated_stdout is True
    assert result.truncated_stderr is True
    assert result.stdout.startswith("o" * 1024)
    assert result.stderr.startswith("e" * 1024)
    assert result.stdout.endswith("\n... output truncated ...\n")
    assert result.stderr.endswith("\n... output truncated ...\n")


def test_runner_builds_windows_powershell_argv() -> None:
    argv = ShellCommandRunner.build_argv(
        "Write-Output 'hello'",
        shell_name="Windows PowerShell",
        shell_path=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    )

    assert argv == (
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-command",
        "Write-Output 'hello'",
    )


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="requires POSIX process inspection")
async def test_runner_cancellation_terminates_and_reaps_process(tmp_path: Path) -> None:
    pid_path = tmp_path / "shell.pid"
    task = asyncio.create_task(
        _runner().run(f"echo $$ > {shlex.quote(str(pid_path))}; exec sleep 30")
    )
    for _ in range(100):
        if pid_path.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_path.exists()

    encoding = "utf-8"
    pid = int(pid_path.read_text(encoding=encoding).strip())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_invalid_shell_path_surfaces_existing_failure_message(monkeypatch) -> None:
    class _InvalidRunner(ShellCommandRunner):
        def __init__(self) -> None:
            super().__init__(
                get_current_host(),
                environment=_environment("/definitely/missing/pythinker-shell"),
            )

    printed: list[object] = []
    fake_console = SimpleNamespace(print=lambda value=None: printed.append(value))
    monkeypatch.setattr(shell_module, "ShellCommandRunner", _InvalidRunner)
    monkeypatch.setattr(shell_module, "console", fake_console)
    monkeypatch.setattr(shell_module, "install_sigint_handler", lambda loop, handler: lambda: None)
    shell = shell_module.Shell(
        cast(Soul, SimpleNamespace(available_slash_commands=[], name="test")),
        None,
    )

    await shell._run_shell_command("printf hello")

    assert "Failed to run shell command:" in str(printed[-1])


def test_shell_output_rendering_sanitizes_control_characters() -> None:
    rendered = shell_module._format_local_shell_output(
        stdout="safe\x1b[2J\x01text\n",
        stderr="",
        returncode=0,
    )

    assert rendered is not None
    assert render_plain(rendered).strip() == "safetext"
