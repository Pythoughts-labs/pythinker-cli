from __future__ import annotations

from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pythinker_core.tooling.empty import EmptyToolset

import pythinker_code.ui.shell as shell_module
from pythinker_code.soul import Soul
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.ui.shell.prompt import PromptMode, UserInput
from pythinker_code.utils.slashcmd import SlashCommand, SlashCommandCall
from pythinker_code.wire.types import TextPart


@pytest.fixture(autouse=True)
def _legacy_tui_style(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "pythinker")


class _FakePromptSession:
    instances: list[_FakePromptSession] = []
    responses: deque[UserInput | BaseException] = deque()

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs
        self.prompt_calls = 0
        self.last_submission_was_running = False
        _FakePromptSession.instances.append(self)

    def __enter__(self) -> _FakePromptSession:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    async def prompt_next(self) -> UserInput:
        self.prompt_calls += 1
        response = _FakePromptSession.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response

    def attach_running_prompt(self, delegate) -> None:
        return None

    def detach_running_prompt(self, delegate) -> None:
        return None

    def mark_turn_starting(self) -> None:
        return None


def _make_user_input(
    command: str,
    *,
    mode: PromptMode = PromptMode.AGENT,
    resolved_command: str | None = None,
) -> UserInput:
    return UserInput(
        mode=mode,
        command=command,
        resolved_command=command if resolved_command is None else resolved_command,
        content=[TextPart(text=command if resolved_command is None else resolved_command)],
    )


def _make_fake_soul():
    return SimpleNamespace(
        name="Test Soul",
        available_slash_commands=[],
        model_capabilities=set(),
        model_name=None,
        thinking=False,
        status=SimpleNamespace(context_usage=0.0, context_tokens=0, max_context_tokens=0),
    )


def _make_pythinker_soul(runtime: Runtime, tmp_path: Path) -> PythinkerSoul:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    return PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))


def _noop(app: object, args: str) -> None:
    pass


@pytest.mark.asyncio
async def test_shell_slash_alias_tracks_canonical_command(monkeypatch) -> None:
    tracked: list[tuple[str, dict[str, object]]] = []
    called: list[str] = []
    fake_command = SlashCommand(
        name="help",
        description="help command",
        func=lambda app, args: called.append(args),
        aliases=["h"],
    )

    monkeypatch.setattr(
        "pythinker_code.telemetry.track",
        lambda event, **properties: tracked.append((event, properties)),
    )
    monkeypatch.setattr(shell_module.shell_slash_registry, "list_commands", lambda: [fake_command])
    monkeypatch.setattr(
        shell_module.shell_slash_registry,
        "find_command",
        lambda name: fake_command if name in {"help", "h"} else None,
    )

    shell = shell_module.Shell(cast(Soul, _make_fake_soul()))

    await shell._run_slash_command(SlashCommandCall(name="h", args="", raw_input="/h"))

    assert called == [""]
    assert ("input_command", {"command": "help"}) in tracked


@pytest.fixture
def _patched_shell_run(monkeypatch):
    _FakePromptSession.instances = []
    _FakePromptSession.responses = deque()
    monkeypatch.setattr(shell_module, "CustomPromptSession", _FakePromptSession)
    monkeypatch.setattr(shell_module, "_print_welcome_info", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell_module, "get_env_bool", lambda name: True)
    monkeypatch.setattr(shell_module, "ensure_tty_sane", lambda: None)
    monkeypatch.setattr(shell_module, "ensure_new_line", lambda: None)

    printed: list[str] = []
    monkeypatch.setattr(
        shell_module.console,
        "print",
        lambda text="": printed.append(getattr(text, "plain", str(text))),
    )
    return printed


@pytest.mark.asyncio
async def test_shell_run_passes_sticky_input_config(
    monkeypatch, runtime: Runtime, tmp_path: Path, _patched_shell_run
) -> None:
    _FakePromptSession.responses = deque([EOFError()])
    runtime.config.tui.sticky_input = False
    monkeypatch.setattr(shell_module.Shell, "_schedule_startup_update_task", lambda self: None)
    monkeypatch.setattr(shell_module, "replay_recent_history", AsyncMock())

    def _close_background_coro(self, coro):
        coro.close()
        return None

    monkeypatch.setattr(shell_module.Shell, "_start_background_task", _close_background_coro)

    shell = shell_module.Shell(_make_pythinker_soul(runtime, tmp_path))

    result = await shell.run()

    assert result is True
    assert _FakePromptSession.instances[0].kwargs["sticky_input"] is False


@pytest.mark.asyncio
async def test_shell_run_treats_hidden_slash_in_placeholder_as_regular_agent_input(
    monkeypatch, _patched_shell_run
) -> None:
    printed = _patched_shell_run
    _FakePromptSession.responses = deque(
        [
            UserInput(
                mode=PromptMode.AGENT,
                command="[Pasted text #1 +3 lines]",
                resolved_command="/quit\nstill send this",
                content=[TextPart(text="/quit\nstill send this")],
            ),
            EOFError(),
        ]
    )
    shell = shell_module.Shell(cast(Soul, _make_fake_soul()))
    shell.run_soul_command = AsyncMock(return_value=True)
    shell._run_slash_command = AsyncMock()

    result = await shell.run()

    assert result is True
    assert _FakePromptSession.instances[0].prompt_calls == 2
    shell.run_soul_command.assert_awaited_once_with([TextPart(text="/quit\nstill send this")])
    shell._run_slash_command.assert_not_awaited()
    assert printed == ["❯ /quit\nstill send this", "", "Bye!"]


@pytest.mark.asyncio
async def test_shell_run_dispatches_visible_slash_with_expanded_placeholder_args(
    monkeypatch, _patched_shell_run
) -> None:
    printed = _patched_shell_run
    _FakePromptSession.responses = deque(
        [
            UserInput(
                mode=PromptMode.AGENT,
                command="/fakecmd [Pasted text #1 +3 lines]",
                resolved_command="/fakecmd line1\nline2\nline3",
                content=[TextPart(text="line1\nline2\nline3")],
            ),
            EOFError(),
        ]
    )
    shell = shell_module.Shell(cast(Soul, _make_fake_soul()))
    shell.run_soul_command = AsyncMock(return_value=True)
    shell._run_slash_command = AsyncMock()

    result = await shell.run()

    assert result is True
    assert _FakePromptSession.instances[0].prompt_calls == 2
    shell.run_soul_command.assert_not_awaited()
    shell._run_slash_command.assert_awaited_once()
    assert shell._run_slash_command.await_args is not None
    command_call = shell._run_slash_command.await_args.args[0]
    assert command_call.name == "fakecmd"
    assert command_call.args == "line1\nline2\nline3"
    assert command_call.raw_input == "/fakecmd line1\nline2\nline3"
    assert printed == ["Bye!"]


@pytest.mark.asyncio
async def test_shell_run_echoes_visible_skill_slash_with_placeholder_before_dispatch(
    monkeypatch, _patched_shell_run
) -> None:
    printed = _patched_shell_run
    _FakePromptSession.responses = deque(
        [
            UserInput(
                mode=PromptMode.AGENT,
                command="/skill:demo [Pasted text #1 +3 lines]",
                resolved_command="/skill:demo line1\nline2\nline3",
                content=[TextPart(text="line1\nline2\nline3")],
            ),
            EOFError(),
        ]
    )
    shell = shell_module.Shell(cast(Soul, _make_fake_soul()))
    shell._available_slash_commands["skill:demo"] = SlashCommand(
        name="skill:demo",
        description="demo skill",
        func=_noop,
        aliases=[],
    )
    shell.run_soul_command = AsyncMock(return_value=True)
    shell._run_slash_command = AsyncMock()

    result = await shell.run()

    assert result is True
    assert _FakePromptSession.instances[0].prompt_calls == 2
    shell.run_soul_command.assert_awaited_once_with("/skill:demo line1\nline2\nline3")
    shell._run_slash_command.assert_not_awaited()
    assert printed == ["❯ /skill:demo line1\nline2\nline3", "", "Bye!"]


@pytest.mark.asyncio
async def test_shell_run_echoes_visible_flow_slash_with_placeholder_before_dispatch(
    monkeypatch, _patched_shell_run
) -> None:
    printed = _patched_shell_run
    _FakePromptSession.responses = deque(
        [
            UserInput(
                mode=PromptMode.AGENT,
                command="/flow:demo [Pasted text #1 +3 lines]",
                resolved_command="/flow:demo line1\nline2\nline3",
                content=[TextPart(text="line1\nline2\nline3")],
            ),
            EOFError(),
        ]
    )
    shell = shell_module.Shell(cast(Soul, _make_fake_soul()))
    shell._available_slash_commands["flow:demo"] = SlashCommand(
        name="flow:demo",
        description="demo flow",
        func=_noop,
        aliases=[],
    )
    shell.run_soul_command = AsyncMock(return_value=True)
    shell._run_slash_command = AsyncMock()

    result = await shell.run()

    assert result is True
    assert _FakePromptSession.instances[0].prompt_calls == 2
    shell.run_soul_command.assert_awaited_once_with("/flow:demo line1\nline2\nline3")
    shell._run_slash_command.assert_not_awaited()
    assert printed == ["❯ /flow:demo line1\nline2\nline3", "", "Bye!"]


@pytest.mark.asyncio
async def test_shell_run_echoes_unregistered_skill_slash_before_unknown_dispatch(
    monkeypatch, _patched_shell_run
) -> None:
    printed = _patched_shell_run
    _FakePromptSession.responses = deque(
        [
            UserInput(
                mode=PromptMode.AGENT,
                command="/skill:not-found fix login",
                resolved_command="/skill:not-found fix login",
                content=[TextPart(text="/skill:not-found fix login")],
            ),
            EOFError(),
        ]
    )
    shell = shell_module.Shell(cast(Soul, _make_fake_soul()))
    shell.run_soul_command = AsyncMock(return_value=True)
    shell._run_slash_command = AsyncMock()

    result = await shell.run()

    assert result is True
    shell.run_soul_command.assert_not_awaited()
    shell._run_slash_command.assert_awaited_once()
    assert printed == ["❯ /skill:not-found fix login", "Bye!"]


@pytest.mark.asyncio
async def test_shell_run_echoes_unregistered_flow_slash_before_unknown_dispatch(
    monkeypatch, _patched_shell_run
) -> None:
    printed = _patched_shell_run
    _FakePromptSession.responses = deque(
        [
            UserInput(
                mode=PromptMode.AGENT,
                command="/flow:not-found run it",
                resolved_command="/flow:not-found run it",
                content=[TextPart(text="/flow:not-found run it")],
            ),
            EOFError(),
        ]
    )
    shell = shell_module.Shell(cast(Soul, _make_fake_soul()))
    shell.run_soul_command = AsyncMock(return_value=True)
    shell._run_slash_command = AsyncMock()

    result = await shell.run()

    assert result is True
    shell.run_soul_command.assert_not_awaited()
    shell._run_slash_command.assert_awaited_once()
    assert printed == ["❯ /flow:not-found run it", "Bye!"]


@pytest.mark.asyncio
async def test_shell_run_exits_immediately_for_visible_quit_command(
    monkeypatch, _patched_shell_run
) -> None:
    printed = _patched_shell_run
    _FakePromptSession.responses = deque([_make_user_input("/quit")])
    shell = shell_module.Shell(cast(Soul, _make_fake_soul()))
    shell.run_soul_command = AsyncMock(return_value=True)
    shell._run_slash_command = AsyncMock()

    result = await shell.run()

    assert result is True
    assert _FakePromptSession.instances[0].prompt_calls == 1
    shell.run_soul_command.assert_not_awaited()
    shell._run_slash_command.assert_not_awaited()
    assert printed == ["Bye!"]


@pytest.mark.asyncio
async def test_shell_run_uses_resolved_command_for_shell_mode(
    monkeypatch, _patched_shell_run
) -> None:
    _FakePromptSession.responses = deque(
        [
            UserInput(
                mode=PromptMode.SHELL,
                command="[Pasted text #1]",
                resolved_command="echo resolved",
                content=[TextPart(text="echo resolved")],
            ),
            EOFError(),
        ]
    )
    shell = shell_module.Shell(cast(Soul, _make_fake_soul()))
    shell.run_soul_command = AsyncMock(return_value=True)
    shell._run_shell_command = AsyncMock()

    result = await shell.run()

    assert result is True
    shell._run_shell_command.assert_awaited_once_with("echo resolved")
    shell.run_soul_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_shell_run_exits_immediately_for_visible_exit_command_in_shell_mode(
    monkeypatch, _patched_shell_run
) -> None:
    printed = _patched_shell_run
    _FakePromptSession.responses = deque([_make_user_input("exit", mode=PromptMode.SHELL)])
    shell = shell_module.Shell(cast(Soul, _make_fake_soul()))
    shell.run_soul_command = AsyncMock(return_value=True)
    shell._run_shell_command = AsyncMock()
    shell._run_slash_command = AsyncMock()

    result = await shell.run()

    assert result is True
    assert _FakePromptSession.instances[0].prompt_calls == 1
    shell.run_soul_command.assert_not_awaited()
    shell._run_shell_command.assert_not_awaited()
    shell._run_slash_command.assert_not_awaited()
    assert printed == ["Bye!"]


@pytest.mark.asyncio
async def test_shell_run_exits_immediately_for_visible_slash_exit_command_in_shell_mode(
    monkeypatch, _patched_shell_run
) -> None:
    printed = _patched_shell_run
    _FakePromptSession.responses = deque([_make_user_input("/exit", mode=PromptMode.SHELL)])
    shell = shell_module.Shell(cast(Soul, _make_fake_soul()))
    shell.run_soul_command = AsyncMock(return_value=True)
    shell._run_shell_command = AsyncMock()
    shell._run_slash_command = AsyncMock()

    result = await shell.run()

    assert result is True
    assert _FakePromptSession.instances[0].prompt_calls == 1
    shell.run_soul_command.assert_not_awaited()
    shell._run_shell_command.assert_not_awaited()
    shell._run_slash_command.assert_not_awaited()
    assert printed == ["Bye!"]


@pytest.mark.asyncio
async def test_shell_run_exits_immediately_for_visible_slash_quit_command_in_shell_mode(
    monkeypatch, _patched_shell_run
) -> None:
    printed = _patched_shell_run
    _FakePromptSession.responses = deque([_make_user_input("/quit", mode=PromptMode.SHELL)])
    shell = shell_module.Shell(cast(Soul, _make_fake_soul()))
    shell.run_soul_command = AsyncMock(return_value=True)
    shell._run_shell_command = AsyncMock()
    shell._run_slash_command = AsyncMock()

    result = await shell.run()

    assert result is True
    assert _FakePromptSession.instances[0].prompt_calls == 1
    shell.run_soul_command.assert_not_awaited()
    shell._run_shell_command.assert_not_awaited()
    shell._run_slash_command.assert_not_awaited()
    assert printed == ["Bye!"]


@pytest.mark.asyncio
async def test_shell_run_prints_welcome_banner_by_default(monkeypatch, _patched_shell_run) -> None:
    banner_calls: list[int] = []
    monkeypatch.setattr(shell_module, "_print_welcome_info", lambda *a, **k: banner_calls.append(1))
    _FakePromptSession.responses = deque([EOFError()])
    shell = shell_module.Shell(cast(Soul, _make_fake_soul()))

    await shell.run()

    assert banner_calls == [1]


@pytest.mark.asyncio
async def test_shell_run_suppresses_welcome_banner_on_reload(
    monkeypatch, _patched_shell_run
) -> None:
    # A same-session reload (/model, /theme, /new, fork, …) leaves the previous
    # banner on screen and prints its own confirmation, so the splash must not
    # be reprinted — otherwise it stacks a redundant copy below the old one.
    banner_calls: list[int] = []
    monkeypatch.setattr(shell_module, "_print_welcome_info", lambda *a, **k: banner_calls.append(1))
    _FakePromptSession.responses = deque([EOFError()])
    shell = shell_module.Shell(cast(Soul, _make_fake_soul()), suppress_banner=True)

    await shell.run()

    assert banner_calls == []
