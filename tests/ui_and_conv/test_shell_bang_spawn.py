from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import pythinker_code.ui.shell as shell_module


@pytest.mark.asyncio
async def test_bang_command_detaches_windows_console(monkeypatch) -> None:
    """`!` foreground commands must not attach to the interactive console on Windows.

    A child sharing the TUI's console can clear it or reset its modes via the
    Win32 console API (bypassing the stdio pipes), blanking the shell UI until
    the terminal is restarted; CREATE_NO_WINDOW detaches it.
    """
    shell = object.__new__(shell_module.Shell)

    captured: dict[str, object] = {}

    async def _capture_and_abort(command: str, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("spawn intercepted by test")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _capture_and_abort)
    monkeypatch.setattr(shell_module, "install_sigint_handler", lambda loop, handler: lambda: None)
    # Swap the module's `os` reference rather than mutating the global
    # `os.name`, which corrupts pathlib/pytest path handling mid-run.
    monkeypatch.setattr(shell_module, "os", SimpleNamespace(name="nt"))

    await shell._run_shell_command("echo hi")

    flags = captured["creationflags"]
    assert isinstance(flags, int)
    assert flags & 0x08000000  # CREATE_NO_WINDOW
