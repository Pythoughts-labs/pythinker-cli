"""Paced reveal of streamed composing text (smooth streaming).

Bursty LLM deltas are buffered and revealed gradually by ``reveal_tick`` so text
flows smoothly instead of landing in delta-sized clumps, while keeping up with a
fast model. Unpaced and thinking blocks reveal immediately (legacy behavior).
"""

from __future__ import annotations

import builtins
import io

import pytest

from pythinker_code.ui.shell.visualize._blocks import (
    FlushReason,
    _ContentBlock,
    set_smooth_streaming,
    smooth_streaming_enabled,
)

# Single markdown block (no committable boundary) so reveal never triggers a
# console.print() commit during the test.
_TEXT = "the quick brown fox jumps over the lazy dog several times in a row"


@pytest.fixture(autouse=True)
def _restore_smooth_streaming_flag():
    """Keep the process-global smooth-streaming flag isolated for future tests."""
    previous = smooth_streaming_enabled()
    try:
        yield
    finally:
        set_smooth_streaming(previous)


def _drain(block: _ContentBlock) -> None:
    """Tick until fully revealed (bounded loop guards against a stuck cursor)."""
    for _ in range(10_000):
        if not block.reveal_tick():
            return
    raise AssertionError("reveal_tick did not converge")


def test_paced_block_buffers_until_ticked() -> None:
    block = _ContentBlock(is_think=False, paced=True)
    block.append(_TEXT)
    # Nothing is revealed until a tick fires.
    assert block._revealed_len == 0
    assert block._pending_text() == ""

    assert block.reveal_tick() is True
    assert 0 < block._revealed_len < len(_TEXT)


def test_paced_reveal_is_monotonic_and_bounded() -> None:
    block = _ContentBlock(is_think=False, paced=True)
    block.append(_TEXT)
    last = 0
    for _ in range(50):
        block.reveal_tick()
        assert last <= block._revealed_len <= len(block.raw_text)
        last = block._revealed_len
    assert block._revealed_len == len(_TEXT)


def test_paced_reveal_step_is_capped_per_tick(monkeypatch) -> None:
    """A large backlog reveals as an even flow, not one lurch (normal motion)."""
    from rich.cells import cell_len

    from pythinker_code.ui.shell.visualize import _blocks
    from pythinker_code.ui.shell.visualize._blocks import _STREAM_REVEAL_MAX_CELLS

    # Force normal motion so the smoothing cap applies (test env may disable motion).
    monkeypatch.setattr(_blocks, "reduced_motion_enabled", lambda: False)

    block = _ContentBlock(is_think=False, paced=True)
    block.append("x" * 1000)  # 1000 single-cell chars => large backlog

    before = block._revealed_len
    assert block.reveal_tick() is True
    revealed_cells = cell_len(block.raw_text[before : block._revealed_len])
    assert revealed_cells <= _STREAM_REVEAL_MAX_CELLS


def test_paced_reveal_reduced_motion_ignores_cap(monkeypatch) -> None:
    """Reduced motion still drains fast (>= half the backlog), bypassing the cap."""
    from rich.cells import cell_len

    from pythinker_code.ui.shell.visualize import _blocks
    from pythinker_code.ui.shell.visualize._blocks import _STREAM_REVEAL_MAX_CELLS

    monkeypatch.setattr(_blocks, "reduced_motion_enabled", lambda: True)

    block = _ContentBlock(is_think=False, paced=True)
    block.append("x" * 1000)

    before = block._revealed_len
    block.reveal_tick()
    revealed_cells = cell_len(block.raw_text[before : block._revealed_len])
    assert revealed_cells > _STREAM_REVEAL_MAX_CELLS


def test_paced_reveal_advances_by_display_cells_for_cjk() -> None:
    from rich.cells import cell_len

    block = _ContentBlock(is_think=False, paced=True)
    block.append("你好")

    assert block.reveal_tick() is True
    revealed = block.raw_text[: block._revealed_len]
    assert revealed == "你"
    assert cell_len(revealed) == 2


def test_paced_reveal_eventually_shows_all_text() -> None:
    block = _ContentBlock(is_think=False, paced=True)
    block.append(_TEXT)
    _drain(block)
    assert block._revealed_len == len(_TEXT)
    # No text is stranded: committed prefix + revealed pending == full buffer.
    assert block.raw_text[: block._committed_len] + block._pending_text() == _TEXT


def test_reveal_all_reveals_everything() -> None:
    block = _ContentBlock(is_think=False, paced=True)
    block.append(_TEXT)
    block.reveal_tick()
    assert block._revealed_len < len(_TEXT)

    assert block.reveal_all() is True
    assert block._revealed_len == len(_TEXT)
    # Already revealed -> no further change.
    assert block.reveal_all() is False


def test_unpaced_block_reveals_immediately() -> None:
    block = _ContentBlock(is_think=False, paced=False)
    block.append(_TEXT)
    assert block._revealed_len == len(_TEXT)
    # Unpaced blocks ignore the reveal tick entirely.
    assert block.reveal_tick() is False


def test_thinking_block_is_never_paced() -> None:
    # paced=True is requested, but thinking blocks opt out (text reveals at once).
    block = _ContentBlock(is_think=True, paced=True)
    block.append(_TEXT)
    assert block._revealed_len == len(_TEXT)
    assert block.reveal_tick() is False


_LONG_TEXT = _TEXT * 12


def test_tool_transition_drains_large_backlog_without_revealing_all() -> None:
    block = _ContentBlock(is_think=False, paced=True)
    block.append(_LONG_TEXT)
    block.reveal_tick()
    assert block._revealed_len < len(block.raw_text)
    revealed_before = block._revealed_len

    block.prepare_for_finalize(FlushReason.TOOL_START)

    assert revealed_before < block._revealed_len < len(block.raw_text)


def test_turn_end_reveals_all_backlog() -> None:
    block = _ContentBlock(is_think=False, paced=True)
    block.append(_LONG_TEXT)
    block.reveal_tick()
    block.prepare_for_finalize(FlushReason.TURN_END)
    assert block._revealed_len == len(block.raw_text)


def test_small_backlog_drains_on_tool_transition() -> None:
    block = _ContentBlock(is_think=False, paced=True)
    block.append("short backlog")
    block.prepare_for_finalize(FlushReason.TOOL_START)
    assert block._revealed_len == len(block.raw_text)


def test_reveal_tick_advances_without_newline_boundary() -> None:
    """Without a newline, reveal_tick must still advance the reveal cursor
    but cannot commit a markdown block. The block stays paced and reveals a
    bounded slice per tick — observable via the public ``_revealed_len``
    and the absence of any committed prefix.
    """
    block = _ContentBlock(is_think=False, paced=True)
    block.append("word " * 400)

    for _ in range(8):
        block.reveal_tick()

    # Pacing advances without a markdown break — revealed cursor is between
    # zero and the full backlog, and no prefix has been committed yet.
    assert 0 < block._revealed_len < len(block.raw_text)
    assert block._committed_len == 0


def test_flush_content_does_not_write_hidden_debug_log(monkeypatch) -> None:
    from unittest.mock import patch

    from pythinker_code.ui.shell.visualize._live_view import _LiveView
    from pythinker_code.wire.types import StatusUpdate

    opened_paths: list[str] = []
    original_open = builtins.open

    def record_open(file, *args, **kwargs):  # noqa: ANN001
        opened_paths.append(str(file))
        if str(file).endswith(".log"):
            return io.StringIO()
        return original_open(file, *args, **kwargs)

    monkeypatch.delenv("PYTHINKER_DEBUG_STREAM_PACING", raising=False)

    view = _LiveView(StatusUpdate())
    block = _ContentBlock(is_think=False, paced=True)
    block.append(_TEXT)
    view._current_content_block = block

    with (
        monkeypatch.context() as ctx,
        patch("pythinker_code.ui.shell.visualize._live_view.emit_scrollback_block"),
    ):
        ctx.setattr(builtins, "open", record_open)
        view.flush_content(FlushReason.TURN_END)

    assert not any("debug-e13c80.log" in path for path in opened_paths)


def test_tool_flush_preserves_full_paced_backlog_in_scrollback() -> None:
    """Transition flush must promote all raw text, not only the revealed slice."""
    from unittest.mock import patch

    from rich.console import Console

    from pythinker_code.ui.shell.visualize._live_view import _LiveView
    from pythinker_code.wire.types import StatusUpdate

    view = _LiveView(StatusUpdate())
    block = _ContentBlock(is_think=False, paced=True)
    block.append(_LONG_TEXT)
    block.reveal_tick()
    assert block._revealed_len < len(block.raw_text)
    view._current_content_block = block

    printed: list[object] = []
    with patch(
        "pythinker_code.ui.shell.visualize._live_view.emit_scrollback_block",
        side_effect=lambda _console, renderable: printed.append(renderable),
    ):
        view.flush_content(FlushReason.TOOL_START)

    assert view._current_content_block is None
    assert block._revealed_len < len(block.raw_text)
    assert len(printed) == 1
    rec = Console(record=True, width=120, color_system=None)
    rec.print(printed[0])
    output = rec.export_text()
    # Full raw text must appear (line wraps may insert newlines in export).
    normalized = "".join(output.split())
    assert "".join(_LONG_TEXT.split()) in normalized


def test_flush_content_promotes_full_streaming_backlog_to_scrollback() -> None:
    """Finalizing a paced block at TURN_END must surface the full raw text
    to scrollback, not only the revealed slice, even when the second
    paragraph is still being streamed.
    """
    from unittest.mock import patch

    from rich.console import Console

    from pythinker_code.ui.shell.visualize._live_view import _LiveView
    from pythinker_code.wire.types import StatusUpdate

    raw = "First paragraph.\n\nSecond paragraph still streaming."

    view = _LiveView(StatusUpdate())
    block = _ContentBlock(is_think=False, paced=True)
    block.append(raw)
    block.reveal_tick()
    # Confirm the test pre-condition: only the first paragraph is revealed.
    assert block._revealed_len < len(raw)
    view._current_content_block = block

    printed: list[object] = []
    with patch(
        "pythinker_code.ui.shell.visualize._live_view.emit_scrollback_block",
        side_effect=lambda _console, renderable: printed.append(renderable),
    ):
        view.flush_content(FlushReason.TURN_END)

    assert view._current_content_block is None
    assert len(printed) == 1
    rec = Console(record=True, width=120, color_system=None)
    rec.print(printed[0])
    output = rec.export_text()
    # Full raw text must appear in the finalized scrollback.
    normalized = "".join(output.split())
    assert "".join(raw.split()) in normalized


def test_live_view_enables_pacing_from_smooth_streaming_flag(monkeypatch) -> None:
    import importlib

    live_view_module = importlib.import_module("pythinker_code.ui.shell.visualize._live_view")
    _LiveView = live_view_module._LiveView
    from pythinker_code.wire.types import StatusUpdate, TextPart

    set_smooth_streaming(True)
    monkeypatch.setattr(live_view_module, "reduced_motion_enabled", lambda: False)

    view = _LiveView(StatusUpdate())
    view.append_content(TextPart(text=_TEXT))

    block = view._current_content_block
    assert block is not None
    assert block._paced is True
    assert block._revealed_len == 0


def test_composing_preview_shows_hidden_rows_marker_when_budget_trims() -> None:
    from pythinker_code.ui.shell.console import render_to_ansi
    from pythinker_code.ui.shell.spacing import PREAMBLE_EARLIER_OUTPUT_HIDDEN_HINT

    block = _ContentBlock(is_think=False, paced=False)
    for index in range(6):
        block.append(f"Paragraph {index} with enough prose to consume vertical space.\n\n")
    block.append("live tail")
    block.set_preview_row_budget(6)
    ansi = render_to_ansi(block.compose(include_activity=False), columns=80)

    assert PREAMBLE_EARLIER_OUTPUT_HIDDEN_HINT in ansi
    assert "live tail" in ansi


def test_handoff_trace_noop_when_env_unset(tmp_path, monkeypatch) -> None:
    """The diagnostic tracer writes nothing (and never raises) when disabled."""
    from pythinker_code.ui.shell.visualize._interactive import _handoff_trace

    monkeypatch.delenv("PYTHINKER_TUI_HANDOFF_LOG", raising=False)
    _handoff_trace("HANDOFF\tprose_commit(1)")  # must be a no-op, no file created
    assert list(tmp_path.iterdir()) == []


def test_handoff_trace_appends_events_when_enabled(tmp_path, monkeypatch) -> None:
    """When enabled, each call appends one tab-delimited timeline line."""
    from pythinker_code.ui.shell.visualize._interactive import _handoff_trace

    log = tmp_path / "handoff.log"
    monkeypatch.setenv("PYTHINKER_TUI_HANDOFF_LOG", str(log))
    _handoff_trace("TRANSITION\tTOOL_START")
    _handoff_trace("HANDOFF\tprose_commit(2)")
    _handoff_trace("TURN_END")

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    events = [line.split("\t", 1)[1] for line in lines]
    assert events == ["TRANSITION\tTOOL_START", "HANDOFF\tprose_commit(2)", "TURN_END"]
