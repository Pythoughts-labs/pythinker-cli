"""Completion-menu rendering controls for the prompt (slash and file mentions).

The prompt_toolkit *layout installation* for these controls stays in
``pythinker_code.ui.shell.prompt`` because the facade owns the
``PromptSession`` object; this module owns only the rendering.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from prompt_toolkit.completion import Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import AnyFormattedText, FormattedText
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.utils import get_cwidth

from pythinker_code.ui.shell.glyphs import TRANSCRIPT_PROMPT_MARKER
from pythinker_code.ui.shell.prompting.completion.context import (
    CompletionKind,
    parse_completion_context,
)


def _resolve_app() -> Any:
    """Resolve the active application through the prompt facade module.

    Repository tests monkeypatch ``pythinker_code.ui.shell.prompt.get_app_or_none``
    to drive these controls; routing the lookup through that module keeps the
    monkeypatch surface intact after the rendering moved here.
    """
    from pythinker_code.ui.shell import prompt as shell_prompt

    return shell_prompt.get_app_or_none()


def truncate_to_width(text: str, width: int) -> str:
    if width <= 0:
        return ""

    total = 0
    chars: list[str] = []
    for ch in text:
        ch_width = get_cwidth(ch)
        if total + ch_width > width:
            break
        chars.append(ch)
        total += ch_width

    if total == get_cwidth(text):
        return text + (" " * max(0, width - total))

    ellipsis = "..."
    ellipsis_width = get_cwidth(ellipsis)
    if width <= ellipsis_width:
        return "." * width

    available = width - ellipsis_width
    total = 0
    chars = []
    for ch in text:
        ch_width = get_cwidth(ch)
        if total + ch_width > available:
            break
        chars.append(ch)
        total += ch_width
    return "".join(chars) + ellipsis + (" " * max(0, width - total - ellipsis_width))


def wrap_to_width(text: str, width: int, *, max_lines: int | None = None) -> list[str]:
    if width <= 0:
        return []

    words = text.split()
    if not words:
        return [""]

    lines: list[str] = []
    current_words: list[str] = []
    current_width = 0
    index = 0

    while index < len(words):
        word = words[index]
        word_width = get_cwidth(word)
        separator_width = 1 if current_words else 0

        if current_words and current_width + separator_width + word_width <= width:
            current_words.append(word)
            current_width += separator_width + word_width
            index += 1
            continue

        if not current_words and word_width <= width:
            current_words.append(word)
            current_width = word_width
            index += 1
            continue

        if not current_words and word_width > width:
            current_words.append(truncate_to_width(word, width).rstrip())
            current_width = get_cwidth(current_words[0])
            index += 1

        lines.append(" ".join(current_words))
        current_words = []
        current_width = 0

        if max_lines is not None and len(lines) == max_lines:
            remaining = " ".join(words[index:])
            if remaining:
                prefix = f"{lines[-1]} " if lines[-1] else ""
                lines[-1] = truncate_to_width(prefix + remaining, width).rstrip()
            return lines

    if current_words:
        line = " ".join(current_words)
        if max_lines is not None and len(lines) + 1 > max_lines:
            if lines:
                lines[-1] = truncate_to_width(f"{lines[-1]} {line}", width).rstrip()
            else:
                lines.append(truncate_to_width(line, width).rstrip())
        else:
            lines.append(line)

    return lines


class SlashCommandMenuControl(UIControl):
    """Render slash command completions as a full-width menu that matches the shell UI."""

    _MAX_EXPANDED_META_LINES = 3
    # One blank line is reserved above the list as breathing room from the input
    # row, so the menu reads as its own region rather than crowding what's typed.
    _GAP_LINES = 1
    # A persistent footer block at the bottom: a blank separator line plus the
    # navigation legend (which folds in the overflow count when the list scrolls).
    # The separator gives the legend the same breathing room as the top gap.
    _FOOTER_LINES = 2
    _FOOTER_LEGEND = "Enter to select · ↑/↓ to navigate · Esc to cancel"

    def __init__(
        self,
        *,
        left_padding: Callable[[], int],
        scroll_offset: int = 1,
    ) -> None:
        self._left_padding = left_padding
        self._scroll_offset = scroll_offset

    def has_focus(self) -> bool:
        return False

    def preferred_width(self, max_available_width: int) -> int | None:
        return max_available_width

    def preferred_height(
        self,
        width: int,
        max_available_height: int,
        wrap_lines: bool,
        get_line_prefix: Callable[..., AnyFormattedText] | None,
    ) -> int | None:
        app = _resolve_app()
        complete_state = (
            getattr(app.current_buffer, "complete_state", None) if app is not None else None
        )
        if complete_state is None:
            return 0
        completions = complete_state.completions
        if not completions:
            return 0
        selected_index = complete_state.complete_index
        if selected_index is None:
            content_height = len(completions)
        else:
            menu_width = max(0, width - self._left_padding())
            marker_width = 2
            command_width = self._command_column_width(completions, menu_width, marker_width)
            gap_width = 3 if menu_width > command_width + 6 else 1
            meta_width = max(0, menu_width - marker_width - command_width - gap_width)
            selected_meta_lines = self._selected_meta_lines(
                completions[selected_index].display_meta_text,
                meta_width,
            )
            content_height = (len(completions) - 1) + len(selected_meta_lines)
        # Reserve the gap line above the list and the footer line below it. When
        # the list is taller than the space the window allows, the window caps the
        # height and create_content lays the list out within whatever rows remain.
        chrome = self._GAP_LINES + self._FOOTER_LINES
        return min(max_available_height, content_height + chrome)

    def create_content(self, width: int, height: int) -> UIContent:
        app = _resolve_app()
        complete_state = (
            getattr(app.current_buffer, "complete_state", None) if app is not None else None
        )
        if complete_state is None or not complete_state.completions:
            return UIContent()

        completions = complete_state.completions
        selected_index = complete_state.complete_index
        match_prefix_len = self._match_prefix_len(app)

        menu_width = max(0, width - self._left_padding())
        marker_width = 2
        command_width = self._command_column_width(completions, menu_width, marker_width)
        gap_width = 3 if menu_width > command_width + 6 else 1
        meta_width = max(0, menu_width - marker_width - command_width - gap_width)

        total_rows = max(1, height)
        # The gap line above the list and the footer line below it are always
        # present, so the list itself lays out within the remaining rows.
        item_rows = max(1, total_rows - self._GAP_LINES - self._FOOTER_LINES)

        rendered_lines: list[FormattedText] = [self._blank_line()]
        cursor_y = 0

        if selected_index is None:
            # Pre-highlight index 0 even before the user navigates: pressing
            # Enter accepts the first completion, so the visual state should
            # match that behavior. Without this the menu looks ambiguous (no
            # row highlighted) but Enter still commits the top row.
            shown = min(len(completions), item_rows)
            for index in range(shown):
                rendered_lines.append(
                    self._render_single_line_item(
                        width=width,
                        completion=completions[index],
                        marker_width=marker_width,
                        command_width=command_width,
                        meta_width=meta_width,
                        gap_width=gap_width,
                        is_current=index == 0,
                        match_prefix_len=match_prefix_len,
                    )
                )
            cursor_y = 1 if shown else 0
            hidden = len(completions) - shown
        else:
            selected_meta_lines = self._selected_meta_lines(
                completions[selected_index].display_meta_text,
                meta_width,
            )
            start, end = self._visible_window_bounds(
                completion_count=len(completions),
                selected_index=selected_index,
                available_rows=item_rows,
                selected_item_height=len(selected_meta_lines),
            )
            for index in range(start, end + 1):
                completion = completions[index]
                if index == selected_index:
                    cursor_y = len(rendered_lines)
                    rendered_lines.extend(
                        self._render_selected_item_lines(
                            width=width,
                            completion=completion,
                            marker_width=marker_width,
                            command_width=command_width,
                            meta_width=meta_width,
                            gap_width=gap_width,
                            meta_lines=selected_meta_lines,
                            match_prefix_len=match_prefix_len,
                        )
                    )
                    continue
                rendered_lines.append(
                    self._render_single_line_item(
                        width=width,
                        completion=completion,
                        marker_width=marker_width,
                        command_width=command_width,
                        meta_width=meta_width,
                        gap_width=gap_width,
                        is_current=False,
                        match_prefix_len=match_prefix_len,
                    )
                )
            hidden = len(completions) - (end - start + 1)

        rendered_lines.append(self._blank_line())
        rendered_lines.append(
            self._render_footer_line(
                width=width, marker_width=marker_width, hidden_count=max(0, hidden)
            )
        )
        return UIContent(
            get_line=lambda i: rendered_lines[i],
            line_count=len(rendered_lines),
            cursor_position=Point(x=0, y=cursor_y),
        )

    def _blank_line(self) -> FormattedText:
        return FormattedText([("class:slash-completion-menu", "")])

    def _render_footer_line(
        self, *, width: int, marker_width: int, hidden_count: int
    ) -> FormattedText:
        # Persistent navigation legend, rendered in the dim meta style and aligned
        # under the command column. When the list scrolled, the count of hidden
        # entries leads so it survives truncation on narrow terminals.
        indent = self._left_padding() + marker_width
        text = self._FOOTER_LEGEND
        if hidden_count > 0:
            text = f"+{hidden_count} more · {text}"
        body = truncate_to_width(text, max(0, width - indent))
        trailing = max(0, width - indent - get_cwidth(body))
        fragments: FormattedText = FormattedText()
        fragments.append(("class:slash-completion-menu", " " * indent))
        fragments.append(("class:slash-completion-menu.meta", body))
        fragments.append(("class:slash-completion-menu", " " * trailing))
        return fragments

    def _match_prefix_len(self, app: Any) -> int:
        document = getattr(getattr(app, "current_buffer", None), "document", None)
        if not isinstance(document, Document):
            return 0
        context = parse_completion_context(document, allow_file=False)
        if context.kind is not CompletionKind.SLASH_COMMAND:
            return 0
        return len(context.token[1:])

    def _selected_meta_lines(self, text: str, meta_width: int) -> list[str]:
        lines = wrap_to_width(
            text,
            meta_width,
            max_lines=self._MAX_EXPANDED_META_LINES,
        )
        return lines or [""]

    def _visible_window_bounds(
        self,
        *,
        completion_count: int,
        selected_index: int,
        available_rows: int,
        selected_item_height: int,
    ) -> tuple[int, int]:
        selected_item_height = min(selected_item_height, available_rows)
        remaining_rows = max(0, available_rows - selected_item_height)

        before = min(self._scroll_offset, selected_index, remaining_rows)
        remaining_rows -= before
        after = min(completion_count - selected_index - 1, remaining_rows)
        remaining_rows -= after

        extra_before = min(selected_index - before, remaining_rows)
        before += extra_before
        remaining_rows -= extra_before

        extra_after = min(completion_count - selected_index - 1 - after, remaining_rows)
        after += extra_after

        return selected_index - before, selected_index + after

    def _command_column_width(
        self,
        completions: Sequence[Completion],
        menu_width: int,
        marker_width: int,
    ) -> int:
        if menu_width <= 0:
            return 0
        longest = max((get_cwidth(c.display_text) for c in completions), default=0)
        preferred = longest + 2
        usable_width = max(0, menu_width - marker_width)
        minimum = min(usable_width, 18)
        maximum = max(minimum, min(28, usable_width // 2))
        return max(minimum, min(preferred, maximum))

    def _render_command_text(
        self,
        text: str,
        *,
        width: int,
        base_style: str,
        is_current: bool,
        match_prefix_len: int,
    ) -> FormattedText:
        display = truncate_to_width(text, width)
        if match_prefix_len <= 0:
            return FormattedText([(base_style, display)])

        # Match highlighting for the slash popup: the leading slash stays in the
        # normal command style; the typed command prefix is emphasized.
        match_end = min(len(text), 1 + match_prefix_len)
        match_style = (
            "class:slash-completion-menu.command.match.current"
            if is_current
            else "class:slash-completion-menu.command.match"
        )
        fragments: FormattedText = FormattedText()
        for index, ch in enumerate(display):
            style = match_style if 0 < index < match_end and index < len(text) else base_style
            fragments.append((style, ch))
        return fragments

    def _render_single_line_item(
        self,
        *,
        width: int,
        completion: Completion,
        marker_width: int,
        command_width: int,
        meta_width: int,
        gap_width: int,
        is_current: bool,
        match_prefix_len: int,
    ) -> FormattedText:
        padding_width = max(0, width - marker_width - command_width - meta_width - gap_width)
        left_padding = min(self._left_padding(), padding_width)
        trailing_width = max(
            0,
            width - left_padding - marker_width - command_width - gap_width - meta_width,
        )

        command_style = (
            "class:slash-completion-menu.command.current"
            if is_current
            else "class:slash-completion-menu.command"
        )
        meta_style = (
            "class:slash-completion-menu.meta.current"
            if is_current
            else "class:slash-completion-menu.meta"
        )
        marker_style = (
            "class:slash-completion-menu.marker.current"
            if is_current
            else "class:slash-completion-menu.marker"
        )
        marker = f"{TRANSCRIPT_PROMPT_MARKER} " if is_current else "  "

        # When a row is selected, use the row.current background for the
        # gap and trailing padding so the highlight reads as a contiguous bar
        # rather than a fragmented set of pieces.
        gap_style = (
            "class:slash-completion-menu.row.current"
            if is_current
            else "class:slash-completion-menu"
        )
        fragments: FormattedText = FormattedText()
        fragments.append(("class:slash-completion-menu", " " * left_padding))
        fragments.append((marker_style, marker.ljust(marker_width)))
        fragments.extend(
            self._render_command_text(
                completion.display_text,
                width=command_width,
                base_style=command_style,
                is_current=is_current,
                match_prefix_len=match_prefix_len,
            )
        )
        fragments.append((gap_style, " " * gap_width))
        fragments.append((meta_style, truncate_to_width(completion.display_meta_text, meta_width)))
        fragments.append((gap_style, " " * trailing_width))
        return fragments

    def _render_selected_item_lines(
        self,
        *,
        width: int,
        completion: Completion,
        marker_width: int,
        command_width: int,
        meta_width: int,
        gap_width: int,
        meta_lines: Sequence[str],
        match_prefix_len: int,
    ) -> list[FormattedText]:
        lines = [
            self._render_single_line_item(
                width=width,
                completion=Completion(
                    text=completion.text,
                    start_position=completion.start_position,
                    display=completion.display,
                    display_meta=meta_lines[0],
                ),
                marker_width=marker_width,
                command_width=command_width,
                meta_width=meta_width,
                gap_width=gap_width,
                is_current=True,
                match_prefix_len=match_prefix_len,
            )
        ]

        continuation_prefix = (
            " " * self._left_padding() + " " * marker_width + " " * command_width + " " * gap_width
        )
        continuation_trailing = max(
            0,
            width - get_cwidth(continuation_prefix) - meta_width,
        )
        for meta_line in meta_lines[1:]:
            fragments: FormattedText = FormattedText()
            fragments.append(("class:slash-completion-menu", continuation_prefix))
            fragments.append(
                (
                    "class:slash-completion-menu.meta.current",
                    truncate_to_width(meta_line, meta_width),
                )
            )
            fragments.append(("class:slash-completion-menu", " " * continuation_trailing))
            lines.append(fragments)

        return lines


class LocalFileMentionMenuControl(UIControl):
    """Render `@` file completions as a clean inline, two-column menu."""

    _MIN_DETAIL_WIDTH = 16
    _MAX_NAME_WIDTH = 32

    def __init__(
        self,
        *,
        left_padding: Callable[[], int],
        scroll_offset: int = 1,
    ) -> None:
        self._left_padding = left_padding
        self._scroll_offset = scroll_offset

    def has_focus(self) -> bool:
        return False

    def preferred_width(self, max_available_width: int) -> int | None:
        return max_available_width

    def preferred_height(
        self,
        width: int,
        max_available_height: int,
        wrap_lines: bool,
        get_line_prefix: Callable[..., AnyFormattedText] | None,
    ) -> int | None:
        app = _resolve_app()
        complete_state = (
            getattr(app.current_buffer, "complete_state", None) if app is not None else None
        )
        if complete_state is None or not complete_state.completions:
            return 0
        # Reserve the final row for the position counter.
        return min(max_available_height, len(complete_state.completions) + 1)

    def create_content(self, width: int, height: int) -> UIContent:
        app = _resolve_app()
        complete_state = (
            getattr(app.current_buffer, "complete_state", None) if app is not None else None
        )
        if complete_state is None or not complete_state.completions or height <= 0:
            return UIContent()

        completions = complete_state.completions
        selected_index = complete_state.complete_index or 0
        selected_index = max(0, min(selected_index, len(completions) - 1))
        show_count = height > 1
        item_rows = max(1, height - (1 if show_count else 0))
        start, end = self._visible_window_bounds(
            completion_count=len(completions),
            selected_index=selected_index,
            available_rows=item_rows,
        )

        menu_width = max(0, width - self._left_padding())
        marker_width = 2
        gap_width = 4 if menu_width >= 48 else 2
        detail_enabled = menu_width >= marker_width + gap_width + self._MIN_DETAIL_WIDTH + 12
        if detail_enabled:
            name_width = min(
                self._MAX_NAME_WIDTH,
                max(12, (menu_width - marker_width - gap_width) // 2),
            )
            detail_width = max(0, menu_width - marker_width - name_width - gap_width)
        else:
            name_width = max(0, menu_width - marker_width)
            detail_width = 0
            gap_width = 0

        rendered_lines: list[FormattedText] = []
        selected_line_index = 0
        for index in range(start, end + 1):
            if index == selected_index:
                selected_line_index = len(rendered_lines)
            rendered_lines.append(
                self._render_item_line(
                    width=width,
                    completion=completions[index],
                    is_current=index == selected_index,
                    marker_width=marker_width,
                    name_width=name_width,
                    gap_width=gap_width,
                    detail_width=detail_width,
                )
            )

        if show_count:
            rendered_lines.append(
                self._render_count_line(
                    width=width,
                    selected_index=selected_index,
                    total=len(completions),
                    marker_width=marker_width,
                )
            )

        return UIContent(
            get_line=lambda i: rendered_lines[i],
            line_count=len(rendered_lines),
            cursor_position=Point(x=0, y=selected_line_index),
        )

    def _visible_window_bounds(
        self,
        *,
        completion_count: int,
        selected_index: int,
        available_rows: int,
    ) -> tuple[int, int]:
        visible_rows = min(completion_count, max(1, available_rows))
        max_start = max(0, completion_count - visible_rows)
        start = min(max(0, selected_index - self._scroll_offset), max_start)
        return start, start + visible_rows - 1

    def _render_item_line(
        self,
        *,
        width: int,
        completion: Completion,
        is_current: bool,
        marker_width: int,
        name_width: int,
        gap_width: int,
        detail_width: int,
    ) -> FormattedText:
        left_padding = min(self._left_padding(), width)
        name = completion.display_text or completion.text
        detail = (completion.text or name).rstrip("/")
        marker = "→ " if is_current else "  "
        marker_style = (
            "class:file-completion-menu.marker.current"
            if is_current
            else "class:file-completion-menu.marker"
        )
        name_style = (
            "class:file-completion-menu.name.current"
            if is_current
            else "class:file-completion-menu.name"
        )
        detail_style = (
            "class:file-completion-menu.detail.current"
            if is_current
            else "class:file-completion-menu.detail"
        )

        fragments: FormattedText = FormattedText()
        fragments.append(("class:file-completion-menu", " " * left_padding))
        fragments.append((marker_style, marker.ljust(marker_width)))
        fragments.append((name_style, truncate_to_width(name, name_width)))
        if detail_width > 0:
            fragments.append(("class:file-completion-menu", " " * gap_width))
            fragments.append((detail_style, truncate_to_width(detail, detail_width)))
        used_width = left_padding + marker_width + name_width + gap_width + detail_width
        if used_width < width:
            fragments.append(("class:file-completion-menu", " " * (width - used_width)))
        return fragments

    def _render_count_line(
        self,
        *,
        width: int,
        selected_index: int,
        total: int,
        marker_width: int,
    ) -> FormattedText:
        left_padding = min(self._left_padding() + marker_width, width)
        label = f"({selected_index + 1}/{total})"
        fragments: FormattedText = FormattedText()
        fragments.append(("class:file-completion-menu", " " * left_padding))
        count_text = truncate_to_width(label, max(0, width - left_padding))
        fragments.append(("class:file-completion-menu.count", count_text))
        return fragments
