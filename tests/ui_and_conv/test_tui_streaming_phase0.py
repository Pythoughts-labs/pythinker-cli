"""Phase 0 streaming pipeline tests (frame scheduler, preview path, commit cache)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console, Group
from rich.text import Text

from pythinker_code.ui.shell.components.markdown import (
    PythinkerMarkdown,
    _markdown_commit_boundary_cached,
    markdown_commit_boundary,
)
from pythinker_code.ui.shell.motion import STREAMING_CARET_GLYPH
from pythinker_code.ui.shell.visualize._blocks import _ContentBlock
from pythinker_code.ui.shell.visualize._live_view import _LiveView
from pythinker_code.wire.types import StatusUpdate


@pytest.fixture
def live_view() -> _LiveView:
    return _LiveView(StatusUpdate())


def test_stream_deltas_mark_dirty_without_immediate_refresh(live_view: _LiveView) -> None:
    live_view.refresh_soon()
    assert live_view._dirty is True
    assert live_view._need_recompose is True
    assert live_view._force_refresh is False


@pytest.mark.asyncio
async def test_frame_scheduler_coalesces_multiple_deltas(live_view: _LiveView) -> None:
    live = MagicMock()
    live_view.refresh_soon()
    live_view.refresh_soon()
    live_view.refresh_soon()

    with patch.object(live_view, "compose", return_value=Text("composed")):
        task = asyncio.create_task(live_view._frame_refresh_loop(live))
        await asyncio.sleep(0.06)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert live.update.call_count >= 1
    assert live.update.call_args.kwargs.get("refresh") is False
    assert live_view._dirty is False


def test_preview_uses_plain_text_not_markdown() -> None:
    block = _ContentBlock(is_think=False, paced=True)
    block.append("plain streaming preview line")
    block.reveal_all()
    composed = block.compose()
    rec = Console(record=True, width=100, color_system=None)
    rec.print(composed)
    output = rec.export_text()
    assert "plain streaming preview line" in output

    def _contains_markdown_widget(renderable: object) -> bool:
        if isinstance(renderable, PythinkerMarkdown):
            return True
        if isinstance(renderable, Group):
            return any(_contains_markdown_widget(child) for child in renderable.renderables)
        return False

    assert not _contains_markdown_widget(composed)


def test_no_console_print_inside_live_context() -> None:
    block = _ContentBlock(is_think=False)
    block.append("First paragraph.\n\nSecond paragraph.\n\nThird.")
    assert block._committed_renderables
    assert all(not isinstance(r, str) for r in block._committed_renderables)


def test_final_output_matches_committed_render() -> None:
    block = _ContentBlock(is_think=False)
    block.append("Alpha paragraph.\n\nBeta paragraph.\n\nGamma tail.")
    final = block.compose_final()
    promoted = block.promote_to_scrollback()
    assert promoted is not None
    rec = Console(record=True, width=100, color_system=None)
    rec.print(final)
    rec2 = Console(record=True, width=100, color_system=None)
    rec2.print(promoted)
    assert rec.export_text() == rec2.export_text()


def test_reduced_motion_disables_blinking(monkeypatch: pytest.MonkeyPatch) -> None:
    from pythinker_code.ui.shell.motion import streaming_caret_visible

    monkeypatch.setattr(
        "pythinker_code.ui.shell.motion.reduced_motion_enabled",
        lambda: True,
    )
    assert streaming_caret_visible() is True


def test_streaming_caret_reserves_width_when_hidden() -> None:
    from rich.cells import cell_len

    from pythinker_code.ui.shell.motion import append_streaming_caret, streaming_caret_visible

    visible = Text("hello")
    append_streaming_caret(visible, now=0.0 if streaming_caret_visible(0.0) else 999.0)
    hidden = Text("hello")
    append_streaming_caret(hidden, now=999.0 if not streaming_caret_visible(999.0) else 0.0)
    assert cell_len(visible.plain) == cell_len(hidden.plain)


def test_no_color_keeps_plain_status_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    block = _ContentBlock(is_think=False, paced=True)
    block.append("streaming")
    block.reveal_tick()
    composed = block.compose()
    rec = Console(record=True, width=80, color_system=None, force_terminal=False)
    rec.print(composed)
    assert "Composing" in rec.export_text()


def test_unclosed_code_fence_does_not_commit_markdown() -> None:
    block = _ContentBlock(is_think=False)
    block.append("Before.\n\n```python\ndef foo():\n    pass")
    pending = block._pending_text()
    assert "```python" in pending
    assert "def foo" in pending
    assert block._committed_len <= len("Before.\n\n")


def test_long_code_block_does_not_reparse_per_tick() -> None:
    _markdown_commit_boundary_cached.cache_clear()
    fence = "```python\n" + "\n".join(f"x = {i}" for i in range(120)) + "\n```\n\nAfter.\n"
    text = "Intro.\n\n" + fence
    first = markdown_commit_boundary(text)
    with patch("pythinker_code.ui.shell.markdown.streaming._get_md_parser") as parser_factory:
        parser_factory.side_effect = AssertionError("parse should be cached")
        second = markdown_commit_boundary(text)
    assert first == second


def test_streaming_caret_appended_during_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pythinker_code.ui.shell.motion.streaming_caret_visible",
        lambda now=None: True,
    )
    block = _ContentBlock(is_think=False, paced=True)
    block.append("hello")
    block.reveal_all()
    composed = block.compose()
    assert isinstance(composed, Group)
    rec = Console(record=True, width=80, color_system=None)
    rec.print(composed)
    assert STREAMING_CARET_GLYPH in rec.export_text()
