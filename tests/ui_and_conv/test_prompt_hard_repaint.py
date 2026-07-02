from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from prompt_toolkit.key_binding import KeyPressEvent

from pythinker_code.ui.shell import prompt as shell_prompt


def test_hard_repaint_clears_renderer() -> None:
    """Ctrl+L must trigger an erase + absolute repaint via renderer.clear()."""
    prompt_session = object.__new__(shell_prompt.CustomPromptSession)

    calls: list[str] = []

    class _Renderer:
        def clear(self) -> None:
            calls.append("clear")

    event = SimpleNamespace(app=SimpleNamespace(renderer=_Renderer()))
    prompt_session._hard_repaint(cast(KeyPressEvent, event))

    assert calls == ["clear"]


def test_hard_repaint_survives_renderer_failure() -> None:
    """A failing renderer must not crash the prompt (recovery path stays safe)."""
    prompt_session = object.__new__(shell_prompt.CustomPromptSession)

    class _BoomRenderer:
        def clear(self) -> None:
            raise RuntimeError("boom")

    event = SimpleNamespace(app=SimpleNamespace(renderer=_BoomRenderer()))
    prompt_session._hard_repaint(cast(KeyPressEvent, event))  # must not raise
