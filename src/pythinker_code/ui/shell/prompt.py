from __future__ import annotations

import asyncio
import contextlib
import os
import random
import shlex
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from contextvars import Token
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import md5
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completion, merge_completers
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.formatted_text import (
    AnyFormattedText,
    FormattedText,
    StyleAndTextTuples,
    to_formatted_text,
)
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    DynamicContainer,
    FloatContainer,
    HSplit,
    Window,
    WindowRenderInfo,
)
from prompt_toolkit.layout.controls import BufferControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import Margin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.utils import get_cwidth
from pydantic import BaseModel
from pythinker_host import get_current_host
from pythinker_host.path import HostPath

from pythinker_code.config import StatusLineConfig
from pythinker_code.llm import ModelCapability
from pythinker_code.share import get_share_dir
from pythinker_code.soul import StatusSnapshot, format_context_status
from pythinker_code.thinking import available_thinking_levels, model_uses_native_thinking
from pythinker_code.tools.display import TodoDisplayItem
from pythinker_code.ui.shell import placeholders as prompt_placeholders
from pythinker_code.ui.shell.console import console
from pythinker_code.ui.shell.glyphs import (
    TRANSCRIPT_PROMPT_MARKER,
    TRANSCRIPT_TOOL_GUTTER,
)
from pythinker_code.ui.shell.motion import active_marker_frame, shimmer_prompt_fragments
from pythinker_code.ui.shell.placeholders import (
    PromptPlaceholderManager,
    normalize_pasted_text,
    sanitize_surrogates,
)
from pythinker_code.ui.shell.prompting import (
    ClipboardAdapter,
    FooterViewModel,
    FrozenFragments,
    GitSnapshot,
    GitStatusIndex,
    PromptFrame,
    PromptFrameCollector,
    PromptHistoryError,
    PromptHistoryStore,
    PromptSceneBudget,
    ToastManager,
    ToastSnapshot,
    allocate_prompt_scene_rows,
    background_task_summary,
    select_footer_content,
    truncate_footer_left,
    truncate_footer_right,
)
from pythinker_code.ui.shell.prompting.clipboard import (
    bind_clipboard_adapter,
    reset_clipboard_adapter,
)
from pythinker_code.ui.shell.prompting.clipboard import (
    grab_media_from_clipboard as grab_media_from_clipboard,
)
from pythinker_code.ui.shell.prompting.clipboard import (
    is_clipboard_available as is_clipboard_available,
)
from pythinker_code.ui.shell.prompting.clipboard import (
    is_media_clipboard_available as is_media_clipboard_available,
)
from pythinker_code.ui.shell.prompting.completion.context import (
    CompletionKind,
    parse_completion_context,
)
from pythinker_code.ui.shell.prompting.completion.slash import (
    InputHighlightLexer,
    SlashCommandAutoSuggest,
    SlashCommandCompleter,
    command_name_set,
    discard_slash_command,
)
from pythinker_code.ui.shell.prompting.completion.workspace import (
    HostFileMentionCompleter,
    WorkspaceIndex,
)
from pythinker_code.ui.shell.prompting.git_status import (
    bind_git_status_index,
    current_git_snapshot,
    reset_git_status_index,
)
from pythinker_code.ui.shell.prompting.history import (
    HistoryEntry,
    ensure_private_history_path,
    load_history_entries,
    redact_history_secrets,
)
from pythinker_code.ui.shell.prompting.lifecycle import PromptLifecycle
from pythinker_code.ui.shell.prompting.state import (
    BufferObserved,
    Invalidate,
    ModalAttached,
    ModalDetached,
    ModalState,
    ModeChanged,
    PromptEffect,
    PromptEvent,
    PromptMode,
    PromptPhase,
    PromptState,
    RestoreDocument,
    RunningDelegateAttached,
    RunningDelegateDetached,
    RunningPromptDelegate,
    SelectCompleter,
    SetEraseWhenDone,
    ShortcutHelpToggled,
    SuspendDocument,
    TurnCleared,
    TurnStarting,
    transition,
)
from pythinker_code.ui.shell.prompting.toasts import (
    bind_toast_manager,
    bootstrap_toast_queues,
    current_toast,
    reset_toast_manager,
    toast,
)
from pythinker_code.ui.shell.spacing import (
    PREAMBLE_EARLIER_OUTPUT_HIDDEN_HINT,
    ensure_prompt_newline,
)
from pythinker_code.ui.shell.spinner_words import spinner_message
from pythinker_code.ui.shell.sync_output import install_synchronized_output
from pythinker_code.ui.terminal_capabilities import synchronized_output_enabled
from pythinker_code.ui.theme import get_prompt_style, get_toolbar_colors, thinking_dot_style
from pythinker_code.ui.theme import get_tui_tokens as _get_tui_tokens
from pythinker_code.ui.tui_config import is_card_style
from pythinker_code.utils.logging import logger
from pythinker_code.utils.slashcmd import SlashCommand
from pythinker_code.wire.types import ContentPart, TextPart

if TYPE_CHECKING:
    from pythinker_code.ui.shell.statusline import StatusLineContext

AttachmentCache = prompt_placeholders.AttachmentCache
CachedAttachment = prompt_placeholders.CachedAttachment
_parse_attachment_kind = prompt_placeholders.parse_attachment_kind

PROMPT_SYMBOL = "✨"
PROMPT_SYMBOL_AGENT_INPUT = TRANSCRIPT_PROMPT_MARKER
PROMPT_SYMBOL_SHELL = "$"
PROMPT_SYMBOL_THINKING = "💫"
PROMPT_SYMBOL_PLAN = "📋"
_CARD_SIDE_PADDING = 2
_INPUT_RIGHT_PADDING = 2


_ORIGINAL_UNRAISABLE_HOOK = sys.unraisablehook


def _is_prompt_toolkit_keyprocessor_shutdown_noise(unraisable: Any) -> bool:
    """Return true for prompt_toolkit's Python 3.14 coroutine-finalizer noise."""
    exc = getattr(unraisable, "exc_value", None)
    obj = getattr(unraisable, "object", None)
    return (
        isinstance(exc, KeyError)
        and exc.args == ("__import__",)
        and "KeyProcessor._start_timeout.<locals>.wait" in repr(obj)
    )


def _pythinker_unraisable_hook(unraisable: Any) -> None:
    if _is_prompt_toolkit_keyprocessor_shutdown_noise(unraisable):
        return
    _ORIGINAL_UNRAISABLE_HOOK(unraisable)


# Python 3.14 can report prompt_toolkit's already-cancelled key-timeout coroutine as an
# unraisable KeyError("__import__") during interpreter/module teardown. The RuntimeWarning filters
# above catch the normal warning path; this hook catches the shutdown-only unraisable path while
# delegating every other unraisable exception to Python's original hook.
if sys.unraisablehook is not _pythinker_unraisable_hook:
    sys.unraisablehook = _pythinker_unraisable_hook


class CwdLostError(OSError):
    """Raised when the working directory no longer exists (e.g. external drive unplugged)."""


_command_name_set = command_name_set
_discard_slash_command = discard_slash_command


def _card_side_padding() -> int:
    return _CARD_SIDE_PADDING if is_card_style() else 0


def _card_side_indent() -> str:
    return " " * _card_side_padding()


def _prompt_rule(columns: int) -> str:
    """Return a prompt-toolkit-safe horizontal rule for the current terminal width.

    prompt_toolkit can leave resize artifacts when non-fullscreen prompts draw
    visible content through the last terminal column; full-width bottom bars make
    the duplicated prompt spam especially obvious. Keep the rightmost column
    blank for prompt-owned chrome while still visually reading as a full rule.
    """
    return "─" * max(0, columns - 1)


def _truncate_to_width(text: str, width: int) -> str:
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


def _formatted_text_display_rows(fragments: FormattedText, columns: int) -> list[FormattedText]:
    """Split formatted text into terminal display rows, preserving styles."""
    rows: list[FormattedText] = [FormattedText()]
    col = 0
    for style, text, *_ in fragments:
        for ch in text:
            if ch == "\n":
                rows.append(FormattedText())
                col = 0
                continue
            width = max(0, get_cwidth(ch))
            if width and col + width > columns:
                rows.append(FormattedText())
                col = 0
            rows[-1].append((style, ch))
            col += width
    return rows


def _extend_rows(out: FormattedText, rows: list[FormattedText]) -> None:
    for index, row in enumerate(rows):
        out.extend(row)
        if index != len(rows) - 1:
            out.append(("", "\n"))


class _PromptRightPaddingMargin(Margin):
    """Reserve blank columns at the right edge of the prompt input window."""

    def __init__(self, width: Callable[[], int]) -> None:
        self._width = width

    def get_width(self, get_ui_content: Callable[[], UIContent]) -> int:
        del get_ui_content
        return max(0, self._width())

    def create_margin(
        self,
        window_render_info: WindowRenderInfo,
        width: int,
        height: int,
    ) -> StyleAndTextTuples:
        del height
        fragments: StyleAndTextTuples = []
        for _ in window_render_info.displayed_lines:
            fragments.append(("class:compact-input", " " * width))
            fragments.append(("", "\n"))
        return fragments


def _background_task_summary(counts: BgTaskCounts) -> str | None:
    return background_task_summary(bash=counts.bash, agent=counts.agent) or None


def _append_footer_hint_fragments(
    fragments: list[tuple[str, str]],
    tip_text: str,
    *,
    tip_style: str,
    key_style: str,
) -> None:
    """Append toolbar tips with bold key emphasis while preserving plain text."""
    parts = tip_text.split(_TIP_SEPARATOR)
    for index, part in enumerate(parts):
        if index:
            fragments.append((tip_style, _TIP_SEPARATOR))
        key, sep, label = part.partition(": ")
        if sep:
            fragments.append((key_style, key))
            fragments.append((tip_style, sep + label))
        else:
            fragments.append((tip_style, part))


def _fit_formatted_text_to_rows(
    fragments: FormattedText,
    columns: int,
    max_rows: int,
    *,
    preserve_tail_rows: int = 0,
) -> FormattedText:
    """Crop prompt preamble text so it cannot cover the input/footer area.

    prompt_toolkit reserves the bottom toolbar separately. If the dynamic
    prompt message grows taller than the terminal, the rendered tool card can
    visually run underneath the input row and footer. Count wrapped display rows
    and leave a compact truncation hint instead of allowing overlap.

    ``preserve_tail_rows`` keeps important trailing status rows, such as the
    live thinking-word spinner, visible when a tall tool card has to be clipped.
    """
    if max_rows <= 0:
        return FormattedText([])
    if columns <= 0:
        columns = 80

    rows = _formatted_text_display_rows(fragments, columns)
    if len(rows) <= max_rows:
        return fragments

    tail_rows: list[FormattedText] = []
    if preserve_tail_rows > 0 and max_rows > 2:
        for row in reversed(rows):
            if not any(text for _, text, *_ in row):
                continue
            tail_rows.append(row)
            if len(tail_rows) >= preserve_tail_rows:
                break
        tail_rows.reverse()
        tail_rows = tail_rows[: max(0, max_rows - 2)]

    content_rows = max(0, max_rows - 1 - len(tail_rows))
    if content_rows == 0:
        return FormattedText(
            [
                (
                    "class:dim",
                    _truncate_right(PREAMBLE_EARLIER_OUTPUT_HIDDEN_HINT, columns),
                )
            ]
        )

    out: FormattedText = FormattedText()
    _extend_rows(out, rows[:content_rows])
    if out and not out[-1][1].endswith("\n"):
        out.append(("", "\n"))
    clip_hint = _truncate_right(PREAMBLE_EARLIER_OUTPUT_HIDDEN_HINT, columns)
    out.append(("class:dim", clip_hint))
    if tail_rows:
        out.append(("", "\n"))
        _extend_rows(out, tail_rows)
    return out


def _fit_prompt_scene_to_rows(
    fragments: FormattedText,
    columns: int,
    max_rows: int,
    *,
    show_clip_hint: bool = True,
    drop_blank_rows: bool = False,
) -> FormattedText:
    """Tail-clip a complete scene, changing nothing unless it overflows."""
    if max_rows <= 0:
        return FormattedText()
    rows = _formatted_text_display_rows(fragments, max(1, columns))
    if len(rows) <= max_rows:
        return fragments
    if drop_blank_rows:
        rows = [row for row in rows if any(text for _, text, *_ in row)]
    while rows and not any(text for _, text, *_ in rows[-1]):
        rows.pop()
    if not rows:
        return FormattedText()
    if len(rows) <= max_rows:
        out = FormattedText()
        _extend_rows(out, rows)
        return out
    if max_rows == 1 or not show_clip_hint:
        selected = rows[-max_rows:]
        out = FormattedText()
        _extend_rows(out, selected)
        return out
    out = FormattedText(
        [
            (
                "class:dim",
                _truncate_right(PREAMBLE_EARLIER_OUTPUT_HIDDEN_HINT, columns),
            ),
            ("", "\n"),
        ]
    )
    _extend_rows(out, rows[-(max_rows - 1) :])
    return out


def _prompt_preamble_max_rows(terminal_rows: int | None) -> int:
    if terminal_rows is None or terminal_rows <= 0:
        return 20
    return PromptSceneBudget(terminal_rows=terminal_rows).preamble_rows


def _wrap_to_width(text: str, width: int, *, max_lines: int | None = None) -> list[str]:
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
            current_words.append(_truncate_to_width(word, width).rstrip())
            current_width = get_cwidth(current_words[0])
            index += 1

        lines.append(" ".join(current_words))
        current_words = []
        current_width = 0

        if max_lines is not None and len(lines) == max_lines:
            remaining = " ".join(words[index:])
            if remaining:
                prefix = f"{lines[-1]} " if lines[-1] else ""
                lines[-1] = _truncate_to_width(prefix + remaining, width).rstrip()
            return lines

    if current_words:
        line = " ".join(current_words)
        if max_lines is not None and len(lines) + 1 > max_lines:
            if lines:
                lines[-1] = _truncate_to_width(f"{lines[-1]} {line}", width).rstrip()
            else:
                lines.append(_truncate_to_width(line, width).rstrip())
        else:
            lines.append(line)

    return lines


def _find_prompt_float_container(layout_container: object) -> FloatContainer | None:
    if not isinstance(layout_container, HSplit):
        return None

    for child in cast(Sequence[object], layout_container.children):
        float_container = _extract_float_container(child)
        if float_container is not None:
            return float_container
    return None


def _extract_float_container(container: object) -> FloatContainer | None:
    if isinstance(container, FloatContainer):
        return container
    if isinstance(container, ConditionalContainer):
        if isinstance(container.content, FloatContainer):
            return container.content
        if isinstance(container.alternative_content, FloatContainer):
            return container.alternative_content
    return None


def _find_default_buffer_container(
    layout_container: object,
    target_buffer: Buffer,
) -> ConditionalContainer | None:
    seen: set[int] = set()

    def _walk(node: object) -> ConditionalContainer | None:
        if id(node) in seen:
            return None
        seen.add(id(node))

        if isinstance(node, ConditionalContainer):
            content = getattr(node, "content", None)
            if isinstance(content, Window):
                control = content.content
                if isinstance(control, BufferControl) and control.buffer is target_buffer:
                    return node

        if isinstance(node, DynamicContainer):
            with contextlib.suppress(Exception):
                found = _walk(node.get_container())
                if found is not None:
                    return found

        for attr in ("children", "content", "floats", "container"):
            if not hasattr(node, attr):
                continue
            value = getattr(node, attr)
            if attr == "children" and isinstance(value, Sequence):
                for child in value:  # pyright: ignore[reportUnknownVariableType]
                    found = _walk(child)  # pyright: ignore[reportUnknownArgumentType]
                    if found is not None:
                        return found
            elif attr == "floats" and isinstance(value, Sequence):
                for float_ in value:  # pyright: ignore[reportUnknownVariableType]
                    content = getattr(float_, "content", None)  # pyright: ignore[reportUnknownArgumentType]
                    if content is None:
                        continue
                    found = _walk(content)
                    if found is not None:
                        return found
            elif (
                attr in {"content", "container"}
                and value is not None
                and type(value).__module__.startswith("prompt_toolkit")
            ):
                found = _walk(value)
                if found is not None:
                    return found
        return None

    return _walk(layout_container)


def _container_contains(root: object, target: object) -> bool:
    seen: set[int] = set()

    def _walk(node: object) -> bool:
        if id(node) in seen:
            return False
        seen.add(id(node))
        if node is target:
            return True
        if isinstance(node, DynamicContainer):
            with contextlib.suppress(Exception):
                if _walk(node.get_container()):
                    return True
        for attr in ("children", "content", "floats", "container", "alternative_content"):
            if not hasattr(node, attr):
                continue
            value: object = getattr(node, attr)
            if attr == "children" and isinstance(value, Sequence):
                children = cast(Sequence[object], value)
                if any(_walk(child) for child in children):
                    return True
            elif attr == "floats" and isinstance(value, Sequence):
                floats = cast(Sequence[object], value)
                if any(_walk(cast(object, getattr(float_, "content", None))) for float_ in floats):
                    return True
            elif value is not None and _walk(value):
                return True
        return False

    return _walk(root)


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
        app = get_app_or_none()
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
        app = get_app_or_none()
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
        body = _truncate_to_width(text, max(0, width - indent))
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
        lines = _wrap_to_width(
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
        display = _truncate_to_width(text, width)
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
        fragments.append((meta_style, _truncate_to_width(completion.display_meta_text, meta_width)))
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
                    _truncate_to_width(meta_line, meta_width),
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
        app = get_app_or_none()
        complete_state = (
            getattr(app.current_buffer, "complete_state", None) if app is not None else None
        )
        if complete_state is None or not complete_state.completions:
            return 0
        # Reserve the final row for the position counter.
        return min(max_available_height, len(complete_state.completions) + 1)

    def create_content(self, width: int, height: int) -> UIContent:
        app = get_app_or_none()
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
        fragments.append((name_style, _truncate_to_width(name, name_width)))
        if detail_width > 0:
            fragments.append(("class:file-completion-menu", " " * gap_width))
            fragments.append((detail_style, _truncate_to_width(detail, detail_width)))
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
        count_text = _truncate_to_width(label, max(0, width - left_padding))
        fragments.append(("class:file-completion-menu.count", count_text))
        return fragments


LocalFileMentionCompleter = HostFileMentionCompleter


_HistoryEntry = HistoryEntry


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _redact_history_secrets(text: str) -> str:
    return redact_history_secrets(text)


_ensure_private_history_path = ensure_private_history_path
_load_history_entries = load_history_entries


class PromptUIState(Enum):
    NORMAL_INPUT = "normal_input"
    MODAL_HIDDEN_INPUT = "modal_hidden_input"
    MODAL_TEXT_INPUT = "modal_text_input"


class UserInput(BaseModel):
    mode: PromptMode
    command: str
    """The plain text representation of the user input."""
    resolved_command: str
    """The text command after UI-only placeholders are expanded."""
    content: list[ContentPart]
    """The rich content parts."""

    def __str__(self) -> str:
        return self.command

    def __bool__(self) -> bool:
        return bool(self.command)


_IDLE_REFRESH_INTERVAL = 1.0
_RUNNING_REFRESH_INTERVAL = 0.1
# ponytail: 2s quiet threshold — silent dev servers drop to idle refresh
_BG_QUIET_THRESHOLD_S = 2.0

_TIP_ROTATE_INTERVAL = 30.0
_MAX_CWD_COLS = 30
_MAX_BRANCH_COLS = 22


def _get_git_branch() -> str | None:
    """Return the active session's cached branch without blocking on I/O."""
    return current_git_snapshot().branch


def _get_git_status() -> tuple[bool, int, int]:
    """Return the active session's cached dirty/ahead/behind state."""
    snapshot = current_git_snapshot()
    return snapshot.dirty, snapshot.ahead, snapshot.behind


def _format_git_badge(branch: str, dirty: bool, ahead: int, behind: int) -> str:
    """Format branch name with an optional status badge: ``main [± ↑3↓1]``."""
    parts: list[str] = []
    if dirty:
        parts.append("±")
    sync = ""
    if ahead:
        sync += f"↑{ahead}"
    if behind:
        sync += f"↓{behind}"
    if sync:
        parts.append(sync)
    if not parts:
        return branch
    return f"{branch} [{' '.join(parts)}]"


def _get_git_diffstat() -> tuple[int, int] | None:
    """Return the active session's cached working-tree line counts."""
    return current_git_snapshot().diffstat


def _shorten_cwd(path: str) -> str:
    """Replace the home directory prefix in *path* with ``~``."""
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def _display_width(text: str) -> int:
    """Return the terminal column width of *text*, handling wide Unicode characters."""
    return sum(get_cwidth(c) for c in text)


def _truncate_left(text: str, max_cols: int) -> str:
    """Truncate *text* from the left, prepending '…' if it exceeds *max_cols*."""
    if max_cols <= 0:
        return ""
    if _display_width(text) <= max_cols:
        return text
    ellipsis = "…"
    budget = max_cols - _display_width(ellipsis)
    chars: list[str] = []
    width = 0
    for ch in reversed(text):
        w = get_cwidth(ch)
        if width + w > budget:
            break
        chars.append(ch)
        width += w
    return ellipsis + "".join(reversed(chars))


def _truncate_right(text: str, max_cols: int) -> str:
    """Truncate *text* from the right, appending '…' if it exceeds *max_cols*."""
    if max_cols <= 0:
        return ""
    if _display_width(text) <= max_cols:
        return text
    ellipsis = "…"
    budget = max_cols - _display_width(ellipsis)
    chars: list[str] = []
    width = 0
    for ch in text:
        w = get_cwidth(ch)
        if width + w > budget:
            break
        chars.append(ch)
        width += w
    return "".join(chars) + ellipsis


@dataclass(frozen=True, slots=True)
class BgTaskCounts:
    bash: int = 0
    agent: int = 0


@runtime_checkable
class AgentStatusProvider(Protocol):
    """Optional protocol for delegates that render always-visible agent status.

    When the running prompt delegate implements this, ``_render_agent_status``
    will call ``render_agent_status`` instead of the fallback status block.
    This ensures spinners, content blocks, and tool calls remain visible
    even when a modal (approval/question/btw) is active.
    """

    def render_agent_status(self, columns: int) -> AnyFormattedText: ...


@runtime_checkable
class PinnedStatusTailProvider(Protocol):
    """Optional protocol for delegates exposing a trailing status tail (the
    verb spinner) that must stay pinned *below* a clipped agent stream.

    Kept separate from ``AgentStatusProvider`` so delegates that don't split
    out a pinned tail still satisfy ``AgentStatusProvider`` unchanged.
    """

    def render_pinned_status_tail(self, columns: int) -> AnyFormattedText: ...


_toast_queues = bootstrap_toast_queues
_current_toast = current_toast


def _build_toolbar_tips(clipboard_available: bool) -> list[str]:
    from pythinker_code.ui.shell.keymap import key_text

    def _tip(binding: str, fallback: str, description: str) -> str:
        label = key_text(binding) or fallback
        return f"{label}: {description}"

    tips = [
        _tip("app.prompt.help", "?", "shortcuts"),
        _tip("app.mode.toggle", "ctrl-x", "toggle mode"),
        _tip("app.thinking.cycle", "shift-tab", "change thinking effort"),
        _tip("app.shell.oneshot", "!", "shell command"),
        _tip("app.editor.external", "ctrl-o", "editor"),
        _tip("app.todos.toggle", "ctrl-t", "toggle todos"),
        _tip("app.prompt.newline", "ctrl-j", "newline"),
        "/feedback: send feedback",
        "/theme: switch dark/light",
    ]
    if clipboard_available:
        tips.append(_tip("app.clipboard.paste", "ctrl-v", "paste clipboard"))
    tips.append(_tip("app.mention.files", "@", "mention files"))
    return tips


_TIP_SEPARATOR = " | "

# Cap prompt redraws at ~30 fps. Smooth streaming calls ``invalidate()`` on a
# fast cadence; without this, prompt_toolkit redraws on *every* invalidate,
# which (per its own docs) "could cause a lot of terminal output, which some
# terminals are not able to process" — the classic streaming flicker/lag.
#
# We use ``max_render_postpone_time`` rather than ``min_redraw_interval``: the
# latter throttles via an ``async def redraw_in_future`` coroutine that
# ``invalidate()`` schedules onto the loop, and during a prompt-app/loop handoff
# (e.g. ``/login`` swapping prompt sessions) that coroutine can be dropped
# un-awaited, emitting a noisy ``RuntimeWarning``. ``max_render_postpone_time``
# coalesces rapid invalidations through a coroutine-free path (it batches
# redraws up to this deadline, rendering immediately when the loop is idle), so
# it achieves the same throttling without the leak. See
# tests/ui_and_conv/test_redraw_throttle.py.
_MAX_RENDER_POSTPONE_S = 1 / 30


class CustomPromptSession:
    def __init__(
        self,
        *,
        status_provider: Callable[[], StatusSnapshot],
        status_block_provider: Callable[[int], AnyFormattedText | None] | None = None,
        fast_refresh_provider: Callable[[], bool] | None = None,
        background_task_count_provider: Callable[[], BgTaskCounts] | None = None,
        update_notice_provider: Callable[[], str | None] | None = None,
        model_capabilities: set[ModelCapability],
        model_name: str | None,
        thinking: bool,
        thinking_effort: str | None = None,
        agent_mode_slash_commands: Sequence[SlashCommand[Any]] = (),
        shell_mode_slash_commands: Sequence[SlashCommand[Any]],
        editor_command_provider: Callable[[], str] = lambda: "",
        turn_recaps_provider: Callable[[], bool] = lambda: False,
        plan_mode_toggle_callback: Callable[[], Awaitable[bool]] | None = None,
        thinking_effort_cycle_callback: Callable[[], Awaitable[str | None]] | None = None,
        history_enabled: bool = True,
        statusline_config: StatusLineConfig | None = None,
        sticky_input: bool = True,
    ) -> None:
        from pythinker_code.ui.shell.statusline import (
            RateSampler,
            StatusLineCommandRunner,
            resolve_segments,
        )

        _statusline_cfg = statusline_config or StatusLineConfig()
        self._statusline_layout = resolve_segments(_statusline_cfg)
        self._statusline_runner: StatusLineCommandRunner | None = None
        self._lifecycle = PromptLifecycle()
        if self._statusline_layout.show_command and _statusline_cfg.command:
            self._statusline_runner = StatusLineCommandRunner(
                command=_statusline_cfg.command,
                timeout_ms=_statusline_cfg.command_timeout_ms,
            )
            self._lifecycle.register_closer(
                "statusline command runner", self._statusline_runner.stop
            )
        self._statusline_cfg = _statusline_cfg
        self._statusline_started_at = time.monotonic()
        self._rate_in_sampler = RateSampler()
        self._rate_out_sampler = RateSampler()
        self._statusline_frame = 0
        history_dir = get_share_dir() / "user-history"
        work_dir_id = md5(
            str(HostPath.cwd()).encode(encoding="utf-8"), usedforsecurity=False
        ).hexdigest()
        self._history_file = (history_dir / work_dir_id).with_suffix(".jsonl")
        self._history_enabled = history_enabled and not _env_truthy(
            "PYTHINKER_DISABLE_PROMPT_HISTORY"
        )
        if self._history_enabled:
            history_dir.mkdir(parents=True, exist_ok=True)
        self._history_store = PromptHistoryStore(
            self._history_file,
            enabled=self._history_enabled,
        )
        self._lifecycle.register_closer("prompt history", self._history_store.aclose)
        self._toast_manager = ToastManager()
        self._lifecycle.register_closer("toast manager", self._toast_manager.aclose)
        self._clipboard_adapter = ClipboardAdapter()
        self._lifecycle.register_closer("clipboard adapter", self._clipboard_adapter.aclose)
        self._git_status_index = GitStatusIndex(
            get_current_host(),
            self._lifecycle,
            on_publish=self.invalidate,
        )
        self._lifecycle.register_closer("Git status index", self._git_status_index.aclose)
        self._git_status_token: Token[GitStatusIndex | None] | None = None
        self._toast_token: Token[ToastManager | None] | None = None
        self._clipboard_token: Token[ClipboardAdapter | None] | None = None
        self._status_provider = status_provider
        self._status_block_provider = status_block_provider
        self._fast_refresh_provider = fast_refresh_provider
        self._background_task_count_provider = background_task_count_provider
        self._update_notice_provider = update_notice_provider
        self._prompt_frame_update_notice: str | None = None
        self._prompt_footer_row_budget = 0
        self._editor_command_provider = editor_command_provider
        self._turn_recaps_provider = turn_recaps_provider
        self._plan_mode_toggle_callback = plan_mode_toggle_callback
        self._thinking_effort_cycle_callback = thinking_effort_cycle_callback
        self._model_capabilities = model_capabilities
        self._model_name = model_name
        self._last_history_content: str | None = None
        self._mode: PromptMode = PromptMode.AGENT
        self._thinking = thinking
        self._thinking_effort = thinking_effort or ("high" if thinking else "off")
        self._placeholder_manager = PromptPlaceholderManager()
        # Keep the old attribute for test compatibility and for any external imports.
        self._attachment_cache = self._placeholder_manager.attachment_cache
        self._last_tip_rotate_time: float = time.monotonic()
        self._last_submission_was_running = False
        self._last_input_activity_time: float = 0.0
        self._suppress_auto_completion: bool = False
        self._input_activity_event: asyncio.Event = asyncio.Event()
        self._running_prompt_previous_mode: PromptMode | None = None
        self._running_prompt_delegate: RunningPromptDelegate | None = None
        # Set by the shell the instant an agent turn is dispatched, before the
        # running-prompt delegate attaches. Bridges the race where the prompt is
        # resumed (and can repaint the input card) before the delegate exists —
        # without it, the pre-attach frame paints the card chrome that then
        # fossilizes above the stream. Cleared on attach/detach. See
        # _input_card_hidden_pre_stream.
        self._turn_starting: bool = False
        self._sticky_input = sticky_input
        self._latest_todos: tuple[TodoDisplayItem, ...] = ()
        self._modal_delegates: list[RunningPromptDelegate] = []
        self._shortcut_help_open = False
        self._prompt_buffer_container: ConditionalContainer | None = None
        self._slash_menu_control: SlashCommandMenuControl | None = None
        self._last_ui_state: PromptUIState = PromptUIState.NORMAL_INPUT
        self._suspended_buffer_document: Document | None = None
        self._prompt_state = PromptState(mode=self._mode)
        clipboard_available = self._clipboard_adapter.is_text_available()
        media_clipboard_available = self._clipboard_adapter.is_media_available()
        self._tips = _build_toolbar_tips(clipboard_available or media_clipboard_available)
        self._tip_rotation_index: int = random.randrange(len(self._tips)) if self._tips else 0

        history_entries = self._history_store.load()
        history = InMemoryHistory()
        for entry in history_entries:
            history.append_string(entry.content)

        if history_entries:
            # for consecutive deduplication
            self._last_history_content = history_entries[-1].content

        from pythinker_code.ui.shell.slash import slash_command_arg_suggestions

        self._slash_arg_suggestions = slash_command_arg_suggestions

        # Build completers
        self._agent_slash_completer = SlashCommandCompleter(
            agent_mode_slash_commands,
            annotate_meta=True,
            command_scope="command",
            is_task_running=lambda: self._running_prompt_delegate is not None,
            arg_suggestions=self._slash_arg_suggestions,
        )
        self._workspace_root = HostPath.cwd()
        self._workspace_index = WorkspaceIndex(
            get_current_host(),
            self._lifecycle,
            self._workspace_root,
            on_publish=self._on_workspace_snapshot_published,
        )
        self._lifecycle.register_closer("workspace index", self._workspace_index.aclose)
        self._file_mention_completer = HostFileMentionCompleter(self._workspace_index)
        self._agent_mode_completer = merge_completers(
            [
                self._agent_slash_completer,
                self._file_mention_completer,
            ],
            deduplicate=True,
        )
        self._shell_slash_completer = SlashCommandCompleter(
            shell_mode_slash_commands,
            annotate_meta=True,
            command_scope="shell",
            arg_suggestions=self._slash_arg_suggestions,
        )
        self._shell_mode_completer = self._shell_slash_completer
        self._agent_command_names = _command_name_set(agent_mode_slash_commands)
        self._shell_command_names = _command_name_set(shell_mode_slash_commands)
        self._input_highlight_lexer = InputHighlightLexer(
            lambda: (
                self._shell_command_names
                if self._mode == PromptMode.SHELL
                else self._agent_command_names
            ),
            agent_mode=lambda: self._mode == PromptMode.AGENT,
            arg_suggestions=self._slash_arg_suggestions,
        )
        self._slash_auto_suggest = SlashCommandAutoSuggest(
            lambda: (
                self._shell_command_names
                if self._mode == PromptMode.SHELL
                else self._agent_command_names
            ),
            exact_suggestions=self._exact_slash_suggestions,
            arg_suggestions=self._slash_arg_suggestions,
        )

        # Build key bindings
        _kb = KeyBindings()

        def _accept_completion(buff: Buffer) -> None:
            """Accept the current or first completion, suppressing re-completion."""
            state = buff.complete_state
            if state is None:
                return
            completion = state.current_completion
            if completion is None:
                if not state.completions:
                    return
                completion = state.completions[0]
            self._suppress_auto_completion = True
            try:
                buff.apply_completion(completion)
            finally:
                self._suppress_auto_completion = False

        def _is_slash_completion() -> bool:
            """True when the active completion menu is for a slash command."""
            buff = self._session.default_buffer
            return bool(
                buff.complete_state
                and buff.complete_state.completions
                and self._slash_completion_active(buff.document)
            )

        _slash_completion_filter = has_completions & Condition(_is_slash_completion)
        _non_slash_completion_filter = has_completions & ~Condition(_is_slash_completion)

        @_kb.add("enter", filter=_slash_completion_filter)
        def _(event: KeyPressEvent) -> None:
            """Slash command completion: accept and submit in one step."""
            _accept_completion(event.current_buffer)
            event.current_buffer.validate_and_handle()

        @_kb.add("escape", eager=True, filter=_slash_completion_filter)
        def _(event: KeyPressEvent) -> None:
            """Slash completion: Escape discards a draft command, or dismisses
            the argument menu when there is no draft command to remove."""
            buffer = event.current_buffer
            if not _discard_slash_command(buffer):
                # Slash-argument completion (e.g. "/model gpt"): the eager
                # binding swallowed Escape but there is no root command to
                # strip, so dismiss the completion menu explicitly instead of
                # leaving it open.
                buffer.cancel_completion()
            event.app.invalidate()

        @_kb.add("enter", filter=_non_slash_completion_filter)
        def _(event: KeyPressEvent) -> None:
            """Non-slash completion (file mentions, etc.): accept only."""
            _accept_completion(event.current_buffer)

        def _has_slash_suggestion() -> bool:
            buff = self._session.default_buffer
            return bool(buff.suggestion and buff.suggestion.text)

        @_kb.add("tab", filter=Condition(_has_slash_suggestion))
        def _(event: KeyPressEvent) -> None:
            """Slash command ghost suggestion: Tab completes the word inline."""
            suggestion = event.current_buffer.suggestion
            if suggestion and suggestion.text:
                event.current_buffer.insert_text(suggestion.text)

        @_kb.add("?", eager=True)
        def _(event: KeyPressEvent) -> None:
            """Toggle a compact shortcuts popup when the input row is empty."""
            if self._active_prompt_delegate() is not None:
                event.current_buffer.insert_text("?")
                return
            if event.current_buffer.text.strip():
                event.current_buffer.insert_text("?")
                return
            self.toggle_shortcut_help()

        @_kb.add("c-x", eager=True)
        def _(event: KeyPressEvent) -> None:
            if self._active_prompt_delegate() is not None:
                return
            self.toggle_mode()
            from pythinker_code.telemetry import track

            track("shortcut_mode_switch", to_mode=self._mode.value)

        @_kb.add("s-tab", eager=True)
        def _(event: KeyPressEvent) -> None:
            """Cycle thinking effort with Shift+Tab."""
            if self._active_prompt_delegate() is not None:
                return
            if self._thinking_effort_cycle_callback is not None:

                async def _cycle() -> None:
                    assert self._thinking_effort_cycle_callback is not None
                    new_level = await self._thinking_effort_cycle_callback()
                    from pythinker_code.telemetry import track

                    if new_level is None:
                        message = (
                            "Current model uses native reasoning"
                            if self._uses_native_thinking()
                            else "Current model does not support thinking"
                        )
                        toast(
                            message,
                            topic="thinking_level",
                            duration=3.0,
                            immediate=True,
                        )
                    else:
                        self._thinking_effort = new_level
                        self._thinking = new_level != "off"
                        track("shortcut_thinking_cycle", level=new_level)
                        toast(
                            f"Thinking level: {new_level}",
                            topic="thinking_level",
                            duration=3.0,
                            immediate=True,
                        )
                    event.app.invalidate()

                event.app.create_background_task(_cycle())
            event.app.invalidate()

        @_kb.add("escape", "enter", eager=True)
        @_kb.add("c-j", eager=True)
        def _(event: KeyPressEvent) -> None:
            """Insert a newline when Alt-Enter or Ctrl-J is pressed."""
            from pythinker_code.telemetry import track

            track("shortcut_newline")
            event.current_buffer.insert_text("\n")

        @_kb.add("c-o", eager=True)
        def _(event: KeyPressEvent) -> None:
            """Expand active transcript content, or open current buffer in external editor."""
            if self._active_prompt_delegate() is not None:
                if self._should_handle_running_prompt_key("c-o"):
                    self._handle_running_prompt_key("c-o", event)
                return

            from pythinker_code.telemetry import track

            track("shortcut_editor")
            self._open_in_external_editor(event)

        @_kb.add("c-l", eager=True)
        def _(event: KeyPressEvent) -> None:
            """Erase and fully repaint the screen (recovery from console damage)."""
            self._hard_repaint(event)

        def _has_staged_suggestion_prefill() -> bool:
            return bool(getattr(self, "_staged_suggestion_prefill", None))

        @_kb.add("escape", "s", eager=True, filter=Condition(_has_staged_suggestion_prefill))
        def _(event: KeyPressEvent) -> None:
            """Accept the latest agent suggestion into the prompt buffer."""
            if self.accept_staged_suggestion_prefill():
                from pythinker_code.telemetry import track

                track("suggestion_accepted")
            event.app.invalidate()

        @_kb.add(
            "up",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("up")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("up", event)

        @_kb.add(
            "down",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("down")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("down", event)

        @_kb.add(
            "left",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("left")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("left", event)

        @_kb.add(
            "right",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("right")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("right", event)

        @_kb.add(
            "tab",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("tab")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("tab", event)

        @_kb.add(
            "enter",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("enter")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("enter", event)

        @_kb.add(
            "space",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("space")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("space", event)

        @_kb.add(
            "c-s",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("c-s")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("c-s", event)

        @_kb.add(
            "c-e",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("c-e")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("c-e", event)

        @_kb.add(
            "c-t",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("c-t")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("c-t", event)

        @_kb.add(
            "c-c",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("c-c")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("c-c", event)

        @_kb.add(
            "c-d",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("c-d")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("c-d", event)

        @_kb.add(
            "escape",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("escape")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("escape", event)

        @_kb.add(
            "escape",
            eager=True,
            filter=Condition(lambda: self._shortcut_help_open),
        )
        def _(event: KeyPressEvent) -> None:
            self.close_shortcut_help()

        @_kb.add(
            "1",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("1")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("1", event)

        @_kb.add(
            "2",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("2")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("2", event)

        @_kb.add(
            "3",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("3")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("3", event)

        @_kb.add(
            "4",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("4")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("4", event)

        @_kb.add(
            "5",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("5")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("5", event)

        @_kb.add(
            "6",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("6")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("6", event)

        @_kb.add(Keys.BracketedPaste, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._handle_bracketed_paste(event)

        if clipboard_available or media_clipboard_available:

            @_kb.add("c-v", eager=True)
            def _(event: KeyPressEvent) -> None:
                from pythinker_code.telemetry import track

                track("shortcut_paste")
                if self._try_paste_media(event):
                    return
                if clipboard_available:
                    clipboard_text = self._clipboard_adapter.paste_text(event.app.clipboard)
                    if clipboard_text is None:
                        return
                    self._insert_pasted_text(event.current_buffer, clipboard_text)
                    event.app.invalidate()

        # Only use PyperclipClipboard when pyperclip actually works.
        # PromptSession built-in keybindings (ctrl-k, ctrl-w, ctrl-y)
        # use clipboard without error handling, so a broken clipboard
        # object would crash the UI.
        clipboard = self._clipboard_adapter.create_text_clipboard(available=clipboard_available)

        self._session = PromptSession[str](
            message=self._render_message,
            completer=self._agent_mode_completer,
            auto_suggest=self._slash_auto_suggest,
            complete_while_typing=True,
            reserve_space_for_menu=6,
            key_bindings=_kb,
            clipboard=clipboard,
            history=history,
            prompt_continuation=self._render_prompt_continuation,
            bottom_toolbar=self._render_bottom_toolbar,
            style=get_prompt_style(),
            lexer=self._input_highlight_lexer,
        )
        self._current_prompt_frame: PromptFrame | None = None
        self._current_footer_view_model: FooterViewModel | None = None
        self._prompt_frame_collector = self._make_prompt_frame_collector()

        def _capture_prompt_frame(app: Application[str]) -> None:
            size = app.output.get_size()
            self._current_prompt_frame = self._prompt_frame_collector.capture(
                columns=size.columns,
                terminal_rows=size.rows,
            )
            try:
                self._current_footer_view_model = self._build_footer_view_model(size.columns)
            except CwdLostError as exc:
                self._current_footer_view_model = None
                app.exit(exception=exc)

        self._session.app.before_render.add_handler(_capture_prompt_frame)
        self._session.app.after_render.add_handler(self._clear_prompt_frame_snapshot)

        # Throttle redraws so the fast streaming-reveal cadence can't overwhelm
        # slower terminals (best practice for "invalidate is called a lot").
        # prompt_toolkit's renderer is already differential (only emits changed
        # cells), so this caps frame rate without forcing full repaints.
        # NB: max_render_postpone_time (not min_redraw_interval) — see the
        # constant's definition for why the coroutine-free path matters here.
        self._session.app.max_render_postpone_time = _MAX_RENDER_POSTPONE_S
        # Deliver each redraw atomically (DEC mode 2026) so supporting
        # terminals never paint a half-written frame — the remaining source
        # of visible flicker once the frame rate above is already capped.
        if synchronized_output_enabled():
            install_synchronized_output(self._session.app.output)
        self._session.default_buffer.read_only = Condition(
            lambda: (
                (delegate := self._active_prompt_delegate()) is not None
                and not delegate.running_prompt_allows_text_input()
            )
        )
        self._install_slash_completion_menu()
        self._install_prompt_buffer_visibility()
        self._apply_mode()

        # Allow completion to be triggered when the text is changed,
        # such as when backspace is used to delete text.
        @self._session.default_buffer.on_text_changed.add_handler
        def _(buffer: Buffer) -> None:
            self._last_input_activity_time = time.monotonic()
            self._input_activity_event.set()
            self._dispatch(BufferObserved(buffer.document))
            if buffer.complete_while_typing() and not self._suppress_auto_completion:
                buffer.start_completion()

        # Pre-select the first custom-rendered completion as soon as the menu
        # appears. The custom menus paint index 0 as highlighted when
        # complete_index is None, but the underlying complete_state would still
        # be un-positioned, so first arrow-down would move None→0 (no visible
        # change) and require a second press to reach row 2. Setting
        # complete_index=0 here keeps visual and behavioral state aligned.
        @self._session.default_buffer.on_completions_changed.add_handler
        def _(buffer: Buffer) -> None:
            state = buffer.complete_state
            if state is None or not state.completions:
                return
            if state.complete_index is not None:
                return
            if not (
                self._slash_completion_active(buffer.document)
                or LocalFileMentionCompleter.should_complete(buffer.document)
            ):
                return
            state.complete_index = 0

        self._status_refresh_task: asyncio.Task[None] | None = None

    def _install_slash_completion_menu(self) -> None:
        float_container = _find_prompt_float_container(self._session.layout.container)
        if not isinstance(float_container, FloatContainer):
            return

        self._slash_menu_control = SlashCommandMenuControl(
            left_padding=self._slash_menu_left_padding
        )
        slash_completion_filter = Condition(self._should_show_slash_completion_menu)
        non_slash_completion_filter = has_completions & ~slash_completion_filter
        slash_menu = ConditionalContainer(
            Window(
                content=self._slash_menu_control,
                dont_extend_height=True,
                # Cap leaves room for the gap + separator + footer chrome (3 rows)
                # while still showing ~9 commands; preferred_height clamps to the
                # terminal's available height so it never overflows a short window.
                height=Dimension(max=12),
                style="class:slash-completion-menu",
            ),
            filter=has_completions & slash_completion_filter,
        )
        non_slash_menu = ConditionalContainer(
            Window(
                content=LocalFileMentionMenuControl(left_padding=self._mention_menu_left_padding),
                dont_extend_height=True,
                height=Dimension(max=8),
                style="class:file-completion-menu",
            ),
            filter=non_slash_completion_filter,
        )
        root = self._session.layout.container
        buffer_container = _find_default_buffer_container(root, self._session.default_buffer)
        inserted_inline_menus = False
        if isinstance(root, HSplit) and buffer_container is not None:
            children = cast(list[object], root.children)
            for index, child in enumerate(children):
                if _container_contains(child, buffer_container):
                    children.insert(index + 1, slash_menu)
                    children.insert(index + 2, non_slash_menu)
                    inserted_inline_menus = True
                    break

        original_float = next(
            (
                float_
                for float_ in float_container.floats
                if isinstance(float_.content, CompletionsMenu)
            ),
            None,
        )
        if original_float is None:
            return
        if inserted_inline_menus:
            # Inline menus are inserted directly below the compact input row.
            # Hide the original floating menu: with the prompt anchored near the
            # bottom of the terminal it can render off-screen, and when it does
            # fit it would duplicate the inline file-mention menu.
            original_float.content = ConditionalContainer(
                original_float.content,
                filter=Condition(lambda: False),
            )
        else:
            original_float.content = ConditionalContainer(
                original_float.content,
                filter=~slash_completion_filter,
            )

    def _install_prompt_buffer_visibility(self) -> None:
        buffer_container = _find_default_buffer_container(
            self._session.layout.container,
            self._session.default_buffer,
        )
        if buffer_container is None:
            return
        buffer_container.filter = buffer_container.filter & Condition(
            self._should_render_input_buffer
        )
        if isinstance(buffer_container.content, Window):
            buffer_window = buffer_container.content
            buffer_window.height = Dimension(min=1, max=5)
            buffer_window.dont_extend_height = Condition(lambda: True)
            buffer_window.style = self._thinking_input_style
            buffer_window.right_margins = [
                *buffer_window.right_margins,
                _PromptRightPaddingMargin(self._input_right_padding),
            ]
        self._prompt_buffer_container = buffer_container

    def _active_slash_completer(self) -> SlashCommandCompleter:
        if self._mode == PromptMode.SHELL:
            return self._shell_slash_completer
        return self._agent_slash_completer

    def _slash_completion_active(self, document: Document) -> bool:
        return self._active_slash_completer().completion_active(document)

    def _should_show_slash_completion_menu(self) -> bool:
        document = self._session.default_buffer.document
        return self._slash_completion_active(document)

    def _slash_menu_left_padding(self) -> int:
        side_padding = _card_side_padding()
        if self._mode == PromptMode.SHELL:
            return side_padding + max(1, get_cwidth(f"{PROMPT_SYMBOL_SHELL} ") - 2)
        # Agent mode: prompt prefix uses the transcript marker inside the compact input block.
        return side_padding + 1

    def _mention_menu_left_padding(self) -> int:
        return _card_side_padding()

    def _input_right_padding(self) -> int:
        return _INPUT_RIGHT_PADDING

    def _current_thinking_effort(self) -> str:
        return getattr(self, "_thinking_effort", None) or (
            "high" if getattr(self, "_thinking", False) else "off"
        )

    def _thinking_input_style(self) -> str:
        """Keep typed input text on the normal prompt color, independent of thinking effort."""
        return "class:compact-input"

    def _thinking_prompt_prefix_style(self) -> str:
        """Keep the prompt marker on the normal prompt color, independent of thinking effort."""
        return "class:compact-input.prompt"

    def _exact_slash_suggestions(self) -> dict[str, str]:
        return {"recap": " off" if self._turn_recaps_provider() else " on"}

    def _uses_native_thinking(self) -> bool:
        return model_uses_native_thinking(getattr(self, "_model_capabilities", None))

    def _supports_thinking_effort(self) -> bool:
        return available_thinking_levels(getattr(self, "_model_capabilities", None)) != ("off",)

    def _prompt_separator_style(self, fallback: str) -> str:
        if getattr(self, "_mode", PromptMode.AGENT) != PromptMode.AGENT:
            return fallback
        # The input border is one static frame color regardless of thinking
        # effort; the effort signal lives in the top-border label instead
        # (see _effort_label_fragments) rather than recoloring the whole bar.
        return "class:compact-input.frame"

    def _effort_label_fragments(self) -> StyleAndTextTuples:
        """Dot + level label shown at the right end of the input's top border.

        Returns ``[]`` when there is no effort to choose: non-AGENT modes,
        non-thinking models, and native-thinking models (``always_thinking``
        without a user 'thinking' dial). The dot carries the cold→hot level
        color; the word stays muted so it never competes with the input.
        """
        if getattr(self, "_mode", PromptMode.AGENT) != PromptMode.AGENT:
            return []
        if self._uses_native_thinking() or not self._supports_thinking_effort():
            return []
        level = self._current_thinking_effort()
        return [
            (thinking_dot_style(level), "● "),
            ("class:compact-input.effort", level),
        ]

    def _render_input_top_border(self, columns: int, fallback: str) -> StyleAndTextTuples:
        """Static-grey top border for the input card, effort label flushed right.

        The rule is shortened by the measured label width so the line never
        wraps; when no label applies it spans the full rule like before.
        """
        border_style = self._prompt_separator_style(fallback)
        rule = _prompt_rule(columns)
        label = self._effort_label_fragments()
        if not label:
            return [(border_style, rule)]
        gap = 2
        label_width = sum(get_cwidth(ch) for fragment in label for ch in fragment[1])
        if len(rule) <= gap + label_width:
            # Too narrow for the label plus its gap; a flushed-right label here
            # would overflow and wrap, so fall back to the plain full-width rule.
            return [(border_style, rule)]
        rule_width = len(rule) - gap - label_width
        return [
            (border_style, "─" * rule_width + " " * gap),
            *label,
        ]

    def _mode_model_thinking_label(self) -> str:
        # Thinking effort lives on the input's top-border label, not the footer.
        if not self._model_name:
            return str(self._mode)
        return f"{self._mode} {self._model_name}"

    def _render_prompt_continuation(
        self,
        width: int,
        line_number: int,
        is_soft_wrap: int,
    ) -> FormattedText:
        """Indent wrapped input rows to the same column as the first text row."""
        del line_number, is_soft_wrap
        return FormattedText([(self._thinking_input_style(), " " * max(0, width))])

    def _render_message(self) -> FormattedText:
        if self._mode == PromptMode.SHELL:
            return self._render_shell_prompt_message()
        return self._render_agent_prompt_message()

    def _render_shell_prompt_message(self) -> FormattedText:
        frame = self._prompt_frame_for_render()
        columns = frame.columns
        # Shell mode has no running-prompt scene allocator, so the footer keeps
        # its natural height within the terminal. Refresh the budget every render
        # (not just on the agent path) so a mode switch or resize cannot leave
        # _fit_toolbar_to_terminal clipping against a stale agent-mode value.
        self._prompt_footer_row_budget = frame.terminal_rows
        self._prompt_frame_update_notice = self._update_notice_for_render()
        fragments: FormattedText = FormattedText()

        if getattr(self, "_shortcut_help_open", False):
            fragments.extend(self._render_shortcut_help(columns))
            ensure_prompt_newline(fragments)

        # Dynamic preamble (agent status + modal/interactive body). Keep it
        # within the visible terminal area so it cannot overlap the input/footer.
        preamble: FormattedText = FormattedText()
        agent_status = self._render_agent_status(frame.agent_status)
        if agent_status:
            preamble.extend(agent_status)
            ensure_prompt_newline(preamble)

        body = self._render_interactive_body(frame.interactive_body)
        if body:
            preamble.extend(body)
            ensure_prompt_newline(preamble)

        pinned = self._render_pinned_status_tail(frame.pinned_tail)
        if preamble or pinned:
            preamble = self._fit_preamble_with_pinned_tail(
                preamble,
                pinned,
                columns,
                _prompt_preamble_max_rows(frame.terminal_rows),
            )
            fragments.extend(preamble)

        if frame.modal_active:
            return fragments
        if is_card_style():
            ensure_prompt_newline(fragments)
            tc = get_toolbar_colors()
            fragments.append((tc.separator, _prompt_rule(columns)))
            fragments.append(("", "\n"))
        elif preamble:
            fragments.append(("", "\n"))
        fragments.append(("", _card_side_indent()))
        fragments.append(("bold", f"{PROMPT_SYMBOL_SHELL} "))
        return fragments

    def _hard_repaint(self, event: KeyPressEvent) -> None:
        """Erase the screen and absolutely repaint the prompt (Ctrl+L escape hatch).

        Pins prompt_toolkit's default clear-screen behavior explicitly: when the
        real screen has diverged from the renderer's frame model (Windows ConPTY
        replay, a child process writing to the shared console), the differential
        renderer keeps emitting empty diffs and the UI looks blank; this forces
        an absolute frame. Explicit so future custom bindings cannot silently
        shadow the recovery path.
        """
        try:
            event.app.renderer.clear()
        except Exception as exc:  # noqa: BLE001 — recovery must never crash the prompt
            logger.debug("Hard repaint (ctrl-l) failed: {}", exc)

    def _open_in_external_editor(self, event: KeyPressEvent) -> None:
        """Open the current buffer content in an external editor."""
        from prompt_toolkit.application.run_in_terminal import run_in_terminal

        from pythinker_code.utils.editor import edit_text_in_editor, get_editor_command

        configured = self._editor_command_provider()

        if get_editor_command(configured) is None:
            toast("No editor found. Set $VISUAL/$EDITOR or run /editor.")
            return

        buff = event.current_buffer
        original_text = buff.text
        editor_text = self._get_placeholder_manager().expand_for_editor(original_text)

        async def _run_editor() -> None:
            result = await run_in_terminal(
                lambda: edit_text_in_editor(editor_text, configured), in_executor=True
            )
            if result is not None:
                refolded = self._get_placeholder_manager().refold_after_editor(
                    result, original_text
                )
                buff.document = Document(text=refolded, cursor_position=len(refolded))

        event.app.create_background_task(_run_editor())

    def _apply_mode(self, event: KeyPressEvent | None = None) -> None:
        # Apply mode to the active buffer (not the PromptSession itself)
        try:
            buff = event.current_buffer if event is not None else self._session.default_buffer
        except Exception:
            buff = None

        if self._mode == PromptMode.SHELL:
            if buff is not None:
                buff.completer = self._shell_mode_completer
        else:
            if buff is not None:
                buff.completer = self._agent_mode_completer
        self._sync_erase_when_done()

    def _sync_erase_when_done(self) -> None:
        app = getattr(self._session, "app", None)
        if app is not None:
            app.erase_when_done = getattr(self, "_mode", PromptMode.AGENT) == PromptMode.AGENT

    def _active_modal_delegate(self) -> RunningPromptDelegate | None:
        modal_delegates = getattr(self, "_modal_delegates", [])
        if not modal_delegates:
            return None
        _, delegate = max(
            enumerate(modal_delegates),
            key=lambda item: (item[1].modal_priority, item[0]),
        )
        return delegate

    def _active_prompt_delegate(self) -> RunningPromptDelegate | None:
        if delegate := self._active_modal_delegate():
            return delegate
        return getattr(self, "_running_prompt_delegate", None)

    def _make_prompt_frame_collector(self) -> PromptFrameCollector:
        return PromptFrameCollector(
            resolve_modal=self._active_modal_delegate,
            resolve_running=lambda: getattr(self, "_running_prompt_delegate", None),
            render_background_status=self._render_background_working_status,
            render_status_block=self._render_status_block,
            input_is_empty=lambda: (
                not getattr(
                    getattr(getattr(self, "_session", None), "default_buffer", None), "text", ""
                )
            ),
            turn_is_starting=lambda: getattr(self, "_turn_starting", False),
        )

    def _clear_prompt_frame_snapshot(self, _app: Application[str] | None = None) -> None:
        """after_render handler: drop the captured frame and its per-frame update
        notice together, so a later render can never read a snapshot captured for a
        stale frame/mode."""
        self._current_prompt_frame = None
        self._current_footer_view_model = None
        self._prompt_frame_update_notice = None

    def _prompt_frame_for_render(self, *, columns: int | None = None) -> PromptFrame:
        current = getattr(self, "_current_prompt_frame", None)
        if current is not None:
            return current
        app = get_app_or_none()
        size = app.output.get_size() if app is not None else None
        frame_columns = (
            columns if columns is not None else (size.columns if size is not None else 80)
        )
        terminal_rows = getattr(size, "rows", 24) if size is not None else 24
        collector = getattr(self, "_prompt_frame_collector", None)
        if collector is None:
            collector = self._make_prompt_frame_collector()
            self._prompt_frame_collector = collector
        return collector.capture(columns=frame_columns, terminal_rows=terminal_rows)

    def _active_ui_state(self) -> PromptUIState:
        delegate = self._active_modal_delegate()
        if delegate is None:
            return PromptUIState.NORMAL_INPUT
        if delegate.running_prompt_hides_input_buffer():
            return PromptUIState.MODAL_HIDDEN_INPUT
        if delegate.running_prompt_allows_text_input():
            return PromptUIState.MODAL_TEXT_INPUT
        return PromptUIState.NORMAL_INPUT

    def _input_card_hidden_pre_stream(self, captured: bool | None = None) -> bool:
        """Gate the empty pre-stream input surface until the first commit.

        Most running frames keep the input card visible. The only exception is
        the first transition into committed scrollback: prompt_toolkit can
        otherwise fossilize the card above the stream. Once that first commit
        establishes the stream geometry, the card repaints below the stream.
        Skipped when the user has typed (non-empty buffer) or a modal owns the
        input line.
        """
        if captured is not None:
            return captured
        if self._active_modal_delegate() is not None:
            return False
        # Direct attribute access (not getattr-with-default): these are set in
        # __init__, so an init regression should fail loudly, not silently drop
        # the input guard and re-introduce the ghost. The delegate method stays a
        # getattr: it is an optional RunningPromptDelegate extension only the
        # live view implements.
        if self._turn_starting:
            hide = True
        else:
            delegate = self._running_prompt_delegate
            hide = (
                delegate is not None
                and getattr(delegate, "running_prompt_hide_input_card", lambda: False)()
            )
        if not hide:
            return False
        return not self._session.default_buffer.text

    def _should_render_input_buffer(self) -> bool:
        if self._active_ui_state() == PromptUIState.MODAL_HIDDEN_INPUT:
            return False
        # Before the running-prompt delegate attaches, there is no pinned spinner
        # frame to own the geometry; hiding this window prevents the prompt row
        # from fossilizing above the spinner. After attach, keep it visible
        # because prompt_toolkit renders the ❯ marker in this buffer window.
        return not (self._turn_starting and not self._session.default_buffer.text)

    def _should_handle_running_prompt_key(self, key: str) -> bool:
        delegate = self._active_prompt_delegate()
        return delegate is not None and delegate.should_handle_running_prompt_key(key)

    def _handle_running_prompt_key(self, key: str, event: KeyPressEvent) -> None:
        delegate = self._active_prompt_delegate()
        if delegate is None:
            return
        delegate.handle_running_prompt_key(key, event)
        event.app.invalidate()

    def _app_for_repaint(self) -> Application[str] | None:
        # Prefer the session's own Application over get_app_or_none(). The latter
        # resolves the running app from the current context, and returns None
        # inside background coroutines (the status refresh task, the MCP
        # completion task), which silently drops the repaint. The stored
        # reference is correct regardless of which task calls it.
        session = getattr(self, "_session", None)
        app = getattr(session, "app", None) if session is not None else None
        return app if app is not None else get_app_or_none()

    def invalidate(self) -> None:
        self._sync_prompt_ui_state()
        app = self._app_for_repaint()
        if app is not None:
            app.invalidate()

    def _sync_prompt_ui_state(self) -> None:
        self._last_ui_state = self._active_ui_state()

    def _render_agent_prompt_message(self) -> FormattedText:
        frame = self._prompt_frame_for_render()
        columns = frame.columns
        fragments: FormattedText = FormattedText()

        update_notice = self._update_notice_for_render()
        self._prompt_frame_update_notice = update_notice
        footer_rows = 3 + (1 if update_notice else 0)

        def finish(scene: FormattedText, *, input_rows: int) -> FormattedText:
            scene_rows = len(_formatted_text_display_rows(scene, columns))
            if scene_rows + footer_rows <= frame.terminal_rows:
                self._prompt_footer_row_budget = footer_rows
                return scene
            has_content_before_pinned = bool(body_rows) or bool(
                agent_status and any(text for _, text, *_ in agent_status)
            )
            allocation = allocate_prompt_scene_rows(
                PromptSceneBudget(terminal_rows=frame.terminal_rows),
                modal_rows=body_rows if modal_active else 0,
                input_rows=input_rows,
                footer_rows=footer_rows,
                pinned_rows=pinned_rows,
                separator_rows=(
                    1 if not modal_active and pinned_rows and has_content_before_pinned else 0
                ),
                body_rows=0 if modal_active else body_rows,
                status_rows=status_rows,
                shortcut_rows=(
                    len(_formatted_text_display_rows(self._render_shortcut_help(columns), columns))
                    if getattr(self, "_shortcut_help_open", False) and not modal_active
                    else 0
                ),
            )
            self._prompt_footer_row_budget = allocation.footer_rows
            if modal_active:
                modal_scene = FormattedText()
                if allocation.status_rows:
                    modal_scene.extend(
                        _fit_prompt_scene_to_rows(
                            agent_status,
                            columns,
                            allocation.status_rows,
                        )
                    )
                    ensure_prompt_newline(modal_scene)
                if allocation.modal_rows:
                    modal_scene.extend(
                        _fit_prompt_scene_to_rows(body, columns, allocation.modal_rows)
                    )
                if allocation.pinned_rows:
                    ensure_prompt_newline(modal_scene)
                    modal_scene.extend(
                        _fit_prompt_scene_to_rows(
                            pinned,
                            columns,
                            allocation.pinned_rows,
                            show_clip_hint=False,
                        )
                    )
                return _fit_prompt_scene_to_rows(
                    modal_scene,
                    columns,
                    allocation.prompt_rows,
                )

            scene_rows = _formatted_text_display_rows(scene, columns)
            input_region = FormattedText()
            if allocation.input_rows:
                _extend_rows(input_region, scene_rows[-input_rows:])

            overflow_scene = FormattedText()

            def append_region(region: FormattedText, rows: int) -> None:
                if rows <= 0:
                    return
                fitted = _fit_prompt_scene_to_rows(
                    region,
                    columns,
                    rows,
                    show_clip_hint=False,
                    drop_blank_rows=True,
                )
                if not fitted:
                    return
                if overflow_scene:
                    ensure_prompt_newline(overflow_scene)
                overflow_scene.extend(fitted)

            if allocation.shortcut_rows:
                append_region(self._render_shortcut_help(columns), allocation.shortcut_rows)
            append_region(agent_status, allocation.status_rows)
            append_region(body, allocation.body_rows)
            if (
                allocation.separator_rows
                and allocation.pinned_rows
                and (allocation.body_rows or allocation.status_rows)
            ):
                ensure_prompt_newline(overflow_scene)
                overflow_scene.append(("", "\n"))
            append_region(pinned, allocation.pinned_rows)
            append_region(input_region, allocation.input_rows)
            return _fit_prompt_scene_to_rows(
                overflow_scene,
                columns,
                allocation.prompt_rows,
                show_clip_hint=False,
            )

        # 1–2. Dynamic preamble — agent status is always rendered from the
        # running prompt delegate, and body comes from the active modal/delegate.
        # Cap the visible rows so large cards do not overwrite the input/footer.
        # When a modal is active, preserve the whole modal body and clip older
        # agent status above it first; approval/question controls must remain usable.
        agent_status = self._render_agent_status(frame.agent_status)
        body = self._render_interactive_body(frame.interactive_body)
        pinned = self._render_pinned_status_tail(frame.pinned_tail)
        body_rows = (
            len(_formatted_text_display_rows(body, columns))
            if body and any(fragment for _, fragment, *_ in body)
            else 0
        )
        status_rows = (
            len(_formatted_text_display_rows(agent_status, columns))
            if agent_status and any(fragment for _, fragment, *_ in agent_status)
            else 0
        )
        pinned_rows = (
            len(_formatted_text_display_rows(pinned, columns))
            if pinned and any(fragment for _, fragment, *_ in pinned)
            else 0
        )
        max_rows = _prompt_preamble_max_rows(frame.terminal_rows)
        modal_active = frame.modal_active

        if getattr(self, "_shortcut_help_open", False) and not modal_active:
            fragments.extend(self._render_shortcut_help(columns))
            ensure_prompt_newline(fragments)

        if not modal_active and frame.running_prompt_active and is_card_style():
            input_card_hidden = self._input_card_hidden_pre_stream(frame.input_card_hidden)
            running_body = body
            preamble = FormattedText()
            if agent_status and any(text for _, text, *_ in agent_status):
                preamble.extend(agent_status)
                ensure_prompt_newline(preamble)
            if running_body and any(text for _, text, *_ in running_body):
                preamble.extend(running_body)
                ensure_prompt_newline(preamble)
            if (preamble and any(text for _, text, *_ in preamble)) or pinned_rows:
                preamble = self._fit_preamble_with_pinned_tail(
                    preamble,
                    pinned,
                    columns,
                    max_rows,
                )
            if preamble and any(text for _, text, *_ in preamble):
                fragments.extend(preamble)

            if input_card_hidden and self._input_card_chrome_hidden(frame.input_chrome_hidden):
                return finish(fragments, input_rows=0)

            tc = get_toolbar_colors()
            scene_fragments: FormattedText = FormattedText()
            if fragments and any(text for _, text, *_ in fragments):
                scene_fragments.extend(fragments)
                ensure_prompt_newline(scene_fragments)

            placeholder_fragments = (
                self._render_running_prompt_placeholder(frame.placeholder)
                if not input_card_hidden
                else FormattedText()
            )

            scene_fragments.extend(self._render_input_top_border(columns, tc.separator))
            scene_fragments.append(("", "\n"))
            scene_fragments.append(("", _card_side_indent()))
            scene_fragments.append(
                (self._thinking_prompt_prefix_style(), f"{PROMPT_SYMBOL_AGENT_INPUT} ")
            )
            if placeholder_fragments:
                scene_fragments.extend(placeholder_fragments)
            return finish(scene_fragments, input_rows=2)

        if modal_active and body:
            status_budget = max(0, max_rows - body_rows - pinned_rows)
            if agent_status and status_budget > 0:
                clipped_status = _fit_formatted_text_to_rows(
                    agent_status,
                    columns,
                    status_budget,
                    preserve_tail_rows=1,
                )
                fragments.extend(clipped_status)
                ensure_prompt_newline(fragments)
            fragments.extend(body)
            ensure_prompt_newline(fragments)
            if pinned_rows:
                fragments.extend(pinned)
                ensure_prompt_newline(fragments)
        else:
            preamble: FormattedText = FormattedText()
            if agent_status:
                preamble.extend(agent_status)
                ensure_prompt_newline(preamble)
            if body:
                preamble.extend(body)
                ensure_prompt_newline(preamble)
            if preamble or pinned_rows:
                preamble = self._fit_preamble_with_pinned_tail(
                    preamble,
                    pinned,
                    columns,
                    max_rows,
                )
                fragments.extend(preamble)

        # 3. When a modal is active, skip the normal input chrome.
        if modal_active:
            return finish(fragments, input_rows=0)

        # Hide editable input content during the narrow pre-stream/first-handoff
        # frame, but keep the empty card chrome visible so the prompt bar does not
        # disappear while the agent is loading.
        if self._input_card_hidden_pre_stream(frame.input_card_hidden):
            if self._input_card_chrome_hidden(frame.input_chrome_hidden):
                return finish(fragments, input_rows=0)
            if is_card_style():
                ensure_prompt_newline(fragments)
                tc = get_toolbar_colors()
                fragments.extend(self._render_input_top_border(columns, tc.separator))
                fragments.append(("", "\n"))
                fragments.append(("", _card_side_indent()))
            else:
                fragments.append(("", "\n"))
            fragments.append(
                (self._thinking_prompt_prefix_style(), f"{PROMPT_SYMBOL_AGENT_INPUT} ")
            )
            return finish(fragments, input_rows=2 if is_card_style() else 1)

        if is_card_style():
            ensure_prompt_newline(fragments)
            tc = get_toolbar_colors()
            fragments.extend(self._render_input_top_border(columns, tc.separator))
            fragments.append(("", "\n"))
            fragments.append(("", _card_side_indent()))
        else:
            fragments.append(("", "\n"))
        fragments.append((self._thinking_prompt_prefix_style(), f"{PROMPT_SYMBOL_AGENT_INPUT} "))
        return finish(fragments, input_rows=2 if is_card_style() else 1)

    def _render_shortcut_help(self, columns: int) -> FormattedText:
        """Render a small keyboard-shortcuts popup above the prompt."""
        from pythinker_code.ui.shell.keymap import keybinding_help

        side_padding = min(_card_side_padding(), max(0, (columns - 2) // 2))
        indent = " " * side_padding
        available = max(1, columns - side_padding * 2)
        width = min(88, available)
        help_ids = {
            "app.prompt.help",
            "app.mode.toggle",
            "app.thinking.cycle",
            "app.shell.oneshot",
            "app.editor.external",
            "app.prompt.newline",
            "app.clipboard.paste",
            "app.mention.files",
            "app.command.slash",
            "app.tools.expand",
            "app.todos.toggle",
        }
        rows = [
            (
                "/".join(info.keys),
                info.description
                if info.context in {"", "prompt", "agent prompt"}
                else f"{info.description} ({info.context})",
            )
            for info in keybinding_help()
            if info.name in help_ids
        ]
        rows.append(("esc", "close shortcuts"))
        key_width = min(20, max(get_cwidth(key) for key, _ in rows) + 1)
        tc = get_toolbar_colors()
        fragments: FormattedText = FormattedText()
        border = "─" * max(0, width - 2)
        fragments.append(("", indent))
        fragments.append((tc.separator, f"╭{border}╮\n"))
        title = " Shortcuts "
        padding = max(0, width - 2 - get_cwidth(title))
        fragments.append(("", indent))
        fragments.append((tc.separator, "│"))
        fragments.append(("class:slash-completion-menu.command.current", title))
        fragments.append(("class:slash-completion-menu.meta", "".ljust(padding)))
        fragments.append((tc.separator, "│\n"))
        for key, desc in rows:
            line = f"  {key.ljust(key_width)} {desc}"
            pad = max(0, width - 2 - get_cwidth(line))
            fragments.append(("", indent))
            fragments.append((tc.separator, "│"))
            fragments.append(("class:slash-completion-menu.command", line[: width - 2]))
            fragments.append(("class:slash-completion-menu", " " * pad))
            fragments.append((tc.separator, "│\n"))
        fragments.append(("", indent))
        fragments.append((tc.separator, f"╰{border}╯"))
        return fragments

    def _render_agent_status(self, captured: int | FrozenFragments) -> FormattedText:
        """Render captured agent output without consulting the pinned-tail provider."""
        if not isinstance(captured, int):
            return FormattedText(list(captured))

        columns = captured
        running = getattr(self, "_running_prompt_delegate", None)
        pinned_active = False
        if running is not None and isinstance(running, PinnedStatusTailProvider):
            pinned = to_formatted_text(running.render_pinned_status_tail(columns))
            pinned_active = any(text for _, text, *_ in pinned)
        if running is not None and isinstance(running, AgentStatusProvider):
            rendered = to_formatted_text(running.render_agent_status(columns))
            if any(text for _, text, *_ in rendered):
                # A blocking foreground TaskOutput card can be visible while the
                # actual background agent is still running. If the live view does
                # not expose a pinned tail for that state, keep the background
                # verb spinner visible above the prompt instead of showing only
                # the footer count.
                if not pinned_active:
                    background = self._render_background_working_status(columns)
                    if background:
                        ensure_prompt_newline(rendered)
                        rendered.extend(background)
                # The prompt layer owns the gap below the agent stream: one blank
                # row under the spinner verb (the stream's tail) before the input,
                # mirroring the blank row above it inside the stream.
                ensure_prompt_newline(rendered)
                rendered.append(("", "\n"))
                return rendered

        # An in-flight turn pins its own working indicator (the verb spinner)
        # and the bottom toolbar already reports background work — rendering a
        # count line here too would duplicate it under the executing step.
        fragments = (
            FormattedText([]) if pinned_active else self._render_background_working_status(columns)
        )
        status = self._render_status_block(columns)
        if status:
            ensure_prompt_newline(fragments)
            fragments.extend(status)
        return fragments

    def _render_pinned_status_tail(self, captured: int | FrozenFragments) -> FormattedText:
        """Render the captured trailing status tail."""
        if not isinstance(captured, int):
            return FormattedText(list(captured))
        running = getattr(self, "_running_prompt_delegate", None)
        if running is not None and isinstance(running, PinnedStatusTailProvider):
            rendered = to_formatted_text(running.render_pinned_status_tail(captured))
            if any(text for _, text, *_ in rendered):
                return rendered
        return FormattedText()

    @staticmethod
    def _fit_preamble_with_pinned_tail(
        preamble: FormattedText,
        pinned: FormattedText,
        columns: int,
        max_rows: int,
    ) -> FormattedText:
        """Clip *preamble* to fit *max_rows* while always rendering *pinned*
        (the verb spinner) below it, so the clip hint never covers the spinner.
        """
        if not (pinned and any(fragment for _, fragment, *_ in pinned)):
            # Status is composed before the current live body. Tail clipping
            # therefore drops older status first and keeps the newest live row.
            return _fit_prompt_scene_to_rows(preamble, columns, max_rows)
        out: FormattedText = FormattedText()
        if max_rows <= 0:
            return out
        original: FormattedText = FormattedText()
        original.extend(preamble)
        ensure_prompt_newline(original)
        original.append(("", "\n"))
        original.extend(pinned)
        ensure_prompt_newline(original)
        original.append(("", "\n"))
        if len(_formatted_text_display_rows(original, columns)) <= max_rows:
            return original

        pinned_rows = _formatted_text_display_rows(pinned, columns)
        visible_pinned_rows = pinned_rows[-max_rows:]
        pinned_budget = len(visible_pinned_rows)
        separator_budget = 1 if preamble and max_rows > pinned_budget else 0
        body_budget = max(0, max_rows - pinned_budget - separator_budget)
        if body_budget:
            out.extend(
                _fit_prompt_scene_to_rows(
                    preamble,
                    columns,
                    body_budget,
                    show_clip_hint=body_budget >= 2,
                    drop_blank_rows=True,
                )
            )
            ensure_prompt_newline(out)
            if separator_budget:
                out.append(("", "\n"))
        if pinned_budget:
            pinned_out = FormattedText()
            _extend_rows(pinned_out, visible_pinned_rows)
            out.extend(pinned_out)
        return _fit_prompt_scene_to_rows(out, columns, max_rows)

    def update_pinned_todos(self, items: Sequence[TodoDisplayItem]) -> None:
        """Remember the latest agent todo list for between-turn background waits."""
        self._latest_todos = tuple(items)
        self.invalidate()

    def _render_background_todo_rows(self, columns: int) -> FormattedText:
        todos = tuple(
            todo
            for todo in getattr(self, "_latest_todos", ())
            if todo.status in ("done", "in_progress", "pending") and todo.title.strip()
        )
        if not todos:
            return FormattedText([])

        # Mirror _LiveView._pinned_todo_row exactly — both renderers must show
        # one design: coral ■ + bold default-color (white) title for running
        # rows, muted □ pending, green ✓ with struck muted titles when done.
        tokens = _get_tui_tokens()
        muted_style = f"fg:{tokens.muted}" if tokens.muted else ""
        success_style = f"fg:{tokens.success}" if tokens.success else muted_style
        activity_style = f"fg:{tokens.activity_verb}" if tokens.activity_verb else muted_style
        fragments: FormattedText = FormattedText()
        visible = todos[:5]
        hidden = todos[5:]
        first_prefix = f"  {TRANSCRIPT_TOOL_GUTTER}  "
        continuation_prefix = " " * _display_width(first_prefix)
        for index, todo in enumerate(visible):
            if fragments:
                fragments.append(("", "\n"))
            prefix = first_prefix if index == 0 else continuation_prefix
            if todo.status == "done":
                icon = "✓"
                icon_style = success_style
                title_style = f"{muted_style} strike".strip()
            elif todo.status == "in_progress":
                icon = "■"
                icon_style = activity_style
                title_style = "bold"
            else:
                icon = "□"
                icon_style = muted_style
                title_style = muted_style
            title_budget = max(1, columns - _display_width(prefix) - _display_width(icon) - 1)
            fragments.append((muted_style, prefix))
            fragments.append((icon_style, icon))
            fragments.append(("", " "))
            fragments.append((title_style, _truncate_right(todo.title.strip(), title_budget)))
        if hidden:
            if fragments:
                fragments.append(("", "\n"))
            hidden_pending = sum(1 for todo in hidden if todo.status == "pending")
            hidden_done = sum(1 for todo in hidden if todo.status == "done")
            label = (
                "pending"
                if hidden_pending == len(hidden)
                else "completed"
                if hidden_done == len(hidden)
                else "more"
            )
            fragments.append((muted_style, f"{continuation_prefix}… +{len(hidden)} {label}"))
        return fragments

    def _render_background_working_status(self, columns: int) -> FormattedText:
        """Render a prompt spinner while background work is active.

        Shows only the verb spinner (and pinned todo rows): the bottom toolbar
        already reports the background-task count, so repeating "N background
        agents" above the input would duplicate it.
        """
        counts = self._background_task_counts()
        total = counts.bash + counts.agent
        if total <= 0:
            # Background work drained — reset the elapsed/rate trackers.
            self._bg_status_started_at = None
            self._bg_status_start_tokens = None
            self._bg_last_active_at = None
            samples = getattr(self, "_bg_token_samples", None)
            if samples is not None:
                samples.clear()
            return FormattedText([])
        now = time.monotonic()
        started_at = getattr(self, "_bg_status_started_at", None)
        if started_at is None:
            from pythinker_code.soul.live_tokens import get_total_output_tokens

            started_at = now
            self._bg_status_started_at = now
            self._bg_status_start_tokens = get_total_output_tokens()
            # ponytail: treat freshly-spawned bg work as active so a quiet
            # bash task gets the fast refresh for its first window.
            self._bg_last_active_at = now
        elapsed = max(0.0, now - started_at)
        frame = active_marker_frame(elapsed)
        tokens = _get_tui_tokens()
        muted_style = f"fg:{tokens.muted}" if tokens.muted else ""
        frame_style = f"fg:{tokens.activity_spinner}" if tokens.activity_spinner else muted_style
        frame_text = f"{frame} "
        # ponytail: pure-bash background work (e.g. npm dev) gets a fixed
        # label, not the agent verb spinner — the verbs read as agent work.
        has_agent_work = counts.agent > 0
        verb_text = spinner_message(now) if has_agent_work else "Running in background…"
        metadata = self._background_status_metadata(now)
        suffix = f" {metadata}" if metadata else ""
        if suffix and _display_width(frame_text + verb_text + suffix) > columns:
            # Narrow terminals: drop the metadata first, then trim the verb.
            suffix = ""
        if _display_width(frame_text + verb_text) > columns:
            verb_text = _truncate_right(verb_text, columns - _display_width(frame_text))
        fragments = FormattedText([(frame_style, frame_text)])
        if has_agent_work:
            fragments.extend(shimmer_prompt_fragments(verb_text, now))
        else:
            fragments.append((muted_style, verb_text))
        if suffix:
            fragments.append((muted_style, suffix))
        todo_rows = self._render_background_todo_rows(columns)
        if todo_rows:
            ensure_prompt_newline(fragments)
            fragments.extend(todo_rows)
        return fragments

    def _background_status_metadata(self, now: float) -> str:
        """Compact ``(elapsed, ↓ Nk tokens, N t/s)`` suffix for the line above.

        Same visual language as the live view's working/todo headers. Elapsed
        counts from when background work first appeared; the token readout is the
        session-wide output tokens produced since this stretch began (detached
        background souls feed the same counter), and the rate is a short sliding
        window over it (mirroring ``_ContentBlock._record_token_rate_sample``).
        """
        from pythinker_code.soul import format_token_count
        from pythinker_code.soul.live_tokens import get_total_output_tokens
        from pythinker_code.utils.datetime import format_elapsed

        parts: list[str] = []
        started = getattr(self, "_bg_status_started_at", None)
        if started is not None:
            parts.append(format_elapsed(max(0.0, now - started)))
        start_tokens = getattr(self, "_bg_status_start_tokens", None)
        bg_tokens = (
            max(0, get_total_output_tokens() - start_tokens) if start_tokens is not None else 0
        )
        if bg_tokens:
            parts.append(f"↓ {format_token_count(bg_tokens)} tokens")
            # ponytail: token flow = real agent activity; stamp for the refresh throttle
            self._bg_last_active_at = now
            samples: deque[tuple[float, int]] | None = getattr(self, "_bg_token_samples", None)
            if samples is None:
                samples = deque()
                self._bg_token_samples = samples
            samples.append((now, bg_tokens))
            # 1.5s window, ≥3 samples — the live view's tracker parameters.
            while len(samples) > 1 and now - samples[0][0] > 1.5:
                samples.popleft()
            if len(samples) >= 3:
                window = samples[-1][0] - samples[0][0]
                delta = samples[-1][1] - samples[0][1]
                if window > 0 and delta > 0:
                    rate = int(delta / window)
                    if rate > 0:
                        parts.append(f"{rate} t/s")
        return f"({', '.join(parts)})" if parts else ""

    def _background_task_counts(self) -> BgTaskCounts:
        provider = getattr(self, "_background_task_count_provider", None)
        if provider is None:
            return BgTaskCounts()
        return provider()

    def _has_background_tasks(self) -> bool:
        counts = self._background_task_counts()
        return counts.bash > 0 or counts.agent > 0

    def _bg_refresh_active(self) -> bool:
        """Whether background work warrants the fast refresh rate.

        ponytail: quiet dev servers (no token flow in last _BG_QUIET_THRESHOLD_S)
        drop to idle refresh so the prompt isn't repainted at 12.5fps for hours.
        """
        last_active = getattr(self, "_bg_last_active_at", None)
        if last_active is None:
            return True
        return time.monotonic() - last_active < _BG_QUIET_THRESHOLD_S

    def _render_interactive_body(self, captured: int | FrozenFragments) -> FormattedText:
        """Render the interactive area captured from the active delegate."""
        if not isinstance(captured, int):
            return FormattedText(list(captured))
        delegate = self._active_prompt_delegate()
        if delegate is None:
            return FormattedText([])
        return to_formatted_text(delegate.render_running_prompt_body(captured))

    @staticmethod
    def _render_running_prompt_placeholder(captured: FrozenFragments) -> FormattedText:
        return FormattedText(list(captured))

    @staticmethod
    def _input_card_chrome_hidden(captured: bool) -> bool:
        return captured

    def _render_status_block(self, columns: int) -> FormattedText:
        status_block_provider = getattr(self, "_status_block_provider", None)
        if status_block_provider is None:
            return FormattedText([])
        block = status_block_provider(columns)
        if block is None:
            return FormattedText([])
        return to_formatted_text(block)

    def _render_agent_prompt_label(self) -> FormattedText:
        """Render the prompt label (empty — cursor starts at column 0)."""
        return FormattedText([("", "  ")])

    def _start(self) -> None:
        if self._status_refresh_task is not None and not self._status_refresh_task.done():
            return

        async def _refresh() -> None:
            try:
                while True:
                    git_index = getattr(self, "_git_status_index", None)
                    if isinstance(git_index, GitStatusIndex):
                        # A lost CWD is reported explicitly at the render boundary.
                        with contextlib.suppress(OSError):
                            git_index.request_refresh(HostPath.cwd())

                    app = self._app_for_repaint()
                    if app is not None:
                        app.invalidate()

                    try:
                        asyncio.get_running_loop()
                    except RuntimeError:
                        logger.warning("No running loop found, exiting status refresh task")
                        self._status_refresh_task = None
                        break

                    interval = (
                        _RUNNING_REFRESH_INTERVAL
                        if self._active_prompt_delegate() is not None
                        or (self._has_background_tasks() and self._bg_refresh_active())
                        or (
                            self._fast_refresh_provider is not None
                            and self._fast_refresh_provider()
                        )
                        else _IDLE_REFRESH_INTERVAL
                    )
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                # graceful exit
                pass

        self._status_refresh_task = self._lifecycle.create_task(_refresh())
        if self._statusline_runner is not None:
            self._statusline_runner.start()

    def __enter__(self) -> CustomPromptSession:
        self._bind_resource_bindings()
        try:
            self._start()
            return self
        except BaseException:
            self._reset_resource_bindings()
            raise

    def __exit__(self, *_: object) -> None:
        if self._status_refresh_task is not None and not self._status_refresh_task.done():
            self._status_refresh_task.cancel()
        self._status_refresh_task = None
        if self._statusline_runner is not None:
            self._statusline_runner.cancel()
        self._reset_resource_bindings()

    async def __aenter__(self) -> CustomPromptSession:
        self._bind_resource_bindings()
        try:
            self._start()
            return self
        except BaseException:
            self._reset_resource_bindings()
            raise

    def _bind_resource_bindings(self) -> None:
        self._git_status_token = bind_git_status_index(self._git_status_index)
        self._toast_token = bind_toast_manager(self._toast_manager)
        self._clipboard_token = bind_clipboard_adapter(self._clipboard_adapter)

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        try:
            await self._lifecycle.aclose()
            self._status_refresh_task = None
        finally:
            self._reset_resource_bindings()

    def _reset_resource_bindings(self) -> None:
        # Partially constructed sessions (lifecycle tests) may never have bound tokens.
        clipboard_token = getattr(self, "_clipboard_token", None)
        self._clipboard_token = None
        if clipboard_token is not None:
            reset_clipboard_adapter(clipboard_token)
        toast_token = getattr(self, "_toast_token", None)
        self._toast_token = None
        if toast_token is not None:
            reset_toast_manager(toast_token)
        git_status_token = getattr(self, "_git_status_token", None)
        self._git_status_token = None
        if git_status_token is not None:
            reset_git_status_index(git_status_token)

    @property
    def prompt_history_store(self) -> PromptHistoryStore:
        """Expose this session's history store to the shell slash command."""
        return self._history_store

    def _get_placeholder_manager(self) -> PromptPlaceholderManager:
        manager = getattr(self, "_placeholder_manager", None)
        if manager is None:
            attachment_cache = getattr(self, "_attachment_cache", None)
            manager = PromptPlaceholderManager(attachment_cache=attachment_cache)
            self._placeholder_manager = manager
            self._attachment_cache = manager.attachment_cache
        return manager

    def _insert_pasted_text(self, buffer: Buffer, text: str) -> None:
        normalized = normalize_pasted_text(text)
        if self._mode != PromptMode.AGENT:
            buffer.insert_text(normalized)
            return
        token_or_text = self._get_placeholder_manager().maybe_placeholderize_pasted_text(normalized)
        buffer.insert_text(token_or_text)

    def _handle_bracketed_paste(self, event: KeyPressEvent) -> None:
        self._insert_pasted_text(event.current_buffer, event.data)
        event.app.invalidate()

    def _try_paste_media(self, event: KeyPressEvent) -> bool:
        """Try to paste media from the clipboard.

        Reads the clipboard once and handles all detected content:
        non-image files (videos, PDFs, etc.) are inserted as paths,
        image files are cached and inserted as placeholders.
        Returns True if any media content was inserted.
        """
        try:
            clipboard_adapter = getattr(self, "_clipboard_adapter", None)
            result = (
                clipboard_adapter.paste_media()
                if clipboard_adapter is not None
                else grab_media_from_clipboard()
            )
        except Exception:
            # ImageGrab.grabclipboard() may fail on headless Linux if the
            # real xclip cannot connect to an X server. Silently ignore so
            # that the text-paste fallback can still be attempted.
            return False
        if result is None:
            return False

        parts: list[str] = []

        # 1. Insert file paths (videos, PDFs, etc.)
        if result.file_paths:
            logger.debug("Pasted {count} file path(s) from clipboard", count=len(result.file_paths))
            for p in result.file_paths:
                text = str(p)
                if self._mode == PromptMode.SHELL:
                    text = shlex.quote(text)
                parts.append(text)

        # 2. Insert images via cache.
        if result.images:
            if "image_in" not in self._model_capabilities:
                console.print(
                    f"[{_get_tui_tokens().warning}]Image input is not supported "
                    "by the selected LLM model[/]"
                )
            else:
                for image in result.images:
                    token = self._get_placeholder_manager().create_image_placeholder(image)
                    if token is None:
                        continue
                    logger.debug(
                        "Pasted image from clipboard placeholder: {token}, {image_size}",
                        token=token,
                        image_size=image.size,
                    )
                    parts.append(token)

        if parts:
            event.current_buffer.insert_text(" ".join(parts))
        event.app.invalidate()
        return bool(parts)

    def set_prefill_text(self, text: str) -> None:
        """Pre-fill the input buffer with the given text.

        Must be called after the prompt session is created but before the
        first prompt_async call.  The text will appear as editable default
        input in the next prompt.
        """
        self._prefill_text = text

    def stage_suggestion_prefill(self, prefill: str) -> None:
        """Remember a non-blocking Suggestion prefill until the user accepts it."""
        text = prefill.strip()
        self._staged_suggestion_prefill = text or None

    def accept_staged_suggestion_prefill(self) -> bool:
        """Insert a staged suggestion prefill into the prompt buffer."""
        text = getattr(self, "_staged_suggestion_prefill", None)
        if not text:
            return False
        self._staged_suggestion_prefill = None
        buffer = self._session.default_buffer
        if buffer.text and not buffer.text.endswith((" ", "\n")):
            buffer.insert_text(" ")
        buffer.insert_text(text)
        return True

    async def prompt_next(self) -> UserInput:
        return await self._prompt_once(append_history=None)

    @property
    def last_submission_was_running(self) -> bool:
        return getattr(self, "_last_submission_was_running", False)

    def has_pending_input(self) -> bool:
        return bool(self._session.default_buffer.text)

    def had_recent_input_activity(self, *, within_s: float) -> bool:
        if self._last_input_activity_time <= 0:
            return False
        return (time.monotonic() - self._last_input_activity_time) <= within_s

    def recent_input_activity_remaining(self, *, within_s: float) -> float:
        if self._last_input_activity_time <= 0:
            return 0.0
        elapsed = time.monotonic() - self._last_input_activity_time
        return max(0.0, within_s - elapsed)

    async def wait_for_input_activity(self) -> None:
        await self._input_activity_event.wait()
        self._input_activity_event.clear()

    def _prompt_reducer_state(self) -> PromptState:
        """Return the authoritative reducer state, bootstrapping legacy sessions once.

        A few integrations historically populated these attributes on partially
        constructed sessions. Bootstrap those sessions at this boundary without
        rebuilding reducer-owned state from mutable facade projections on every
        dispatch.
        """
        existing = getattr(self, "_prompt_state", None)
        if isinstance(existing, PromptState):
            return existing

        running = getattr(self, "_running_prompt_delegate", None)
        turn_starting = getattr(self, "_turn_starting", False)
        phase = (
            PromptPhase.RUNNING
            if running is not None
            else PromptPhase.TURN_STARTING
            if turn_starting
            else PromptPhase.IDLE
        )
        return PromptState(
            mode=getattr(self, "_mode", PromptMode.AGENT),
            phase=phase,
            running_delegate=running,
            modal_stack=tuple(
                ModalState(
                    delegate=delegate,
                    priority=delegate.modal_priority,
                    hides_input=delegate.running_prompt_hides_input_buffer(),
                )
                for delegate in getattr(self, "_modal_delegates", ())
            ),
            suspended_document=getattr(self, "_suspended_buffer_document", None),
            shortcut_help_open=getattr(self, "_shortcut_help_open", False),
            running_previous_mode=getattr(
                self,
                "_running_prompt_previous_mode",
                None,
            ),
        )

    def _apply_reducer_state(self, state: PromptState) -> None:
        self._prompt_state = state
        self._mode = state.mode
        self._turn_starting = state.phase is PromptPhase.TURN_STARTING
        self._running_prompt_delegate = state.running_delegate
        self._running_prompt_previous_mode = state.running_previous_mode
        self._modal_delegates = [modal.delegate for modal in state.modal_stack]
        self._suspended_buffer_document = state.suspended_document
        self._shortcut_help_open = state.shortcut_help_open

    def _apply_prompt_effect(self, effect: PromptEffect) -> None:
        session = getattr(self, "_session", None)
        buffer = getattr(session, "default_buffer", None)
        if isinstance(effect, SelectCompleter):
            if buffer is not None:
                attribute = (
                    "_shell_mode_completer"
                    if effect.mode is PromptMode.SHELL
                    else "_agent_mode_completer"
                )
                completer = getattr(self, attribute, None)
                if completer is not None:
                    buffer.completer = completer
        elif isinstance(effect, SetEraseWhenDone):
            app = getattr(session, "app", None)
            if app is not None:
                app.erase_when_done = effect.erase_when_done
        elif isinstance(effect, SuspendDocument):
            if buffer is not None and buffer.text:
                buffer.set_document(Document(), bypass_readonly=True)
        elif isinstance(effect, RestoreDocument) and buffer is not None and not buffer.text:
            buffer.set_document(effect.document, bypass_readonly=True)

    def _dispatch(self, event: PromptEvent, *, invalidate_noop: bool = False) -> None:
        result = transition(self._prompt_reducer_state(), event)
        self._apply_reducer_state(result.state)
        should_invalidate = invalidate_noop
        for effect in result.effects:
            if isinstance(effect, Invalidate):
                should_invalidate = True
            else:
                self._apply_prompt_effect(effect)
        if should_invalidate:
            self.invalidate()

    def toggle_mode(self) -> None:
        self._dispatch(ModeChanged(self._prompt_reducer_state().mode.toggle()))

    def toggle_shortcut_help(self) -> None:
        self._dispatch(ShortcutHelpToggled())

    def close_shortcut_help(self) -> None:
        self._dispatch(ShortcutHelpToggled(open=False))

    def mark_turn_starting(self) -> None:
        """Collapse the input card immediately, before the delegate attaches.

        The shell calls this the moment it dispatches an agent turn — right
        before it resumes the prompt read — so the first repaint after the turn
        starts never paints the input-card chrome (which would fossilize above
        the stream). Superseded by the delegate once :meth:`attach_running_prompt`
        runs; cleared there and on detach.
        """
        self._dispatch(TurnStarting())

    def clear_turn_starting(self) -> None:
        """Drop the pre-attach turn-starting hint without an attach/detach.

        Public counterpart to :meth:`mark_turn_starting`, for callers (the shell's
        ``run_soul_command`` ``finally``) that need to clear the hint on an error
        path that occurred before the running-prompt delegate ever attached —
        without reaching into the private ``_turn_starting`` attribute.
        """
        self._dispatch(TurnCleared(), invalidate_noop=not hasattr(self, "_session"))

    def attach_running_prompt(self, delegate: RunningPromptDelegate) -> None:
        self._dispatch(RunningDelegateAttached(delegate))

    def detach_running_prompt(self, delegate: RunningPromptDelegate) -> None:
        self._dispatch(RunningDelegateDetached(delegate))

    def attach_modal(self, delegate: RunningPromptDelegate) -> None:
        buffer = getattr(getattr(self, "_session", None), "default_buffer", None)
        document = buffer.document if buffer is not None else Document()
        self._dispatch(
            ModalAttached(
                delegate=delegate,
                priority=delegate.modal_priority,
                hides_input=delegate.running_prompt_hides_input_buffer(),
                document=document,
            )
        )

    def detach_modal(self, delegate: RunningPromptDelegate) -> None:
        buffer = getattr(getattr(self, "_session", None), "default_buffer", None)
        document = buffer.document if buffer is not None else Document()
        self._dispatch(ModalDetached(delegate, document))

    def running_prompt_accepts_submission(self) -> bool:
        delegate = self._active_prompt_delegate()
        if delegate is None:
            return False
        return delegate.running_prompt_accepts_submission()

    def _on_workspace_snapshot_published(self) -> None:
        """Re-run file completion when a fresh workspace snapshot lands mid-menu."""
        app = self._session.app
        if not app.is_running:
            return
        buffer = self._session.default_buffer
        if not HostFileMentionCompleter.should_complete(buffer.document):
            return
        buffer.start_completion(select_first=False)
        app.invalidate()

    async def _prompt_once(self, *, append_history: bool | None) -> UserInput:
        workspace_index = getattr(self, "_workspace_index", None)
        if workspace_index is not None:
            try:
                workspace_root: HostPath | None = HostPath.cwd()
            except OSError:
                # CWD was removed mid-session (e.g. an external drive was
                # unplugged). Keep the last known root instead of crashing the
                # prompt turn; the statusline render raises CwdLostError on the
                # same turn to exit gracefully.
                workspace_root = None
            if workspace_root is not None and workspace_root != getattr(
                self, "_workspace_root", None
            ):
                self._workspace_root = workspace_root
                workspace_index.set_root(workspace_root)
            workspace_index.request_refresh("")
        placeholder = None
        if (delegate := self._active_prompt_delegate()) is not None:
            placeholder = delegate.running_prompt_placeholder()
        # Consume one-shot prefill text if set
        default = getattr(self, "_prefill_text", None) or ""
        self._prefill_text = None
        self._staged_suggestion_prefill = None
        with patch_stdout(raw=True):
            command = str(
                await self._session.prompt_async(
                    placeholder=placeholder,
                    default=default,
                    set_exception_handler=False,
                )
            ).strip()
            command = command.replace("\x00", "")  # just in case null bytes are somehow inserted
            # Sanitize UTF-16 surrogates that may come from Windows clipboard
            command = sanitize_surrogates(command)
        was_running = self.running_prompt_accepts_submission()
        self._last_submission_was_running = was_running
        if append_history is None:
            append_history = not was_running
        if append_history:
            self._append_history_entry(command)
        self._tip_rotation_index += 1
        return self._build_user_input(command)

    def _build_user_input(self, command: str) -> UserInput:
        resolved = self._get_placeholder_manager().resolve_command(command)
        mode = self._mode
        display_command = resolved.display_command
        resolved_command = resolved.resolved_text
        content: list[ContentPart] = resolved.content

        if (
            mode == PromptMode.AGENT
            and self._active_prompt_delegate() is None
            and display_command.startswith("!")
            and display_command[1:].strip()
        ):
            mode = PromptMode.SHELL
            display_command = display_command[1:].lstrip()
            if resolved_command.startswith("!"):
                resolved_command = resolved_command[1:].lstrip()
            content = [cast(ContentPart, TextPart(text=resolved_command))]

        return UserInput(
            mode=mode,
            command=display_command,
            resolved_command=resolved_command,
            content=content,
        )

    def _append_history_entry(self, text: str) -> None:
        if not getattr(self, "_history_enabled", True):
            return
        safe_history_text = self._get_placeholder_manager().serialize_for_history(text).strip()
        safe_history_text = _redact_history_secrets(safe_history_text)
        entry = _HistoryEntry(content=safe_history_text)
        if not entry.content:
            return

        # skip if same as last entry
        if entry.content == self._last_history_content:
            return

        history_store = getattr(self, "_history_store", None)
        if not isinstance(history_store, PromptHistoryStore):
            history_store = PromptHistoryStore(self._history_file)
            self._history_store = history_store
        try:
            if history_store.append(entry.content):
                self._last_history_content = entry.content
        except PromptHistoryError as exc:
            logger.warning(
                "Failed to append user history entry: {file} ({error})",
                file=self._history_file,
                error=exc,
            )

    def _append_update_notice(
        self,
        fragments: list[tuple[str, str]],
        columns: int,
        footer: FooterViewModel | None = None,
    ) -> None:
        """Append a persistent yellow 'update available' line as the *last* footer
        row — below the status/clock line — so it sits fully clear of the prompt
        input box instead of glued to it. Call this last, after the status lines
        are assembled: it prepends its own newline (the prior footer line carries
        none) and adds no trailing newline, so it never leaves a blank row at the
        bottom. No-op when no update is pending; style-agnostic across both
        toolbar layouts."""
        if footer is not None:
            text = footer.update_notice
        elif getattr(self, "_current_prompt_frame", None) is not None and hasattr(
            self, "_prompt_frame_update_notice"
        ):
            # Reuse the notice sampled for this frame; never re-sample mid-frame.
            text = self._prompt_frame_update_notice
        else:
            provider = cast(
                Callable[[], str | None] | None,
                getattr(self, "_update_notice_provider", None),
            )
            text = provider() if callable(provider) else None
        if not text:
            return
        line = truncate_footer_right(
            text,
            max(0, columns - 1),
            ascii_only=footer.status.ascii_only if footer is not None else False,
        )
        if not line:
            return
        tokens = _get_tui_tokens()
        style = f"fg:{tokens.warning or 'ansiyellow'} bold"
        fragments.extend([("", "\n"), (style, line)])

    def _prompt_git_snapshot(self, root: HostPath) -> GitSnapshot:
        index = getattr(self, "_git_status_index", None)
        if isinstance(index, GitStatusIndex):
            return index.snapshot(root)
        branch = _get_git_branch()
        dirty, ahead, behind = _get_git_status() if branch else (False, 0, 0)
        diffstat = _get_git_diffstat()
        added, removed = diffstat if diffstat is not None else (0, 0)
        return GitSnapshot(
            root=root.canonical(),
            branch=branch,
            dirty=dirty,
            ahead=ahead,
            behind=behind,
            added=added,
            removed=removed,
        )

    def _prompt_toast(self, position: Literal["left", "right"]) -> ToastSnapshot | None:
        manager = getattr(self, "_toast_manager", None)
        if isinstance(manager, ToastManager):
            return manager.current(position)
        return _current_toast(position)

    def _fit_toolbar_to_terminal(self, fragments: FormattedText, columns: int) -> FormattedText:
        app = get_app_or_none()
        size = app.output.get_size() if app is not None else None
        rows = getattr(size, "rows", None)
        if not isinstance(rows, int) or rows <= 0:
            return fragments
        max_rows = min(rows, max(0, getattr(self, "_prompt_footer_row_budget", rows)))
        return _fit_prompt_scene_to_rows(
            fragments,
            columns,
            max_rows,
            show_clip_hint=False,
        )

    def _render_bottom_toolbar(self) -> FormattedText:
        if (
            hasattr(self, "_session")
            and self._should_show_slash_completion_menu()
            and self._session.default_buffer.complete_state is not None
        ):
            return FormattedText([])
        app = get_app_or_none()
        assert app is not None
        columns = app.output.get_size().columns
        try:
            footer = self._footer_view_model_for_render(columns)
        except CwdLostError as exc:
            app.exit(exception=exc)
            return FormattedText([])

        # Pythinker footer dispatch. Mirrors components/footer.ts layout while
        # reusing the existing data sources so we never lose information vs
        # the legacy toolbar.
        from pythinker_code.ui.tui_config import is_card_style

        if is_card_style():
            return self._render_card_bottom_toolbar(footer)

        return self._render_legacy_bottom_toolbar(footer)

    def _render_legacy_bottom_toolbar(self, footer: FooterViewModel) -> FormattedText:
        """Render legacy footer chrome over one immutable footer snapshot."""
        from pythinker_code.ui.shell.statusline import format_git_badge

        columns = footer.status.columns

        fragments: list[tuple[str, str]] = []
        tc = get_toolbar_colors()

        fragments.append((self._prompt_separator_style(tc.separator), _prompt_rule(columns)))
        fragments.append(("", "\n"))

        remaining = columns

        # Time-based tip rotation (every 30 s, independent of user submissions)
        now = time.monotonic()
        if now - self._last_tip_rotate_time >= _TIP_ROTATE_INTERVAL:
            self._tip_rotation_index += 1
            self._last_tip_rotate_time = now

        # Status flags: yolo / auto / plan
        ctx = footer.status
        if ctx.flags.yolo:
            fragments.extend([(tc.yolo_label, "yolo"), ("", "  ")])
            remaining -= 6  # "yolo" = 4, "  " = 2
        if ctx.flags.auto:
            fragments.extend([(tc.auto_label, "auto"), ("", "  ")])
            remaining -= 6  # "auto" = 4, "  " = 2
        if ctx.flags.plan:
            fragments.extend([(tc.plan_label, "plan"), ("", "  ")])
            remaining -= 6

        # Mode indicator (agent / shell) + model name. Thinking effort is shown
        # on the input's top-border label, not in the footer. Degrade gracefully
        # on narrow terminals: full "agent (model-name)" → bare "agent".
        tokens = _get_tui_tokens()
        mode_style = f"fg:{tokens.text or tokens.activity_label}"
        secondary_style = f"fg:{tokens.muted}"
        mode = str(self._mode)
        if self._mode == PromptMode.AGENT and self._model_name:
            mode_full = f"{mode} ({self._model_name})"
            if _display_width(mode_full) <= remaining - 2:
                mode = mode_full
            # else: keep bare mode name — model_name is dropped
        fragments.extend([(mode_style, mode), ("", "  ")])
        remaining -= _display_width(mode) + 2

        # CWD (truncated from left) + git branch with status badge
        # Degrade gracefully on narrow terminals: full → cwd-only → truncated cwd → skip
        cwd = ctx.cwd or ""
        git_info = ctx.git
        if git_info is not None:
            badge = format_git_badge(git_info, ascii_only=ctx.ascii_only)
            cwd_text = f"{cwd}  {badge}"
        else:
            cwd_text = cwd
        cwd_w = _display_width(cwd_text)
        if cwd_w > remaining - 2:
            cwd_text = cwd  # drop badge
            cwd_w = _display_width(cwd_text)
        if cwd_w > remaining - 2:
            cwd_text = truncate_footer_right(
                cwd,
                max(0, remaining - 2),
                ascii_only=ctx.ascii_only,
            )
            cwd_w = _display_width(cwd_text)
        if cwd_text and remaining >= cwd_w + 2:
            fragments.extend([(tc.cwd, cwd_text), ("", "  ")])
            remaining -= cwd_w + 2

        # Active background task counts (bash + agent, each rendered as its own
        # badge). Order matters: bash renders first; if there isn't room for the
        # agent badge too, drop agent and keep bash.
        for kind_label, kind_count in (
            ("bash", ctx.background_bash),
            ("agent", ctx.background_agent),
        ):
            if kind_count <= 0:
                continue
            bg_text = f"{'*' if ctx.ascii_only else '◇'} {kind_label}: {kind_count}"
            bg_width = _display_width(bg_text)
            if remaining < bg_width + 2:
                break
            fragments.extend([(tc.bg_tasks, bg_text), ("", "  ")])
            remaining -= bg_width + 2

        # Tips fill remaining space on line 1
        tip_text = self._get_two_rotating_tips()
        if tip_text and _display_width(tip_text) > remaining:
            tip_text = self._get_one_rotating_tip()
        if tip_text and _display_width(tip_text) <= remaining:
            _append_footer_hint_fragments(
                fragments,
                tip_text,
                tip_style=tc.tip,
                key_style=tc.tip_key,
            )

        # ── line 2: toast (left) + context (right) — always rendered ──────
        fragments.append(("", "\n"))

        usable = max(0, columns - 1)
        right_text = self._render_right_span(footer)
        right_width = _display_width(right_text)
        if right_width > usable:
            right_text = truncate_footer_left(
                right_text,
                usable,
                ascii_only=ctx.ascii_only,
            )
            right_width = _display_width(right_text)

        left_content = select_footer_content(footer)
        if left_content is not None:
            max_left = max(0, usable - right_width - 1)
            if max_left > 0:
                left_text = left_content.text
                if _display_width(left_text) > max_left:
                    left_text = truncate_footer_right(
                        left_text,
                        max_left,
                        ascii_only=ctx.ascii_only,
                    )
                left_width = _display_width(left_text)
                left_style = {
                    "background": tc.bg_tasks,
                    "toast": left_content.style or secondary_style,
                }.get(left_content.kind, tc.tip)
                fragments.append((left_style, left_text))
            else:
                left_width = 0
        else:
            left_width = 0

        fragments.append(("", " " * max(0, usable - left_width - right_width)))
        fragments.append((secondary_style, right_text))

        self._append_update_notice(fragments, columns, footer)
        return self._fit_toolbar_to_terminal(FormattedText(fragments), columns)

    def _build_statusline_context(
        self,
        columns: int,
        *,
        status: StatusSnapshot | None = None,
        background_counts: BgTaskCounts | None = None,
    ) -> StatusLineContext:
        from pythinker_code.ui.shell.statusline import (
            GitInfo,
            RateSampler,
            StatusFlags,
            StatusLineContext,
        )
        from pythinker_code.ui.terminal_capabilities import ascii_glyphs_enabled

        cfg = getattr(self, "_statusline_cfg", None) or StatusLineConfig()
        status = status if status is not None else self._status_provider()
        background_counts = background_counts or self._background_task_counts()
        now = time.monotonic()

        self._statusline_frame = getattr(self, "_statusline_frame", 0) + 1
        working = background_counts.bash > 0 or background_counts.agent > 0

        # Samplers may be missing when a session is constructed without __init__
        # (test helpers do this); fall back to fresh ones so rendering is robust.
        rate_in_sampler = getattr(self, "_rate_in_sampler", None) or RateSampler()
        self._rate_in_sampler = rate_in_sampler
        rate_out_sampler = getattr(self, "_rate_out_sampler", None) or RateSampler()
        self._rate_out_sampler = rate_out_sampler

        rate_in: int | None = None
        rate_out: int | None = None
        if working:
            rate_in = rate_in_sampler.update(now, status.total_input_tokens)
            rate_out = rate_out_sampler.update(now, status.total_output_tokens)
        else:
            rate_in_sampler.reset()
            rate_out_sampler.reset()

        ascii_only = ascii_glyphs_enabled()
        try:
            git_root = HostPath.cwd()
            cwd_text = truncate_footer_left(
                _shorten_cwd(str(git_root)),
                _MAX_CWD_COLS,
                ascii_only=ascii_only,
            )
        except OSError as exc:
            raise CwdLostError() from exc

        git_info: GitInfo | None = None
        git_snapshot = self._prompt_git_snapshot(git_root)
        branch = git_snapshot.branch
        if branch:
            git_info = GitInfo(
                branch=truncate_footer_right(
                    branch,
                    _MAX_BRANCH_COLS,
                    ascii_only=ascii_only,
                ),
                dirty=git_snapshot.dirty,
                ahead=git_snapshot.ahead,
                behind=git_snapshot.behind,
            )

        diff = git_snapshot.diffstat
        diff_added, diff_removed = diff if diff is not None else (None, None)

        thinking_effort = getattr(self, "_thinking_effort", None)
        effort = thinking_effort if thinking_effort in ("high", "medium", "low") else None

        started = getattr(self, "_statusline_started_at", None)
        elapsed_s = (now - started) if started is not None else 0.0

        return StatusLineContext(
            columns=columns,
            working=working,
            frame=self._statusline_frame,
            model_name=getattr(self, "_model_name", None),
            provider_label=None,
            effort=effort,
            rate_in=rate_in,
            rate_out=rate_out,
            session_cost_usd=getattr(status, "session_cost_usd", 0.0),
            cost_budget_usd=cfg.cost_budget,
            context_tokens=status.context_tokens,
            max_context_tokens=status.max_context_tokens,
            elapsed_s=elapsed_s,
            clock=datetime.now().strftime("%H:%M"),
            cwd=cwd_text,
            git=git_info,
            diff_added=diff_added,
            diff_removed=diff_removed,
            flags=StatusFlags(
                yolo=status.yolo_enabled,
                auto=status.auto_enabled,
                plan=status.plan_mode,
            ),
            limits=None,
            ascii_only=ascii_only,
            style=cfg.style if cfg.enabled else "plain",
            bar_width=cfg.bar_width,
            context_usage=status.context_usage,
            background_bash=background_counts.bash,
            background_agent=background_counts.agent,
        )

    def _build_footer_view_model(self, columns: int) -> FooterViewModel:
        """Sample every dynamic footer provider exactly once for one frame."""
        from pythinker_code.extensions import footer_statuses
        from pythinker_code.ui.shell.statusline import StatusLineCommandRunner

        cfg = getattr(self, "_statusline_cfg", None) or StatusLineConfig()
        status = self._status_provider()
        background_counts = self._background_task_counts()
        runner = getattr(self, "_statusline_runner", None)
        command_line = ""
        if (
            cfg.enabled
            and "command" in cfg.segments
            and isinstance(runner, StatusLineCommandRunner)
        ):
            command_line = runner.current_line

        left_toast = self._prompt_toast("left")
        toast_snapshot = left_toast if left_toast is not None else self._prompt_toast("right")
        update_provider = cast(
            Callable[[], str | None] | None,
            getattr(self, "_update_notice_provider", None),
        )
        return FooterViewModel(
            status=self._build_statusline_context(
                columns,
                status=status,
                background_counts=background_counts,
            ),
            command_line=command_line,
            extension_statuses=tuple(sorted(footer_statuses().items())),
            background_summary=background_task_summary(
                bash=background_counts.bash,
                agent=background_counts.agent,
            ),
            toast=toast_snapshot,
            update_notice=update_provider() if callable(update_provider) else None,
        )

    def _update_notice_for_render(self) -> str | None:
        """Read the update notice for message rendering without building a footer.

        Message rendering must stay independent of the footer providers so
        partially constructed sessions (tests, shell mode) can render; prefer
        the per-frame footer snapshot when one exists.
        """
        footer = getattr(self, "_current_footer_view_model", None)
        if footer is not None:
            return footer.update_notice
        provider = cast(
            Callable[[], str | None] | None,
            getattr(self, "_update_notice_provider", None),
        )
        return provider() if callable(provider) else None

    def _footer_view_model_for_render(self, columns: int) -> FooterViewModel:
        current = getattr(self, "_current_footer_view_model", None)
        if current is not None and current.status.columns == columns:
            return current
        footer = self._build_footer_view_model(columns)
        if getattr(self, "_current_prompt_frame", None) is not None:
            self._current_footer_view_model = footer
        return footer

    def _render_card_bottom_toolbar(self, footer: FooterViewModel) -> FormattedText:
        """Pythinker two-line footer (statusline v2).

        Line 1 + line-2 right are assembled from the segment registry; the
        line-2 left side keeps the command/extension/background/toast
        precedence from the legacy footer.
        """
        from pythinker_code.config import StatusLineConfig
        from pythinker_code.ui.shell.statusline import (
            DEFAULT_STATUSLINE_SEGMENTS,
            assemble_footer,
        )

        columns = footer.status.columns
        cfg = getattr(self, "_statusline_cfg", None) or StatusLineConfig()
        tc = get_toolbar_colors()
        tokens = _get_tui_tokens()
        secondary_style = f"fg:{tokens.muted}"

        fragments: list[tuple[str, str]] = []
        fragments.append((self._prompt_separator_style(tc.separator), _prompt_rule(columns)))
        fragments.append(("", "\n"))

        segments = list(cfg.segments) if cfg.enabled else list(DEFAULT_STATUSLINE_SEGMENTS)
        line1, line2_right = assemble_footer(footer.status, segments)
        fragments.extend(line1)
        fragments.append(("", "\n"))

        # Reserve the last column (like _prompt_rule) so writing the final cell
        # can't wrap the line on terminals such as Windows conhost / PowerShell.
        usable = max(0, columns - 1)

        right_text = "".join(t for _, t in line2_right)
        right_width = _display_width(right_text)
        if right_width > usable:
            right_text = truncate_footer_left(
                right_text,
                usable,
                ascii_only=footer.status.ascii_only,
            )
            line2_right = [(secondary_style, right_text)]
            right_width = _display_width(right_text)

        max_left_width = max(0, usable - right_width - 1)
        left_content = select_footer_content(footer)
        if left_content is not None:
            left_text = truncate_footer_right(
                left_content.text,
                max_left_width,
                ascii_only=footer.status.ascii_only,
            )
            left_style = {
                "background": tc.bg_tasks,
                "toast": left_content.style or secondary_style,
            }.get(left_content.kind, tc.tip)
            fragments.append((left_style, left_text))
            left_width = _display_width(left_text)
        else:
            left_width = 0

        fragments.append(("", " " * max(0, usable - left_width - right_width)))
        fragments.extend(line2_right)
        self._append_update_notice(fragments, columns, footer)
        return self._fit_toolbar_to_terminal(FormattedText(fragments), columns)

    def _get_two_rotating_tips(self) -> str | None:
        """Return a string with exactly 2 tips from the rotation, or fewer if not enough."""
        n = len(self._tips)
        if n == 0:
            return None
        if n == 1:
            return self._tips[0]
        offset = self._tip_rotation_index % n
        tip1 = self._tips[offset]
        tip2 = self._tips[(offset + 1) % n]
        return f"{tip1}{_TIP_SEPARATOR}{tip2}"

    def _get_one_rotating_tip(self) -> str | None:
        """Return the single leading tip for the current rotation."""
        if not self._tips:
            return None
        return self._tips[self._tip_rotation_index % len(self._tips)]

    def _render_right_span(self, footer: FooterViewModel) -> str:
        if footer.toast is None or footer.toast.position != "right":
            status = footer.status
            return format_context_status(
                status.context_usage,
                status.context_tokens,
                status.max_context_tokens,
            )
        return footer.toast.message


# Compatibility surface kept for tests that still import the legacy footer
# helpers (tests/ui_and_conv/test_prompt_tips.py); rendering now goes through
# pythinker_code.ui.shell.prompting.footer.
_LEGACY_FOOTER_HELPERS = (
    _background_task_summary,
    _format_git_badge,
    _truncate_left,
)
