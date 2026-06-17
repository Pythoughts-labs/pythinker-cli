"""Diff-based live-region renderer for the shell TUI.

Updates only changed terminal rows in place instead of repainting the full
Rich ``Live`` frame on every streaming tick.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, TextIO, cast

from rich.console import Console, RenderHook
from rich.control import Control
from rich.file_proxy import FileProxy
from rich.segment import ControlType, Segment

if TYPE_CHECKING:
    from rich.console import ConsoleRenderable, RenderableType


@dataclass(frozen=True)
class _RenderedLine:
    text: str
    cell_length: int


def _first_different_line(
    old_lines: list[_RenderedLine],
    lines: list[_RenderedLine],
) -> int:
    first_diff = 0
    shared = min(len(old_lines), len(lines))
    while first_diff < shared and old_lines[first_diff] == lines[first_diff]:
        first_diff += 1
    return first_diff


def _should_rewrite_growing_last_line(
    old_lines: list[_RenderedLine],
    lines: list[_RenderedLine],
    first_diff: int,
) -> bool:
    return bool(old_lines and len(lines) > len(old_lines) and first_diff == len(old_lines) - 1)


class DiffLive(RenderHook):
    """Minimal live-region renderer that updates changed lines in place."""

    def __init__(
        self,
        renderable: RenderableType | None = None,
        *,
        console: Console,
        transient: bool = False,
        redirect_stdout: bool = True,
        redirect_stderr: bool = True,
        get_renderable: Callable[[], RenderableType] | None = None,
    ) -> None:
        self.console = console
        self.transient = transient
        self._renderable = renderable
        self._get_renderable = get_renderable
        self._started = False
        self._lines: list[_RenderedLine] = []
        self._is_interactive = self.console.is_terminal
        self._nested = False
        self._redirect_stdout = redirect_stdout
        self._redirect_stderr = redirect_stderr
        self._restore_stdout: IO[str] | None = None
        self._restore_stderr: IO[str] | None = None
        self._console_state_active = False
        self._cursor_below_frame = False
        self._frame_truncated = False

    def __enter__(self) -> DiffLive:
        if not self._started:
            self._started = True
            if self._is_interactive:
                if not self.console.set_live(self):  # pyright: ignore[reportArgumentType]
                    self._nested = True
                    return self
                self.console.show_cursor(False)
                self._enable_redirect_io()
                self.console.push_render_hook(self)
                self._console_state_active = True
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()

    def start(self) -> None:
        """Re-enter the live region after ``stop()`` (pager pause/resume)."""
        if not self._started:
            self.__enter__()

    def update(self, renderable: RenderableType, *, refresh: bool = False) -> None:
        self._renderable = renderable
        if refresh:
            self.refresh()

    def refresh(self) -> None:
        if not self._started:
            self.__enter__()
        renderable = self.get_renderable()
        if renderable is None:
            return
        if not self._is_interactive:
            return
        lines = self._render_lines(renderable)

        max_visible = self.console.size.height
        self._frame_truncated = False
        if max_visible > 0:
            if len(lines) > max_visible:
                self._frame_truncated = True
                lines = lines[-max_visible:]
            if len(self._lines) > max_visible:
                self._lines = self._lines[-max_visible:]

        if not self._lines:
            self._write_initial(lines)
        else:
            self._write_diff(lines)
        self._lines = lines

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._started = False
            if self._is_interactive:
                self._stop_interactive()
            else:
                self._print_current_renderable()
        finally:
            self._restore_console_state()
            self._nested = False
            self._frame_truncated = False

    def _print_current_renderable(self) -> None:
        renderable = self.get_renderable()
        if renderable is not None:
            self.console.print(renderable)

    def _stop_interactive(self) -> None:
        self.console.clear_live()
        if self._nested:
            if not self.transient:
                self._print_current_renderable()
            return
        if self._lines:
            self._stop_drawn_frame()

    def _stop_drawn_frame(self) -> None:
        if self.transient:
            self._clear_region()
            return
        if self._frame_truncated:
            self._clear_region()
            renderable = self.get_renderable()
            if renderable is not None:
                self.console.print(renderable)
            else:
                self._write("\n")
            return
        if not self._cursor_below_frame:
            self._write("\n")

    def _restore_console_state(self) -> None:
        if not self._is_interactive or not self._console_state_active:
            return
        self._disable_redirect_io()
        self.console.pop_render_hook()
        self.console.show_cursor(True)
        self._console_state_active = False

    def get_renderable(self) -> RenderableType | None:
        if self._get_renderable is not None:
            return self._get_renderable()
        return self._renderable

    def process_renderables(
        self,
        renderables: list[ConsoleRenderable],
    ) -> list[ConsoleRenderable]:
        if not self._is_interactive or not self._started or self._nested:
            return renderables
        renderable = self.get_renderable()
        if renderable is None:
            return renderables
        if isinstance(renderable, str):
            current_renderable: ConsoleRenderable = self.console.render_str(renderable)
        else:
            current_renderable = cast("ConsoleRenderable", renderable)
        self._cursor_below_frame = True
        return [self._position_cursor_control(), *renderables, current_renderable]

    def _render_lines(self, renderable: RenderableType) -> list[_RenderedLine]:
        options = self.console.options.update(width=self.console.size.width)
        rendered_lines = self.console.render_lines(renderable, options=options, pad=False)
        return [
            _RenderedLine(
                text=self.console._render_buffer(line),  # pyright: ignore[reportPrivateUsage]
                cell_length=Segment.get_line_length(line),
            )
            for line in rendered_lines
        ]

    def _write_initial(self, lines: list[_RenderedLine]) -> None:
        if not lines:
            return
        payload_parts: list[str] = []
        for index, line in enumerate(lines):
            if index:
                payload_parts.append(self._scroll_newline())
            payload_parts.append(line.text)
        payload_parts.append(str(Control.move_to_column(0)))
        payload = "".join(payload_parts)
        self._write(payload)
        self._cursor_below_frame = False

    def _write_diff(self, lines: list[_RenderedLine]) -> None:
        old_lines = self._lines
        first_diff = _first_different_line(old_lines, lines)
        if first_diff == len(old_lines) == len(lines):
            return
        if first_diff == len(old_lines) and len(lines) > len(old_lines):
            self._write_appended_lines(lines[first_diff:])
            return
        if _should_rewrite_growing_last_line(old_lines, lines, first_diff):
            self._rewrite_growing_last_line(old_lines[-1], lines[first_diff:])
            return

        max_height = max(len(old_lines), len(lines))
        current_row = self._current_diff_cursor_row(old_lines)
        payload: list[str] = [self._move_to_line_start(first_diff - current_row)]
        last_old_row = len(old_lines) - 1

        for row in range(first_diff, max_height):
            new_line = lines[row] if row < len(lines) else None
            old_line = old_lines[row] if row < len(old_lines) else None
            self._append_diff_row(payload, new_line, old_line)

            if row < max_height - 1:
                self._append_diff_row_transition(payload, row, last_old_row)

        target_row = len(lines) - 1
        payload.append(self._move_to_line_start(target_row - (max_height - 1)))
        self._write("".join(payload))
        self._cursor_below_frame = False

    def _current_diff_cursor_row(self, old_lines: list[_RenderedLine]) -> int:
        current_row = len(old_lines) - 1
        if self._cursor_below_frame:
            return current_row + 1
        return current_row

    @staticmethod
    def _append_diff_row(
        payload: list[str],
        new_line: _RenderedLine | None,
        old_line: _RenderedLine | None,
    ) -> None:
        if new_line is None:
            payload.append(str(Control((ControlType.ERASE_IN_LINE, 2))))
            return
        payload.append(new_line.text)
        if old_line is not None and old_line.cell_length > new_line.cell_length:
            payload.append(str(Control((ControlType.ERASE_IN_LINE, 0))))

    def _append_diff_row_transition(
        self,
        payload: list[str],
        row: int,
        last_old_row: int,
    ) -> None:
        next_row = row + 1
        if row >= last_old_row or next_row > last_old_row:
            payload.append(self._scroll_newline())
            return
        payload.append(self._move_to_line_start(1))

    def _write_appended_lines(self, lines: list[_RenderedLine]) -> None:
        if not lines:
            return
        payload_parts: list[str] = []
        if self._cursor_below_frame:
            payload_parts.append(str(Control.move_to_column(0)))
            payload_parts.append(lines[0].text)
            remaining_lines = lines[1:]
        else:
            remaining_lines = lines
        for line in remaining_lines:
            payload_parts.append(self._scroll_newline())
            payload_parts.append(line.text)
        payload_parts.append(str(Control.move_to_column(0)))
        payload = "".join(payload_parts)
        self._write(payload)
        self._cursor_below_frame = False

    def _rewrite_growing_last_line(
        self,
        old_last_line: _RenderedLine,
        new_lines: list[_RenderedLine],
    ) -> None:
        if not new_lines:
            return
        row_delta = -1 if self._cursor_below_frame else 0
        payload = [self._move_to_line_start(row_delta), new_lines[0].text]
        if old_last_line.cell_length > new_lines[0].cell_length:
            payload.append(str(Control((ControlType.ERASE_IN_LINE, 0))))
        for line in new_lines[1:]:
            payload.append(self._scroll_newline())
            payload.append(line.text)
        payload.append(str(Control.move_to_column(0)))
        self._write("".join(payload))
        self._cursor_below_frame = False

    def _clear_region(self) -> None:
        height = len(self._lines)
        if height <= 0:
            return
        cursor_row = height - 1
        if self._cursor_below_frame:
            cursor_row += 1
        payload = [self._move_to_line_start(-cursor_row)]
        for row in range(height):
            payload.append(str(Control((ControlType.ERASE_IN_LINE, 2))))
            if row < height - 1:
                payload.append(self._move_to_line_start(1))
        payload.append(self._move_to_line_start(-(height - 1)))
        self._write("".join(payload))
        self._lines = []
        self._cursor_below_frame = False

    def _move_to_line_start(self, row_delta: int) -> str:
        return str(Control.move_to_column(0, y=row_delta))

    def _scroll_newline(self) -> str:
        return f"{Control.move_to_column(0)}\n"

    def _position_cursor_control(self) -> Control:
        height = len(self._lines)
        if height <= 0:
            return Control()
        lines_to_rewind = height - 1
        if self._cursor_below_frame:
            lines_to_rewind += 1
        return Control(
            ControlType.CARRIAGE_RETURN,
            (ControlType.ERASE_IN_LINE, 2),
            *(((ControlType.CURSOR_UP, 1), (ControlType.ERASE_IN_LINE, 2)) * lines_to_rewind),
        )

    def _write(self, text: str) -> None:
        if not text:
            return
        with self.console._lock:  # pyright: ignore[reportPrivateUsage]
            self.console.file.write(text)
            self.console.file.flush()

    def _enable_redirect_io(self) -> None:
        if not self._is_interactive:
            return
        if self._redirect_stdout and not isinstance(sys.stdout, FileProxy):
            self._restore_stdout = sys.stdout
            sys.stdout = cast("TextIO", FileProxy(self.console, sys.stdout))
        if self._redirect_stderr and not isinstance(sys.stderr, FileProxy):
            self._restore_stderr = sys.stderr
            sys.stderr = cast("TextIO", FileProxy(self.console, sys.stderr))

    def _disable_redirect_io(self) -> None:
        if self._restore_stdout:
            sys.stdout = cast("TextIO", self._restore_stdout)
            self._restore_stdout = None
        if self._restore_stderr:
            sys.stderr = cast("TextIO", self._restore_stderr)
            self._restore_stderr = None
