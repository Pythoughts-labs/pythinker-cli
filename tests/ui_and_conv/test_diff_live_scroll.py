"""Geometry-safe scroll vs cursor-down transitions in DiffLive."""

from __future__ import annotations

import io
from typing import cast

import pytest
from rich.console import Console, ConsoleDimensions
from rich.control import Control
from rich.text import Text

from pythinker_code.ui.shell.visualize._diff_live import (
    DiffLive,
    _diff_live_trace,
    _RenderedLine,
)


def _make_console(*, height: int, width: int = 80) -> Console:
    console = Console(
        file=io.StringIO(),
        width=width,
        height=height,
        force_terminal=True,
        color_system=None,
        legacy_windows=False,
    )
    object.__setattr__(console, "size", ConsoleDimensions(width=width, height=height))
    return console


def _set_console_height(console: Console, *, height: int, width: int = 80) -> None:
    object.__setattr__(console, "size", ConsoleDimensions(width=width, height=height))


def _line(text: str) -> _RenderedLine:
    return _RenderedLine(text=text, cell_length=len(text))


def _make_live(*, height: int, origin: int | None = 0) -> tuple[DiffLive, Console]:
    console = _make_console(height=height)
    live = DiffLive(console=console, transient=True)
    live._is_interactive = True
    live._started = True
    live._frame_origin_row = origin
    live._last_terminal_height = height
    return live, console


def _payload(console: Console) -> str:
    return cast("io.StringIO", console.file).getvalue()


def _cud_bytes() -> str:
    return str(Control.move_to_column(0, y=1))


def _scroll_bytes() -> str:
    return f"{Control.move_to_column(0)}\n"


@pytest.mark.parametrize(
    ("origin", "height", "target_row", "expect_cud", "expect_scroll"),
    [
        (0, 40, 5, True, False),
        (35, 40, 5, False, True),
        (None, 40, 5, False, True),
        (0, 40, 40, False, True),
    ],
)
def test_append_row_transition_geometry(
    origin: int | None,
    height: int,
    target_row: int,
    *,
    expect_cud: bool,
    expect_scroll: bool,
) -> None:
    live, console = _make_live(height=height, origin=origin)
    payload_parts: list[str] = []
    live._append_row_transition(payload_parts, target_row)
    live._write("".join(payload_parts))

    payload = _payload(console)
    if expect_cud:
        assert _cud_bytes() in payload
    else:
        assert _cud_bytes() not in payload
    if expect_scroll:
        assert _scroll_bytes() in payload
    else:
        assert _scroll_bytes() not in payload


def test_write_appended_lines_uses_cud_when_rows_fit() -> None:
    live, console = _make_live(height=40, origin=0)
    live._lines = [_line("seed")]

    live._write_appended_lines(
        [_line("a"), _line("b"), _line("c"), _line("d")],
        base_row=1,
    )

    payload = _payload(console)
    assert _cud_bytes() in payload
    assert _scroll_bytes() not in payload


def test_write_appended_lines_scrolls_when_near_bottom() -> None:
    live, console = _make_live(height=40, origin=35)
    live._lines = [_line("seed")]

    live._write_appended_lines([_line("overflow")], base_row=5)

    payload = _payload(console)
    assert _scroll_bytes() in payload
    assert _cud_bytes() not in payload


def test_row_fits_without_scroll_unknown_origin_fails_closed() -> None:
    live, _console = _make_live(height=40, origin=None)
    assert live._row_fits_without_scroll(5) is False


def test_row_fits_without_scroll_near_bottom() -> None:
    live, _console = _make_live(height=40, origin=35)
    assert live._row_fits_without_scroll(4) is True
    assert live._row_fits_without_scroll(5) is False


def test_bump_origin_after_scroll_updates_fit() -> None:
    live, console = _make_live(height=40, origin=35)
    live._lines = [_line("seed")]

    # target 5 does not fit at origin 35 (35+5=40).
    live._write_appended_lines([_line("a")], base_row=5)
    first_payload = _payload(console)
    assert _scroll_bytes() in first_payload
    assert live._frame_origin_row == 34

    cast("io.StringIO", console.file).seek(0)
    cast("io.StringIO", console.file).truncate(0)
    live._lines = [_line("seed"), _line("a")]
    # After one scroll, origin 34 + target 5 = 39 < 40 → CUD allowed.
    live._write_appended_lines([_line("b")], base_row=5)
    second_payload = _payload(console)
    assert _cud_bytes() in second_payload
    assert _scroll_bytes() not in second_payload


def test_height_shrink_re_evaluates_fit() -> None:
    live, console = _make_live(height=40, origin=0)
    live._lines = [_line("seed")]

    live._write_appended_lines([_line("grow")], base_row=1)
    assert _cud_bytes() in _payload(console)

    _set_console_height(console, height=30)
    live._last_terminal_height = 30
    cast("io.StringIO", console.file).seek(0)
    cast("io.StringIO", console.file).truncate(0)
    live._lines = [_line("seed"), _line("grow")]

    live._write_appended_lines([_line("more")], base_row=2)
    payload = _payload(console)
    # origin 0 + target 2 = 2 < 30 → still fits with CUD.
    assert _cud_bytes() in payload
    assert _scroll_bytes() not in payload


def test_rewrite_growing_last_line_uses_geometry_gate() -> None:
    live, console = _make_live(height=40, origin=35)
    live._lines = [_line("last")]

    live._rewrite_growing_last_line(
        _line("last"),
        [_line("last"), _line("extra")],
    )

    payload = _payload(console)
    # target row 1: 35+1=36 < 40 → CUD for the extra line.
    assert _cud_bytes() in payload
    assert _scroll_bytes() not in payload


def test_append_diff_row_transition_growth_branch() -> None:
    live, console = _make_live(height=40, origin=0)
    live._lines = [_line("a"), _line("b")]

    payload_parts: list[str] = []
    live._append_diff_row_transition(payload_parts, row=1, last_old_row=1)
    live._write("".join(payload_parts))

    payload = _payload(console)
    assert _cud_bytes() in payload
    assert _scroll_bytes() not in payload


def test_diff_live_trace_noop_when_env_unset(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("PYTHINKER_DIFF_LIVE_LOG", raising=False)
    _diff_live_trace("DIFF_LIVE\tinitial\tlines=1\theight=40\torigin=0")
    assert not any(tmp_path.iterdir())


def test_diff_live_trace_appends_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    log = tmp_path / "diff-live.log"
    monkeypatch.setenv("PYTHINKER_DIFF_LIVE_LOG", str(log))
    _diff_live_trace("DIFF_LIVE\tgrowth\tlines=5\theight=40\torigin=0")
    _diff_live_trace("DIFF_LIVE\tinitial\tlines=1\theight=40\torigin=None")
    text = log.read_text(encoding="utf-8")
    assert "DIFF_LIVE\tgrowth" in text
    assert "DIFF_LIVE\tinitial" in text
    assert text.count("\n") == 2


def test_row_fits_rejects_negative_target_row() -> None:
    live, _console = _make_live(height=40, origin=0)
    assert live._row_fits_without_scroll(-1) is False


def test_bump_origin_after_scroll_invalidates_when_exhausted() -> None:
    live, console = _make_live(height=40, origin=0)
    live._frame_origin_row = 0
    for _ in range(2):
        live._bump_origin_after_scroll()
    assert live._frame_origin_row is None

    payload_parts: list[str] = []
    live._append_row_transition(payload_parts, 1)
    live._write("".join(payload_parts))
    assert _scroll_bytes() in _payload(console)


def test_ensure_frame_origin_rejects_out_of_range_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = _make_console(height=40)
    live = DiffLive(console=console, transient=True)
    live._is_interactive = True
    monkeypatch.setattr("pythinker_code.utils.term.get_cursor_row", lambda: 50)
    live._ensure_frame_origin_row()
    assert live._frame_origin_row is None


def test_refresh_clears_origin_when_height_shrinks_below_it() -> None:
    console = _make_console(height=40)
    live = DiffLive(console=console, transient=True)
    live._is_interactive = True
    live._started = True
    live._frame_origin_row = 35
    live._last_terminal_height = 40
    live._lines = [_line("seed")]
    live._renderable = Text("seed\nmore")

    _set_console_height(console, height=30)
    live.refresh()

    assert live._frame_origin_row is None


def test_refresh_logs_growth_when_env_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    log = tmp_path / "diff-live.log"
    monkeypatch.setenv("PYTHINKER_DIFF_LIVE_LOG", str(log))
    monkeypatch.setattr(
        "pythinker_code.utils.term.get_cursor_row",
        lambda: 1,
    )

    console = _make_console(height=40)
    live = DiffLive(console=console, transient=True)
    live._is_interactive = True
    live._started = True
    live._renderable = Text("one")
    live.refresh()
    live._renderable = Text("one\ntwo")
    live.refresh()

    text = log.read_text(encoding="utf-8")
    assert "DIFF_LIVE\tinitial" in text
    assert "DIFF_LIVE\tgrowth" in text
