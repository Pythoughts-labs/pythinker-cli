from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pythinker_code.ui.shell.components.render_utils import truncate_to_width

FileActivityStatus = Literal["writing", "updated", "failed"]


@dataclass(slots=True)
class TranscriptRow:
    kind: str
    title: str
    detail: str = ""
    expandable: bool = False
    done: bool = False


@dataclass(slots=True)
class FileActivity:
    path: str
    status: FileActivityStatus


class FocusTuiModel:
    def __init__(self) -> None:
        self.prompt = ""
        self.file_shelf_open = False
        self._rows: dict[str, TranscriptRow] = {}
        self._row_order: list[str] = []
        self._files: dict[str, FileActivityStatus] = {}
        self._file_order: list[str] = []

    def begin_turn(self, prompt: str) -> None:
        self.prompt = prompt
        self.file_shelf_open = False
        self._rows.clear()
        self._row_order.clear()
        self._files.clear()
        self._file_order.clear()

    def append_tool_row(
        self,
        tool_id: str,
        title: str,
        detail: str = "",
        expandable: bool = False,
    ) -> None:
        if tool_id not in self._rows:
            self._row_order.append(tool_id)
        self._rows[tool_id] = TranscriptRow(
            kind="tool",
            title=title,
            detail=detail,
            expandable=expandable,
        )

    def update_tool_row(
        self,
        tool_id: str,
        *,
        detail: str | None = None,
        done: bool | None = None,
    ) -> None:
        row = self._rows.get(tool_id)
        if row is None:
            return
        if detail is not None:
            row.detail = detail
        if done is not None:
            row.done = done

    def mark_file(self, path: str, status: FileActivityStatus) -> None:
        clean = path.strip()
        if not clean:
            return
        if clean not in self._files:
            self._file_order.append(clean)
        self._files[clean] = status

    def toggle_files(self) -> None:
        self.file_shelf_open = not self.file_shelf_open

    def visible_file_count(self) -> int:
        return len(self._files)

    def render_rows(self, width: int) -> list[str]:
        rows: list[str] = []
        for row_id in self._row_order[-12:]:
            row = self._rows[row_id]
            suffix = "  ctrl+o" if row.expandable else ""
            detail = f"  {row.detail}" if row.detail else ""
            rows.append(truncate_to_width(f"⏺ {row.title}{detail}{suffix}", width))
        if self.file_shelf_open and self._file_order:
            rows.append("Files")
            visible = self._file_order[-5:]
            hidden = len(self._file_order) - len(visible)
            for path in visible:
                rows.append(truncate_to_width(f"  ✓ {self._files[path]} {path}", width))
            if hidden > 0:
                rows.append(f"  +{hidden} more")
        elif self._file_order:
            rows.append(f"files: {len(self._file_order)}")
        return rows
