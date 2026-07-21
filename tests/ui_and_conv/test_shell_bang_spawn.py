from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import pythinker_code.ui.shell as shell_module
from pythinker_code.ui.shell.command_runner import ShellCommandResult


@pytest.mark.asyncio
async def test_bang_command_runs_through_host_exec_runner(monkeypatch) -> None:
    """`!` foreground commands must execute via ShellCommandRunner/Host.exec.

    Host.exec applies CREATE_NO_WINDOW on Windows (covered by
    packages/pythinker-host/tests/test_local_host.py), which keeps a child off
    the interactive console: console-API writes from a child sharing the TUI's
    console bypass the stdio pipes and can blank the shell UI until terminal
    restart. The shell must therefore never fall back to
    asyncio.create_subprocess_shell for `!` commands.
    """
    shell = object.__new__(shell_module.Shell)

    ran: dict[str, object] = {}

    class _RecordingRunner:
        async def run(self, command: str) -> ShellCommandResult:
            ran["command"] = command
            return ShellCommandResult(
                stdout="hi\n",
                stderr="",
                returncode=0,
                truncated_stdout=False,
                truncated_stderr=False,
            )

    async def _forbidden_shell_spawn(command: str, **kwargs):
        raise AssertionError("`!` commands must not use create_subprocess_shell")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _forbidden_shell_spawn)
    monkeypatch.setattr(shell_module, "ShellCommandRunner", _RecordingRunner)
    monkeypatch.setattr(shell_module, "install_sigint_handler", lambda loop, handler: lambda: None)
    printed: list[object] = []
    monkeypatch.setattr(
        shell_module, "console", SimpleNamespace(print=lambda value=None: printed.append(value))
    )

    await shell._run_shell_command("echo hi")

    assert ran["command"] == "echo hi"
    assert printed
