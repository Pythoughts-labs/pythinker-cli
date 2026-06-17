"""Helpers for file-diff tool card renderers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from pythinker_core.tooling import DisplayBlock
from rich.console import Group, RenderableType
from rich.text import Text

from pythinker_code.tools.display import DiffDisplayBlock
from pythinker_code.ui.shell.components import compute_edit_diff_string, render_diff
from pythinker_code.ui.shell.render_constants import DIFF_EXPANDED_MAX_LINES, expand_hint
from pythinker_code.ui.shell.tool_renderers import ToolResultPayload
from pythinker_code.ui.shell.tool_renderers._render_utils import fg


@dataclass(frozen=True, slots=True)
class DiffPreview:
    diff_text: str
    added: int
    removed: int
    summary_only: bool = False
    is_new_file: bool = False


# Huge-file summary blocks (utils/diff.py) describe the old side as "(N lines)".
# A zero-line old side means the file did not exist before this write.
_SUMMARY_LINE_COUNT_RE = re.compile(r"^\((\d+) lines?")


def _block_old_is_empty(block: DiffDisplayBlock) -> bool:
    """Whether *block* represents content with no pre-existing old side."""
    if block.is_summary:
        match = _SUMMARY_LINE_COUNT_RE.match(block.old_text.strip())
        return match is not None and match.group(1) == "0"
    return not block.old_text.strip()


def display_blocks_from_result(result: ToolResultPayload) -> list[DisplayBlock]:
    display_raw = result.details.get("display")
    if not isinstance(display_raw, list):
        return []
    display = cast("list[object]", display_raw)
    return [block for block in display if isinstance(block, DisplayBlock)]


def diff_blocks_from_result(result: ToolResultPayload) -> list[DiffDisplayBlock]:
    return [
        block for block in display_blocks_from_result(result) if isinstance(block, DiffDisplayBlock)
    ]


def _diff_counts(diff_text: str) -> tuple[int, int]:
    added = removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _prefix_diff_summary_lines(prefix: str, text: str) -> str:
    lines = text.splitlines() or [""]
    return "\n".join(f"{prefix} {line}" for line in lines)


def preview_from_diff_blocks(blocks: list[DiffDisplayBlock]) -> DiffPreview | None:
    if not blocks:
        return None

    chunks: list[str] = []
    summary_only = False
    for block in blocks:
        if block.is_summary:
            summary_only = True
            chunks.append(
                "\n".join(
                    [
                        _prefix_diff_summary_lines("-", block.old_text),
                        _prefix_diff_summary_lines("+", block.new_text),
                    ]
                )
            )
            continue
        diff = compute_edit_diff_string(
            block.old_text,
            block.new_text,
            old_start=block.old_start,
            new_start=block.new_start,
        ).diff
        if diff:
            chunks.append(diff)

    diff_text = "\n ...\n".join(chunks)
    if not diff_text:
        return None
    added, removed = _diff_counts(diff_text)
    is_new_file = all(_block_old_is_empty(block) for block in blocks)
    return DiffPreview(
        diff_text=diff_text,
        added=added,
        removed=removed,
        summary_only=summary_only,
        is_new_file=is_new_file,
    )


def preview_from_result(result: ToolResultPayload) -> DiffPreview | None:
    return preview_from_diff_blocks(diff_blocks_from_result(result))


def change_summary_text(added: int, removed: int) -> Text:
    parts: list[Text | str] = []
    if added:
        text = Text("Added ")
        text.append(str(added), style="bold")
        text.append(f" line{'s' if added != 1 else ''}")
        parts.append(text)
    if removed:
        text = Text("removed ")
        text.append(str(removed), style="bold")
        text.append(f" line{'s' if removed != 1 else ''}")
        parts.append(text)
    if not parts:
        return fg("tool_output", "No line changes")

    out = Text()
    for index, part in enumerate(parts):
        if index:
            out.append(", ")
        if isinstance(part, Text):
            out.append_text(part)
        else:
            out.append(part)
    return fg("tool_output", out)


def diff_frame(
    diff_text: str,
    *,
    width: int,
    expanded: bool = True,
    collapsed_max_lines: int = 16,
    state: dict[str, object] | None = None,
    path: str | None = None,
) -> RenderableType:
    """Render the inline diff body for a file tool card.

    The terminal transcript shows the summary line immediately
    followed by numbered +/- rows, without an ASCII box or dashed rails. Large
    diffs are collapsed by default and can be expanded from the tool card.
    """
    _ = width
    lines = diff_text.splitlines()
    if not expanded and len(lines) > collapsed_max_lines:
        if state is not None:
            state["__has_expandable_payload__"] = True
            state["__suppress_generic_expand_hint__"] = True
        shown = "\n".join(lines[:collapsed_max_lines])
        remaining = len(lines) - collapsed_max_lines
        return Group(render_diff(shown, path=path), fg("muted", expand_hint(remaining)))
    if len(lines) > DIFF_EXPANDED_MAX_LINES:
        # Guard against pathological diffs: even expanded, cap the rendered
        # body at head + tail with an explicit omitted-line count so one huge
        # edit cannot freeze or flood the terminal.
        head_count = DIFF_EXPANDED_MAX_LINES * 3 // 4
        tail_count = DIFF_EXPANDED_MAX_LINES - head_count
        omitted = len(lines) - head_count - tail_count
        return Group(
            render_diff("\n".join(lines[:head_count]), path=path),
            fg("muted", f"… {omitted} middle lines omitted (diff too large to render fully)"),
            render_diff("\n".join(lines[-tail_count:]), path=path),
        )
    return render_diff(diff_text, path=path)
