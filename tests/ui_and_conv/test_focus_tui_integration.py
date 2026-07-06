from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from pythinker_core.tooling.empty import EmptyToolset
from rich.text import Text

import pythinker_code.ui.shell as shell_module
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.ui.shell.focus_model import FocusTuiModel
from pythinker_code.ui.shell.focus_surface import FocusTuiSurface
from pythinker_code.ui.shell.visualize import _PromptLiveView, visualize
from pythinker_code.utils.aioqueue import QueueShutDown
from pythinker_code.wire.types import StatusUpdate


class _PromptSession:
    def __init__(self) -> None:
        self.modals: list[object] = []
        self.running: object | None = None

    def mark_turn_starting(self) -> None:
        pass

    def clear_turn_starting(self) -> None:
        pass

    def update_pinned_todos(self, _items: object) -> None:
        pass

    def invalidate(self) -> None:
        pass

    def attach_running_prompt(self, delegate: object) -> None:
        self.running = delegate

    def detach_running_prompt(self, delegate: object) -> None:
        if self.running is delegate:
            self.running = None

    def attach_modal(self, delegate: object) -> None:
        self.modals.append(delegate)

    def detach_modal(self, delegate: object) -> None:
        self.modals.remove(delegate)


def _make_shell(runtime: Runtime, tmp_path: Path) -> shell_module.Shell:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    return shell_module.Shell(soul)


@pytest.mark.asyncio
async def test_shell_focus_mode_enables_focus_surface(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.config.tui.focus_mode = True
    shell = _make_shell(runtime, tmp_path)
    prompt_session = cast(Any, _PromptSession())
    shell._prompt_session = prompt_session  # pyright: ignore[reportPrivateUsage]
    enabled_models: list[FocusTuiModel | None] = []

    class _Wire:
        def ui_side(self, *, merge: bool) -> object:
            return object()

    async def fake_run_soul(_soul, _user_input, ui_factory, _cancel_event, _wire_file, _runtime):
        await ui_factory(_Wire())

    async def fake_visualize(_wire, **kwargs) -> None:
        view = _PromptLiveView(
            kwargs["initial_status"],
            prompt_session=kwargs["prompt_session"],
            steer=kwargs["steer"],
        )
        assert kwargs["focus_mode"] is True
        if kwargs["focus_mode"]:
            view.enable_focus_model()
        kwargs["on_view_ready"](view)
        enabled_models.append(view.focus_model)

    monkeypatch.setattr(shell_module, "run_soul", fake_run_soul)
    monkeypatch.setattr(shell_module, "visualize", fake_visualize)

    assert await shell.run_soul_command("hello") is True

    model = enabled_models[0]
    assert isinstance(model, FocusTuiModel)
    assert FocusTuiSurface(model).create_application().full_screen is True


@pytest.mark.asyncio
async def test_focus_mode_does_not_run_scrollback_handoff_for_action_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoffs: list[str] = []
    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    view.enable_focus_model()
    view._active_turn_depth = 1
    view._emit_action_block(Text("tool summary"))

    async def _fake_handoff(_emit: object, *, reason: str = "?") -> None:
        handoffs.append(reason)

    monkeypatch.setattr(view, "_run_scrollback_handoff", _fake_handoff)
    await view._flush_pending_scrollback()

    assert handoffs == []
    assert view._pending_scrollback == []


@pytest.mark.asyncio
async def test_visualize_focus_mode_attaches_surface_modal() -> None:
    class _Wire:
        async def receive(self) -> object:
            raise QueueShutDown

    prompt_session = _PromptSession()
    attached: list[object] = []

    def _on_view_ready(view: object) -> None:
        assert isinstance(view, _PromptLiveView)
        assert isinstance(view.focus_model, FocusTuiModel)
        attached.extend(prompt_session.modals)

    await visualize(
        cast(Any, _Wire()),
        initial_status=StatusUpdate(),
        prompt_session=cast(Any, prompt_session),
        steer=lambda _content: None,
        focus_mode=True,
        on_view_ready=_on_view_ready,
    )

    assert len(attached) == 1
    assert isinstance(attached[0], FocusTuiSurface)
    assert prompt_session.modals == []
    assert prompt_session.running is None
