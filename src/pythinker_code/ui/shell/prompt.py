from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import md5
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, override, runtime_checkable

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard.pyperclip import PyperclipClipboard
from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    FuzzyCompleter,
    WordCompleter,
    merge_completers,
)
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
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.utils import get_cwidth
from pydantic import BaseModel, ValidationError
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
from pythinker_code.ui.shell.spacing import ensure_prompt_newline
from pythinker_code.ui.shell.spinner_words import spinner_message
from pythinker_code.ui.shell.sync_output import install_synchronized_output
from pythinker_code.ui.terminal_capabilities import synchronized_output_enabled
from pythinker_code.ui.theme import get_prompt_style, get_toolbar_colors, thinking_dot_style
from pythinker_code.ui.theme import get_tui_tokens as _get_tui_tokens
from pythinker_code.ui.tui_config import is_card_style
from pythinker_code.utils.clipboard import (
    grab_media_from_clipboard,
    is_clipboard_available,
    is_media_clipboard_available,
)
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


def _is_prompt_toolkit_empty_exception_context(context: dict[str, Any]) -> bool:
    """Return true for prompt_toolkit's unhelpful ``Exception None`` report.

    prompt_toolkit prints ``Unhandled exception in event loop`` and blocks on
    ``Press ENTER to continue`` even when asyncio only supplied a diagnostic
    context with no exception object. That message has no traceback or useful
    recovery action for users, so Pythinker logs it instead of surfacing a modal
    terminal pause.
    """
    if context.get("exception") is not None:
        return False
    message = str(context.get("message") or "")
    if not message:
        return True
    return message.startswith(("Task was destroyed but it is pending", "Future exception"))


# Python 3.14 can report prompt_toolkit's already-cancelled key-timeout coroutine as an
# unraisable KeyError("__import__") during interpreter/module teardown. The RuntimeWarning filters
# above catch the normal warning path; this hook catches the shutdown-only unraisable path while
# delegating every other unraisable exception to Python's original hook.
if sys.unraisablehook is not _pythinker_unraisable_hook:
    sys.unraisablehook = _pythinker_unraisable_hook


class CwdLostError(OSError):
    """Raised when the working directory no longer exists (e.g. external drive unplugged)."""


def _command_name_set(commands: Sequence[SlashCommand[Any]]) -> frozenset[str]:
    """Lowercased names and aliases for slash highlighting and completion."""
    names: set[str] = set()
    for cmd in commands:
        names.add(cmd.name.lower())
        names.update(alias.lower() for alias in cmd.aliases)
    return frozenset(names)


def _is_known_slash_command_prefix(name: str, known: frozenset[str]) -> bool:
    """True when ``name`` is a registered command or a prefix of one (e.g. ``/skill:py``)."""
    lower = name.lower()
    if lower in known:
        return True
    return any(command_name.startswith(lower) for command_name in known)


def _fuzzy_subsequence(needle: str, haystack: str) -> bool:
    """True when ``needle`` is an (ordered, gap-tolerant) subsequence of ``haystack``.

    Mirrors the selector/settings filters (``selector.py``, ``settings_list.py``);
    used as the lowest-priority slash match so a misspelled distinctive word like
    ``gurd`` still surfaces ``/skill:pythinker-guard`` when no prefix matches.
    """
    pos = 0
    for ch in needle:
        found = haystack.find(ch, pos)
        if found < 0:
            return False
        pos = found + 1
    return True


def _slash_first_arg_context(
    document: Document,
    known_names: frozenset[str],
    arg_suggestions: dict[str, tuple[str, ...]],
) -> tuple[str, str] | None:
    """When the cursor trails the first argument of a known slash command, return (cmd, partial)."""
    if document.text_after_cursor.strip():
        return None
    line = document.current_line_before_cursor
    if not line.startswith("/"):
        return None
    match = re.match(r"^/([A-Za-z0-9][A-Za-z0-9_:.-]*)(?:\s+(\S*))?$", line)
    if match is None:
        return None
    command = match.group(1).lower()
    if command not in known_names or command not in arg_suggestions:
        return None
    return command, match.group(2) or ""


def _slash_command_token_before_cursor(document: Document) -> str | None:
    """Return the active slash-command token, or ``None`` when completion should stay hidden."""
    text = document.text_before_cursor

    if document.text_after_cursor.strip():
        return None

    last_space = text.rfind(" ")
    token = text[last_space + 1 :]
    prefix = text[: last_space + 1] if last_space != -1 else ""

    if prefix.strip() or not token.startswith("/"):
        return None
    return token


def _slash_suggest_token_before_cursor(document: Document) -> str | None:
    """Return the active slash token for inline ghost-text suggestion.

    Unlike :func:`_slash_command_token_before_cursor` (which gates the dropdown
    menu and only fires when the slash command starts the line), this accepts a
    ``/name`` token anywhere on the current line, so mid-sentence references such
    as ``use /desi`` still ghost-complete. Ghost text only renders at the end of
    the buffer, so we still require nothing typed after the cursor.
    """
    if document.text_after_cursor.strip():
        return None
    line = document.current_line_before_cursor
    last_space = line.rfind(" ")
    token = line[last_space + 1 :]
    if not token.startswith("/"):
        return None
    return token


def _discard_slash_command(buffer: Buffer) -> bool:
    """Cancel slash completion and remove the in-progress root slash command."""
    document = buffer.document
    token = _slash_command_token_before_cursor(document)
    if token is None:
        return False

    buffer.cancel_completion()
    prefix = document.text_before_cursor[: -len(token)]
    new_text = prefix + document.text_after_cursor
    if not new_text.strip():
        buffer.set_document(Document(), bypass_readonly=True)
        return True

    buffer.set_document(
        Document(new_text, cursor_position=min(len(prefix), len(new_text))),
        bypass_readonly=True,
    )
    return True


# A "/name" token that starts the input or follows whitespace. The name charset
# matches registered command names and aliases (including "skill:x" / "flow:x").
_SLASH_TOKEN_RE = re.compile(r"(?<!\S)/([A-Za-z0-9][A-Za-z0-9_:.-]*)")
# An "@path" mention: "@" followed by a non-space fragment. The word boundary
# before "@" is validated separately to mirror LocalFileMentionCompleter.
_MENTION_TOKEN_RE = re.compile(r"@[^\s@]+")
# Characters that, immediately before "@", disqualify it as a mention boundary
# (so emails like "foo@bar" don't trigger). Shared by the lexer and completer.
_MENTION_TRIGGER_GUARDS = frozenset((".", "-", "_", "`", "'", '"', ":", "@", "#", "~"))


class InputHighlightLexer(Lexer):
    """Highlight recognized input tokens in the prompt buffer.

    Three token kinds are styled, composing on the same line:

    - **Slash commands** (``class:slash-command``) -- registered command names,
      aliases, and in-progress prefixes (``/cle``, ``/skill:py``), anywhere on
      the line.
    - **Slash arguments** (``class:slash-arg``) -- the first token after a
      command that declares fixed subcommands (e.g. ``current`` in
      ``/theme current``).
    - **``@file`` mentions** (``class:file-mention``) -- agent mode only, styled
      syntactically at a word boundary. The lexer runs on every keystroke and
      cannot touch the filesystem, so mentions are not resolution-checked.
    - **Leading ``!`` bash prefix** (``class:bash-prefix``) -- agent mode only,
      the first character when the input is a one-shot shell command (mirrors
      ``_build_user_input``).
    """

    def __init__(
        self,
        known_names: Callable[[], frozenset[str]],
        *,
        agent_mode: Callable[[], bool],
        arg_suggestions: Callable[[], dict[str, tuple[str, ...]]] | None = None,
    ) -> None:
        self._known_names = known_names
        self._agent_mode = agent_mode
        self._arg_suggestions = arg_suggestions or _no_arg_suggestions

    @override
    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        known = self._known_names()
        arg_suggestions = self._arg_suggestions()
        agent_mode = self._agent_mode()
        lines = document.lines

        def spans(line: str, lineno: int) -> list[tuple[int, int, str]]:
            out: list[tuple[int, int, str]] = []
            # Leading "!" bash prefix: first line, agent mode, command after it.
            if agent_mode and lineno == 0 and line.startswith("!") and line[1:].strip():
                out.append((0, 1, "class:bash-prefix"))
            # Slash commands anywhere on the line (registered names and prefixes).
            for match in _SLASH_TOKEN_RE.finditer(line):
                name = match.group(1)
                if not _is_known_slash_command_prefix(name, known):
                    continue
                # Path-like tokens ("/clear/subdir") are not commands.
                if match.end() < len(line) and line[match.end()] == "/":
                    continue
                out.append((match.start(), match.end(), "class:slash-command"))
            # First argument after a line-start slash command with known subcommands.
            if line.startswith("/"):
                arg_match = re.match(r"^/([A-Za-z0-9][A-Za-z0-9_:.-]*)(?:\s+(\S+))", line)
                if arg_match is not None:
                    command = arg_match.group(1).lower()
                    partial = arg_match.group(2)
                    options = arg_suggestions.get(command)
                    if options and any(option.startswith(partial.lower()) for option in options):
                        arg_start = arg_match.start(2)
                        out.append((arg_start, arg_match.end(2), "class:slash-arg"))
            # "@path" file mentions at a word boundary (agent mode only).
            if agent_mode:
                for match in _MENTION_TOKEN_RE.finditer(line):
                    start = match.start()
                    if start > 0:
                        prev = line[start - 1]
                        if prev.isalnum() or prev in _MENTION_TRIGGER_GUARDS:
                            continue
                    out.append((start, match.end(), "class:file-mention"))
            out.sort(key=lambda span: span[0])
            return out

        def get_line(lineno: int) -> StyleAndTextTuples:
            try:
                line = lines[lineno]
            except IndexError:
                return []
            fragments: StyleAndTextTuples = []
            pos = 0
            for start, end, style in spans(line, lineno):
                if start < pos:
                    continue  # defensive: drop overlapping spans
                if start > pos:
                    fragments.append(("", line[pos:start]))
                fragments.append((style, line[start:end]))
                pos = end
            if pos < len(line):
                fragments.append(("", line[pos:]))
            return fragments

        return get_line


def _no_exact_suggestions() -> dict[str, str]:
    return {}


def _no_arg_suggestions() -> dict[str, tuple[str, ...]]:
    return {}


class SlashCommandAutoSuggest(AutoSuggest):
    """Inline ghost-text completion for a partially typed slash command.

    While the user types a ``/name`` token -- at the start of the line *or*
    mid-sentence (e.g. ``use /desi``) -- the remainder of the best (alphabetically
    first) matching command renders as dim ghost text after the cursor; Tab
    accepts it word-for-word. After a command that declares fixed subcommands
    (e.g. ``/theme cur``), the first argument is ghost-completed too. The dropdown
    menu stays line-start-only, so mid-sentence typing never pops a completion
    list. Rendering and the standard accept bindings (right-arrow / ctrl-e) come
    from prompt_toolkit's auto-suggest plumbing; the Tab binding is added in
    CustomPromptSession.
    """

    def __init__(
        self,
        known_names: Callable[[], frozenset[str]],
        *,
        exact_suggestions: Callable[[], dict[str, str]] | None = None,
        arg_suggestions: Callable[[], dict[str, tuple[str, ...]]] | None = None,
    ) -> None:
        self._known_names = known_names
        self._exact_suggestions = exact_suggestions or _no_exact_suggestions
        self._arg_suggestions = arg_suggestions or _no_arg_suggestions

    @override
    def get_suggestion(self, buffer: Buffer, document: Document) -> Suggestion | None:
        arg_ctx = _slash_first_arg_context(document, self._known_names(), self._arg_suggestions())
        if arg_ctx is not None:
            command, partial = arg_ctx
            options = self._arg_suggestions()[command]
            partial_lower = partial.lower()
            if not partial:
                return Suggestion(options[0])
            matches = [
                option
                for option in options
                if option.startswith(partial_lower) and len(option) > len(partial)
            ]
            if matches:
                return Suggestion(matches[0][len(partial) :])
            return None

        token = _slash_suggest_token_before_cursor(document)
        if token is None or len(token) < 2:
            return None
        typed = token[1:]
        typed_lower = typed.lower()
        exact = self._exact_suggestions().get(typed_lower)
        if exact is not None and typed_lower in self._known_names():
            return Suggestion(exact)
        matches = sorted(
            name
            for name in self._known_names()
            if name.lower().startswith(typed_lower) and len(name) > len(typed)
        )
        if not matches:
            return None
        return Suggestion(matches[0][len(typed) :])


class SlashCommandCompleter(Completer):
    """
    A completer that:
    - Shows one line per slash command using the canonical "/name"
    - Matches exact names first, then name/alias prefixes, while inserting the canonical "/name"
    - Only activates when the current token starts with '/'
    """

    def __init__(
        self,
        available_commands: Sequence[SlashCommand[Any]],
        *,
        annotate_meta: bool = False,
        command_scope: str = "command",
        is_task_running: Callable[[], bool] | None = None,
        arg_suggestions: Callable[[], dict[str, tuple[str, ...]]] | None = None,
    ) -> None:
        super().__init__()
        self._available_commands = sorted(available_commands, key=lambda c: c.name)
        self._command_names = _command_name_set(available_commands)
        self._annotate_meta = annotate_meta
        self._command_scope = command_scope
        self._is_task_running = is_task_running
        self._arg_suggestions = arg_suggestions or _no_arg_suggestions

    def completion_active(self, document: Document) -> bool:
        """Return whether slash command or subcommand completion should be active."""
        if _slash_command_token_before_cursor(document) is not None:
            return True
        return (
            _slash_first_arg_context(document, self._command_names, self._arg_suggestions())
            is not None
        )

    @staticmethod
    def should_complete(document: Document) -> bool:
        """Return whether slash command completion should be active for the current buffer."""
        return _slash_command_token_before_cursor(document) is not None

    @override
    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        if not self.completion_active(document):
            return

        arg_ctx = _slash_first_arg_context(document, self._command_names, self._arg_suggestions())
        if arg_ctx is not None:
            _, partial = arg_ctx
            partial_lower = partial.lower()
            for option in self._arg_suggestions()[arg_ctx[0]]:
                if partial and not option.startswith(partial_lower):
                    continue
                yield Completion(
                    text=option[len(partial) :] if partial else option,
                    start_position=-len(partial),
                    display=option,
                )
            return

        token = _slash_command_token_before_cursor(document)
        if token is None:
            return

        typed = token[1:]
        typed_lower = typed.lower()
        seen: set[str] = set()

        def emit(cmd: SlashCommand[Any], label: str | None = None) -> Iterable[Completion]:
            if cmd.name in seen:
                return
            seen.add(cmd.name)
            shown = label or cmd.name
            yield Completion(
                text=f"/{shown}",
                start_position=-len(token),
                display=f"/{shown}",
                display_meta=self._display_meta(cmd),
            )

        if not typed:
            for cmd in self._available_commands:
                yield from emit(cmd)
            return

        def match_tier(cmd: SlashCommand[Any]) -> tuple[int, str] | None:
            """Return ``(tier, label)`` or ``None``. Lower tier = stronger match.
            Name matches rank above alias matches so typing toward a command name
            (e.g. ``/report`` → ``/reports``) wins over a command that only matches
            via an exact alias. For alias-only matches the label is the matched
            alias, so the menu surfaces what the user typed toward (e.g. ``/res``
            → ``/resume``) rather than the differently-named command (``/sessions``)."""
            name_lower = cmd.name.lower()
            if name_lower == typed_lower:
                return (0, cmd.name)
            if name_lower.startswith(typed_lower):
                return (1, cmd.name)
            alias_prefix: str | None = None
            for alias in cmd.aliases:
                alias_lower = alias.lower()
                if alias_lower == typed_lower:
                    return (2, alias)
                if alias_prefix is None and alias_lower.startswith(typed_lower):
                    alias_prefix = alias
            if alias_prefix is not None:
                return (3, alias_prefix)
            # Namespaced commands ("skill:designer-skill", "flow:build-api") also
            # match on their bare segment after the prefix, so `/designer` or
            # `/build` surfaces them. The label stays the canonical name so the
            # accepted completion inserts "/skill:designer-skill", not the bare
            # term -- one execution path, no duplicate command.
            segment = name_lower.split(":", 1)[1] if ":" in name_lower else name_lower
            if ":" in name_lower:
                if segment == typed_lower:
                    return (4, cmd.name)
                if segment.startswith(typed_lower):
                    return (5, cmd.name)
            # Last resort: fuzzy subsequence on the bare segment, so the
            # distinctive word -- even misspelled (``gurd`` -> ``guard``) --
            # surfaces a command whose shared prefix (``pythinker-``) makes
            # plain prefix matching useless. Gated at 2+ chars to avoid a
            # single keystroke matching nearly everything. Label is canonical.
            if len(typed_lower) >= 2 and _fuzzy_subsequence(typed_lower, segment):
                return (6, cmd.name)
            return None

        # Rank by (match tier, command-name length, name): the closest, shortest
        # command name surfaces first within each tier.
        matched: list[tuple[int, int, str, str, SlashCommand[Any]]] = []
        for cmd in self._available_commands:
            result = match_tier(cmd)
            if result is not None:
                tier, label = result
                matched.append((tier, len(cmd.name), cmd.name, label, cmd))
        matched.sort(key=lambda item: (item[0], item[1], item[2]))
        if matched and matched[0][0] < 6:
            matched = [item for item in matched if item[0] < 6]

        for _, _, _, label, cmd in matched:
            yield from emit(cmd, label)

    def _disabled_during_task(self, cmd: SlashCommand[Any]) -> bool:
        """True when a running turn blocks this shell-level command."""
        if self._is_task_running is None or not self._is_task_running():
            return False
        from pythinker_code.ui.shell.slash import registry as shell_registry

        shell_cmd = shell_registry.find_command(cmd.name)
        return shell_cmd is not None and not shell_cmd.available_during_task

    def _display_meta(self, cmd: SlashCommand[Any]) -> str:
        if self._disabled_during_task(cmd):
            return "disabled while a task is in progress"
        if not self._annotate_meta:
            return cmd.description

        # Only surface a kind tag when it distinguishes the entry from a plain
        # command. Skills and flows are interleaved with commands in the agent
        # menu, so their tag carries information; the generic command/shell scope
        # is already obvious from the menu itself, so tagging every row is noise.
        if cmd.name.startswith("skill:"):
            kind: str | None = "skill"
        elif cmd.name.startswith("flow:"):
            kind = "flow"
        else:
            kind = None

        parts: list[str] = []
        if kind is not None:
            parts.append(f"[{kind}]")
        parts.append(cmd.description)
        if cmd.aliases:
            parts.append(f"aliases: {', '.join('/' + alias for alias in cmd.aliases)}")
        return "  ".join(part for part in parts if part)


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
    total = counts.bash + counts.agent
    if total <= 0:
        return None
    noun = "background task" if total == 1 else "background tasks"
    parts: list[str] = []
    if counts.bash:
        parts.append(f"{counts.bash} bash")
    if counts.agent:
        parts.append(f"{counts.agent} agent")
    detail = f" ({', '.join(parts)})" if parts else ""
    return f"{total} {noun} running{detail} · /task to view"


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
            [("class:dim", _truncate_right("… output clipped to fit terminal", columns))]
        )

    out: FormattedText = FormattedText()
    _extend_rows(out, rows[:content_rows])
    if out and not out[-1][1].endswith("\n"):
        out.append(("", "\n"))
    clip_hint = _truncate_right("… output clipped to fit terminal", columns)
    out.append(("class:dim", clip_hint))
    if tail_rows:
        out.append(("", "\n"))
        _extend_rows(out, tail_rows)
    return out


def _prompt_preamble_max_rows(terminal_rows: int | None) -> int:
    if terminal_rows is None or terminal_rows <= 0:
        return 20
    # Reserve rows for: spacer, separator, input row, toolbar separator, footer
    # rows, and one safety row. This prevents large tool cards from painting
    # underneath the prompt/footer on short terminals.
    return max(1, terminal_rows - 7)


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
        token = _slash_command_token_before_cursor(document)
        if token is None:
            return 0
        return len(token[1:])

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


class LocalFileMentionCompleter(Completer):
    """Offer fuzzy `@` path completion by indexing workspace files.

    File discovery and ignore rules are delegated to
    :mod:`pythinker_code.utils.file_filter` so that the web backend can reuse
    them.
    """

    _FRAGMENT_PATTERN = re.compile(r"[^\s@]+")
    _TRIGGER_GUARDS = _MENTION_TRIGGER_GUARDS

    def __init__(
        self,
        root: Path,
        *,
        refresh_interval: float = 2.0,
        limit: int = 1000,
    ) -> None:
        self._root = root
        self._refresh_interval = refresh_interval
        self._limit = limit
        self._cache_time: float = 0.0
        self._cached_paths: list[str] = []
        self._cache_scope: str | None = None
        self._top_cache_time: float = 0.0
        self._top_cached_paths: list[str] = []
        self._fragment_hint: str | None = None
        self._is_git: bool | None = None  # lazily detected
        self._git_index_mtime: float | None = None

        self._word_completer = WordCompleter(
            self._get_paths,
            WORD=False,
            pattern=self._FRAGMENT_PATTERN,
        )

        self._fuzzy = FuzzyCompleter(
            self._word_completer,
            WORD=False,
            pattern=r"^[^\s@]*",
        )

    def _get_paths(self) -> list[str]:
        fragment = self._fragment_hint or ""
        if "/" not in fragment and len(fragment) < 3:
            return self._get_top_level_paths()
        return self._get_deep_paths()

    def _get_top_level_paths(self) -> list[str]:
        from pythinker_code.utils.file_filter import is_ignored

        now = time.monotonic()
        if now - self._top_cache_time <= self._refresh_interval:
            return self._top_cached_paths

        entries: list[str] = []
        try:
            for entry in sorted(self._root.iterdir(), key=lambda p: p.name):
                name = entry.name
                if is_ignored(name):
                    continue
                entries.append(f"{name}/" if entry.is_dir() else name)
                if len(entries) >= self._limit:
                    break
        except OSError:
            return self._top_cached_paths

        self._top_cached_paths = entries
        self._top_cache_time = now
        return self._top_cached_paths

    def _get_deep_paths(self) -> list[str]:
        from pythinker_code.utils.file_filter import (
            detect_git,
            git_index_mtime,
            list_files_git,
            list_files_walk,
        )

        fragment = self._fragment_hint or ""

        scope: str | None = None
        if "/" in fragment:
            scope = fragment.rsplit("/", 1)[0]

        now = time.monotonic()
        cache_valid = (
            now - self._cache_time <= self._refresh_interval and self._cache_scope == scope
        )

        # Invalidate on .git/index mtime change.
        if cache_valid and self._is_git:
            mtime = git_index_mtime(self._root)
            if mtime != self._git_index_mtime:
                cache_valid = False

        if cache_valid:
            return self._cached_paths

        if self._is_git is None:
            self._is_git = detect_git(self._root)

        paths: list[str] | None = None
        if self._is_git:
            paths = list_files_git(self._root, scope)
            self._git_index_mtime = git_index_mtime(self._root)
        if paths is None:
            paths = list_files_walk(self._root, scope, limit=self._limit)

        self._cached_paths = paths
        self._cache_scope = scope
        self._cache_time = now
        return self._cached_paths

    @staticmethod
    def _extract_fragment(text: str) -> str | None:
        index = text.rfind("@")
        if index == -1:
            return None

        if index > 0:
            prev = text[index - 1]
            if prev.isalnum() or prev in LocalFileMentionCompleter._TRIGGER_GUARDS:
                return None

        fragment = text[index + 1 :]
        if not fragment:
            return ""

        if any(ch.isspace() for ch in fragment):
            return None

        return fragment

    @staticmethod
    def should_complete(document: Document) -> bool:
        """Return whether `@` file completion should be active for the buffer."""
        return LocalFileMentionCompleter._extract_fragment(document.text_before_cursor) is not None

    def _is_completed_file(self, fragment: str) -> bool:
        candidate = fragment.rstrip("/")
        if not candidate:
            return False
        try:
            return (self._root / candidate).is_file()
        except OSError:
            return False

    @override
    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        fragment = self._extract_fragment(document.text_before_cursor)
        if fragment is None:
            return
        if self._is_completed_file(fragment):
            return

        mention_doc = Document(text=fragment, cursor_position=len(fragment))
        self._fragment_hint = fragment
        try:
            # First, ask the fuzzy completer for candidates.
            candidates = list(self._fuzzy.get_completions(mention_doc, complete_event))

            # re-rank: prefer basename matches
            frag_lower = fragment.lower()

            def _rank(c: Completion) -> tuple[int, ...]:
                path = c.text
                base = path.rstrip("/").split("/")[-1].lower()
                if base.startswith(frag_lower):
                    cat = 0
                elif frag_lower in base:
                    cat = 1
                else:
                    cat = 2
                test_penalty = int(any("test" in segment.lower() for segment in path.split("/")))
                # preserve original FuzzyCompleter's order in the same category
                return (cat, test_penalty)

            candidates.sort(key=_rank)
            yield from candidates
        finally:
            self._fragment_hint = None


class _HistoryEntry(BaseModel):
    content: str


_HISTORY_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)\b((?:authorization\s*:\s*)?(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{8,}"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|token|secret|password|access[_-]?token|"
            r"refresh[_-]?token|id[_-]?token|session[_-]?token)[\"']?\s*[:=]\s*[\"'])"
            r"([^\"'\r\n]{8,})([\"'])"
        ),
        r"\1[REDACTED]\3",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|access[_-]?token|"
            r"refresh[_-]?token|id[_-]?token|session[_-]?token)(\s*[:=]\s*)([^\s'\"&]{8,})"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"\b(sk-[A-Za-z0-9][A-Za-z0-9_-]{16,})\b"), "[REDACTED]"),
    (re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED]"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "[REDACTED]"),
    (re.compile(r"\b(AIza[0-9A-Za-z_-]{20,})\b"), "[REDACTED]"),
)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _redact_history_secrets(text: str) -> str:
    redacted = text
    for pattern, replacement in _HISTORY_SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _ensure_private_history_path(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    if path.exists():
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def _load_history_entries(history_file: Path) -> list[_HistoryEntry]:
    entries: list[_HistoryEntry] = []
    if not history_file.exists():
        return entries

    try:
        with history_file.open(encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "Failed to parse user history line; skipping: {line}",
                        line=line,
                    )
                    continue
                try:
                    entry = _HistoryEntry.model_validate(record)
                    entries.append(entry)
                except ValidationError:
                    logger.warning(
                        "Failed to validate user history entry; skipping: {line}",
                        line=line,
                    )
                    continue
    except OSError as exc:
        logger.warning(
            "Failed to load user history file: {file} ({error})",
            file=history_file,
            error=exc,
        )

    return entries


class PromptMode(Enum):
    AGENT = "agent"
    SHELL = "shell"

    def toggle(self) -> PromptMode:
        return PromptMode.SHELL if self == PromptMode.AGENT else PromptMode.AGENT

    def __str__(self) -> str:
        return self.value


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

_GIT_BRANCH_TTL = 5.0
_GIT_STATUS_TTL = 15.0
_TIP_ROTATE_INTERVAL = 30.0
_MAX_CWD_COLS = 30
_MAX_BRANCH_COLS = 22


@dataclass
class _GitBranchState:
    timestamp: float = 0.0
    branch: str | None = None
    proc: subprocess.Popen[str] | None = None


@dataclass
class _GitStatusState:
    timestamp: float = 0.0
    dirty: bool = False
    ahead: int = 0
    behind: int = 0
    proc: subprocess.Popen[str] | None = None


_git_branch_state = _GitBranchState()
_git_status_state = _GitStatusState()

_GIT_STATUS_AB_RE = re.compile(r"\[(?:ahead (\d+))?(?:, )?(?:behind (\d+))?\]")


def _get_git_branch() -> str | None:
    """Return the current git branch name via a non-blocking cached subprocess."""
    state = _git_branch_state
    now = time.monotonic()

    # Collect result if a previously launched process has finished
    if state.proc is not None:
        returncode = state.proc.poll()
        if returncode is not None:
            try:
                stdout, _ = state.proc.communicate()
                new_branch = stdout.strip() or None
                # Branch changed — discard any in-flight status subprocess so it cannot
                # write stale results for the old branch, then force an immediate refresh.
                if new_branch != state.branch:
                    if _git_status_state.proc is not None:
                        with contextlib.suppress(Exception):
                            _git_status_state.proc.terminate()
                        _git_status_state.proc = None
                    _git_status_state.timestamp = 0.0
                state.branch = new_branch
            except Exception:
                state.branch = None
            state.proc = None

    # Launch a new process when the TTL has expired and nothing is running
    if state.timestamp + _GIT_BRANCH_TTL <= now and state.proc is None:
        state.timestamp = now
        try:
            state.proc = subprocess.Popen(
                ["git", "branch", "--show-current"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception:
            state.branch = None

    return state.branch


def _get_git_status() -> tuple[bool, int, int]:
    """Return (dirty, ahead, behind) via a non-blocking cached subprocess.

    Runs ``git status --porcelain -b`` (includes untracked files so newly created
    files show as dirty).  TTL is longer than the branch check because file-tree
    scanning is expensive.
    """
    state = _git_status_state
    now = time.monotonic()

    if state.proc is not None:
        returncode = state.proc.poll()
        if returncode is not None:
            try:
                stdout, _ = state.proc.communicate()
                dirty = False
                ahead = 0
                behind = 0
                for line in stdout.splitlines():
                    if line.startswith("## "):
                        m = _GIT_STATUS_AB_RE.search(line)
                        if m:
                            ahead = int(m.group(1) or 0)
                            behind = int(m.group(2) or 0)
                    elif line.strip():
                        dirty = True
                state.dirty = dirty
                state.ahead = ahead
                state.behind = behind
            except Exception:
                pass
            state.proc = None
        elif now - state.timestamp > _GIT_STATUS_TTL:
            # Subprocess is stuck (e.g. OS pipe buffer full from many untracked files).
            # Terminate it so the toolbar is not permanently frozen; retry after next TTL.
            with contextlib.suppress(Exception):
                state.proc.terminate()
            state.proc = None
            state.timestamp = now  # delay next spawn by one full TTL

    if state.timestamp + _GIT_STATUS_TTL <= now and state.proc is None:
        state.timestamp = now
        with contextlib.suppress(Exception):
            state.proc = subprocess.Popen(
                ["git", "status", "--porcelain", "-b"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

    return state.dirty, state.ahead, state.behind


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


_GIT_DIFFSTAT_TTL = 15.0


@dataclass
class _GitDiffStatState:
    timestamp: float = 0.0
    added: int = 0
    removed: int = 0
    proc: subprocess.Popen[str] | None = None


_git_diffstat_state = _GitDiffStatState()


def _get_git_diffstat() -> tuple[int, int] | None:
    """Return (added, removed) working-tree line counts via a non-blocking cached
    subprocess. None when not a repo / no changes."""
    from pythinker_code.ui.shell.statusline import parse_shortstat

    state = _git_diffstat_state
    now = time.monotonic()
    if state.proc is not None:
        returncode = state.proc.poll()
        if returncode is not None:
            try:
                stdout, _ = state.proc.communicate()
                state.added, state.removed = parse_shortstat(stdout)
            except Exception:
                logger.debug("git diff --shortstat read/parse failed", exc_info=True)
            state.proc = None
        elif now - state.timestamp > _GIT_DIFFSTAT_TTL:
            with contextlib.suppress(Exception):
                state.proc.terminate()
            state.proc = None
            state.timestamp = now
    if state.timestamp + _GIT_DIFFSTAT_TTL <= now and state.proc is None:
        state.timestamp = now
        with contextlib.suppress(Exception):
            state.proc = subprocess.Popen(
                ["git", "--no-optional-locks", "diff", "--shortstat"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
    if state.added == 0 and state.removed == 0:
        return None
    return state.added, state.removed


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


@dataclass(slots=True)
class _ToastEntry:
    topic: str | None
    """There can be only one toast of each non-None topic in the queue."""
    message: str
    expires_at: float
    style: str = ""
    """Optional prompt_toolkit style for the rendered line; "" uses the default toast style."""


class RunningPromptDelegate(Protocol):
    """Protocol for components that can take over the bottom prompt area."""

    modal_priority: int

    def render_running_prompt_body(self, columns: int) -> AnyFormattedText: ...

    def running_prompt_placeholder(self) -> AnyFormattedText | None: ...

    def running_prompt_allows_text_input(self) -> bool: ...

    def running_prompt_hides_input_buffer(self) -> bool: ...

    def running_prompt_accepts_submission(self) -> bool: ...

    def should_handle_running_prompt_key(self, key: str) -> bool: ...

    def handle_running_prompt_key(self, key: str, event: KeyPressEvent) -> None: ...


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


_toast_queues: dict[Literal["left", "right"], deque[_ToastEntry]] = {
    "left": deque(),
    "right": deque(),
}
"""The queue of toasts to show, including the one currently being shown (the first one)."""


def toast(
    message: str,
    duration: float = 5.0,
    topic: str | None = None,
    immediate: bool = False,
    position: Literal["left", "right"] = "left",
    style: str = "",
) -> None:
    queue = _toast_queues[position]
    duration = max(duration, _IDLE_REFRESH_INTERVAL)
    entry = _ToastEntry(
        topic=topic,
        message=message,
        expires_at=time.monotonic() + duration,
        style=style,
    )
    if topic is not None:
        # Remove existing toasts with the same topic
        for existing in list(queue):
            if existing.topic == topic:
                queue.remove(existing)
    if immediate:
        queue.appendleft(entry)
    else:
        queue.append(entry)


def _current_toast(position: Literal["left", "right"] = "left") -> _ToastEntry | None:
    queue = _toast_queues[position]
    now = time.monotonic()
    while queue and queue[0].expires_at <= now:
        queue.popleft()
    if not queue:
        return None
    return queue[0]


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
    ) -> None:
        from pythinker_code.ui.shell.statusline import (
            RateSampler,
            StatusLineCommandRunner,
            resolve_segments,
        )

        _statusline_cfg = statusline_config or StatusLineConfig()
        self._statusline_layout = resolve_segments(_statusline_cfg)
        self._statusline_runner: StatusLineCommandRunner | None = None
        if self._statusline_layout.show_command and _statusline_cfg.command:
            self._statusline_runner = StatusLineCommandRunner(
                command=_statusline_cfg.command,
                timeout_ms=_statusline_cfg.command_timeout_ms,
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
            _ensure_private_history_path(self._history_file)
        self._status_provider = status_provider
        self._status_block_provider = status_block_provider
        self._fast_refresh_provider = fast_refresh_provider
        self._background_task_count_provider = background_task_count_provider
        self._update_notice_provider = update_notice_provider
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
        self._latest_todos: tuple[TodoDisplayItem, ...] = ()
        self._modal_delegates: list[RunningPromptDelegate] = []
        self._shortcut_help_open = False
        self._prompt_buffer_container: ConditionalContainer | None = None
        self._slash_menu_control: SlashCommandMenuControl | None = None
        self._last_ui_state: PromptUIState = PromptUIState.NORMAL_INPUT
        self._suspended_buffer_document: Document | None = None
        clipboard_available = is_clipboard_available()
        media_clipboard_available = is_media_clipboard_available()
        self._tips = _build_toolbar_tips(clipboard_available or media_clipboard_available)
        self._tip_rotation_index: int = random.randrange(len(self._tips)) if self._tips else 0

        history_entries = _load_history_entries(self._history_file) if self._history_enabled else []
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
        self._agent_mode_completer = merge_completers(
            [
                self._agent_slash_completer,
                # TODO(host): we need an async HostFileMentionCompleter
                LocalFileMentionCompleter(HostPath.cwd().unsafe_to_local_path()),
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
            """Slash command completion: Escape discards the draft command."""
            if _discard_slash_command(event.current_buffer):
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
            self._shortcut_help_open = not self._shortcut_help_open
            event.app.invalidate()

        @_kb.add("c-x", eager=True)
        def _(event: KeyPressEvent) -> None:
            if self._active_prompt_delegate() is not None:
                return
            self._mode = self._mode.toggle()
            from pythinker_code.telemetry import track

            track("shortcut_mode_switch", to_mode=self._mode.value)
            # Apply mode-specific settings
            self._apply_mode(event)
            # Redraw UI
            event.app.invalidate()

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
            self._shortcut_help_open = False
            event.app.invalidate()

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
                    try:
                        clipboard_data = event.app.clipboard.get_data()
                    except Exception:
                        return
                    if clipboard_data is None:  # type: ignore[reportUnnecessaryComparison]
                        return
                    self._insert_pasted_text(event.current_buffer, clipboard_data.text)
                    event.app.invalidate()

        # Only use PyperclipClipboard when pyperclip actually works.
        # PromptSession built-in keybindings (ctrl-k, ctrl-w, ctrl-y)
        # use clipboard without error handling, so a broken clipboard
        # object would crash the UI.
        clipboard = PyperclipClipboard() if clipboard_available else None

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
        self._install_prompt_exception_filter()
        self._install_slash_completion_menu()
        self._install_prompt_buffer_visibility()
        self._apply_mode()

        # Allow completion to be triggered when the text is changed,
        # such as when backspace is used to delete text.
        @self._session.default_buffer.on_text_changed.add_handler
        def _(buffer: Buffer) -> None:
            self._last_input_activity_time = time.monotonic()
            self._input_activity_event.set()
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

    def _install_prompt_exception_filter(self) -> None:
        """Avoid prompt_toolkit's blocking ``Exception None`` terminal pause."""
        app = self._session.app
        original_handler = app._handle_exception  # pyright: ignore[reportPrivateUsage]

        def _handle_exception(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            if _is_prompt_toolkit_empty_exception_context(context):
                logger.debug(
                    "Suppressed prompt_toolkit empty exception context: {context}",
                    context={k: repr(v) for k, v in context.items()},
                )
                return
            original_handler(loop, context)

        app._handle_exception = _handle_exception  # pyright: ignore[reportPrivateUsage]

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

    def _effort_label_fragments(self) -> list[tuple[str, str]]:
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

    def _render_input_top_border(self, columns: int, fallback: str) -> list[tuple[str, str]]:
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
        label_width = sum(get_cwidth(ch) for _, text in label for ch in text)
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
        app = get_app_or_none()
        size = app.output.get_size() if app is not None else None
        columns = size.columns if size is not None else 80
        fragments: FormattedText = FormattedText()

        if getattr(self, "_shortcut_help_open", False):
            fragments.extend(self._render_shortcut_help(columns))
            ensure_prompt_newline(fragments)

        # Dynamic preamble (agent status + modal/interactive body). Keep it
        # within the visible terminal area so it cannot overlap the input/footer.
        preamble: FormattedText = FormattedText()
        agent_status = self._render_agent_status(columns)
        if agent_status:
            preamble.extend(agent_status)
            ensure_prompt_newline(preamble)

        body = self._render_interactive_body(columns)
        if body:
            preamble.extend(body)
            ensure_prompt_newline(preamble)

        pinned = self._render_pinned_status_tail(columns)
        if preamble or pinned:
            preamble = self._fit_preamble_with_pinned_tail(
                preamble,
                pinned,
                columns,
                _prompt_preamble_max_rows(getattr(size, "rows", None)),
            )
            fragments.extend(preamble)

        if self._active_modal_delegate() is not None:
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
            app.erase_when_done = self._mode == PromptMode.AGENT

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

    def _active_ui_state(self) -> PromptUIState:
        delegate = self._active_modal_delegate()
        if delegate is None:
            return PromptUIState.NORMAL_INPUT
        if delegate.running_prompt_hides_input_buffer():
            return PromptUIState.MODAL_HIDDEN_INPUT
        if delegate.running_prompt_allows_text_input():
            return PromptUIState.MODAL_TEXT_INPUT
        return PromptUIState.NORMAL_INPUT

    def _should_render_input_buffer(self) -> bool:
        return self._active_ui_state() != PromptUIState.MODAL_HIDDEN_INPUT

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
        new_state = self._active_ui_state()
        old_state = getattr(self, "_last_ui_state", PromptUIState.NORMAL_INPUT)
        buffer = self._session.default_buffer

        if (
            old_state != PromptUIState.MODAL_HIDDEN_INPUT
            and new_state == PromptUIState.MODAL_HIDDEN_INPUT
        ):
            if self._suspended_buffer_document is None and buffer.text:
                self._suspended_buffer_document = buffer.document
                buffer.set_document(Document(), bypass_readonly=True)
        elif (
            old_state == PromptUIState.MODAL_HIDDEN_INPUT
            and new_state != PromptUIState.MODAL_HIDDEN_INPUT
            and self._suspended_buffer_document is not None
        ):
            if not buffer.text:
                buffer.set_document(self._suspended_buffer_document, bypass_readonly=True)
            else:
                # Buffer was externally modified (e.g. approval inline feedback).
                # Don't overwrite the new content, but log that the old input is lost.
                logger.debug(
                    "Dropping suspended buffer document because buffer was modified externally"
                )
            self._suspended_buffer_document = None

        self._last_ui_state = new_state

    def _render_agent_prompt_message(self) -> FormattedText:
        app = get_app_or_none()
        size = app.output.get_size() if app is not None else None
        columns = size.columns if size is not None else 80
        fragments: FormattedText = FormattedText()

        # 1–2. Dynamic preamble — agent status is always rendered from the
        # running prompt delegate, and body comes from the active modal/delegate.
        # Cap the visible rows so large cards do not overwrite the input/footer.
        # When a modal is active, preserve the whole modal body and clip older
        # agent status above it first; approval/question controls must remain usable.
        agent_status = self._render_agent_status(columns)
        body = self._render_interactive_body(columns)
        pinned = self._render_pinned_status_tail(columns)
        body_rows = (
            len(_formatted_text_display_rows(body, columns))
            if body and any(fragment for _, fragment, *_ in body)
            else 0
        )
        pinned_rows = (
            len(_formatted_text_display_rows(pinned, columns))
            if pinned and any(fragment for _, fragment, *_ in pinned)
            else 0
        )
        max_rows = _prompt_preamble_max_rows(getattr(size, "rows", None))
        modal_active = self._active_modal_delegate() is not None

        if getattr(self, "_shortcut_help_open", False) and not modal_active:
            fragments.extend(self._render_shortcut_help(columns))
            ensure_prompt_newline(fragments)

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
            return fragments

        if is_card_style():
            ensure_prompt_newline(fragments)
            tc = get_toolbar_colors()
            fragments.extend(self._render_input_top_border(columns, tc.separator))
            fragments.append(("", "\n"))
            fragments.append(("", _card_side_indent()))
        else:
            fragments.append(("", "\n"))
        fragments.append((self._thinking_prompt_prefix_style(), f"{PROMPT_SYMBOL_AGENT_INPUT} "))
        return fragments

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

    def _render_agent_status(self, columns: int) -> FormattedText:
        """Render agent streaming output (always visible, independent of modals)."""
        running = self._running_prompt_delegate
        if running is not None and isinstance(running, AgentStatusProvider):
            rendered = to_formatted_text(running.render_agent_status(columns))
            if any(fragment for _, fragment, *_ in rendered):
                # A blocking foreground TaskOutput card can be visible while the
                # actual background agent is still running. If the live view does
                # not expose a pinned tail for that state, keep the background
                # verb spinner visible above the prompt instead of showing only
                # the footer count.
                if not self._render_pinned_status_tail(columns):
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
        pinned_active = bool(self._render_pinned_status_tail(columns))
        fragments = (
            FormattedText([]) if pinned_active else self._render_background_working_status(columns)
        )
        status = self._render_status_block(columns)
        if status:
            ensure_prompt_newline(fragments)
            fragments.extend(status)
        return fragments

    def _render_pinned_status_tail(self, columns: int) -> FormattedText:
        """Trailing verb spinner that stays pinned below a clipped agent stream."""
        running = self._running_prompt_delegate
        if running is not None and isinstance(running, PinnedStatusTailProvider):
            rendered = to_formatted_text(running.render_pinned_status_tail(columns))
            if any(fragment for _, fragment, *_ in rendered):
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
            # No separate pinned tail: keep the old behavior of preserving the
            # last status row so delegates that don't split stay correct.
            return _fit_formatted_text_to_rows(preamble, columns, max_rows, preserve_tail_rows=1)
        pinned_rows = len(_formatted_text_display_rows(pinned, columns))
        body_budget = max(1, max_rows - pinned_rows)
        clipped = _fit_formatted_text_to_rows(preamble, columns, body_budget)
        out: FormattedText = FormattedText()
        out.extend(clipped)
        ensure_prompt_newline(out)
        # Keep the pinned verb spinner visually separated from preceding tool
        # output/background summaries; when it is the first visible row, this
        # also creates the initial breathing room above the spinner.
        out.append(("", "\n"))
        out.extend(pinned)
        # Mirror the breathing room below the pinned tail so the todo list / verb
        # spinner is never flush against the prompt separator beneath it.
        ensure_prompt_newline(out)
        out.append(("", "\n"))
        return out

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
        elapsed = max(0.0, now - started_at)
        frame = active_marker_frame(elapsed)
        tokens = _get_tui_tokens()
        muted_style = f"fg:{tokens.muted}" if tokens.muted else ""
        frame_style = f"fg:{tokens.activity_spinner}" if tokens.activity_spinner else muted_style
        frame_text = f"{frame} "
        verb_text = spinner_message(now)
        metadata = self._background_status_metadata(now)
        suffix = f" {metadata}" if metadata else ""
        if _display_width(frame_text + verb_text + suffix) > columns:
            # Narrow terminals: drop the metadata first, then trim the verb.
            suffix = ""
            if _display_width(frame_text + verb_text) > columns:
                verb_text = _truncate_right(verb_text, columns - _display_width(frame_text))
        fragments = FormattedText(
            [
                (frame_style, frame_text),
                *shimmer_prompt_fragments(verb_text, now),
            ]
        )
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

    def _render_interactive_body(self, columns: int) -> FormattedText:
        """Render the interactive area from the active delegate (modal or running prompt)."""
        delegate = self._active_prompt_delegate()
        if delegate is None:
            return FormattedText([])
        return to_formatted_text(delegate.render_running_prompt_body(columns))

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

    def __enter__(self) -> CustomPromptSession:
        if self._status_refresh_task is not None and not self._status_refresh_task.done():
            return self

        async def _refresh() -> None:
            try:
                while True:
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
                        or self._has_background_tasks()
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

        self._status_refresh_task = asyncio.create_task(_refresh())
        if self._statusline_runner is not None:
            self._statusline_runner.start()
        return self

    def __exit__(self, *_) -> None:
        if self._status_refresh_task is not None and not self._status_refresh_task.done():
            self._status_refresh_task.cancel()
        self._status_refresh_task = None
        if self._statusline_runner is not None:
            self._statusline_runner.cancel()

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
            result = grab_media_from_clipboard()
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

    def attach_running_prompt(self, delegate: RunningPromptDelegate) -> None:
        current = getattr(self, "_running_prompt_delegate", None)
        if current is delegate:
            return
        if current is None:
            self._running_prompt_previous_mode = self._mode
        self._running_prompt_delegate = delegate
        self._mode = PromptMode.AGENT
        self._apply_mode()
        self.invalidate()

    def detach_running_prompt(self, delegate: RunningPromptDelegate) -> None:
        if getattr(self, "_running_prompt_delegate", None) is not delegate:
            return
        previous_mode = getattr(self, "_running_prompt_previous_mode", None)
        self._running_prompt_delegate = None
        self._running_prompt_previous_mode = None
        if previous_mode is not None:
            self._mode = previous_mode
        self._apply_mode()
        self.invalidate()

    def attach_modal(self, delegate: RunningPromptDelegate) -> None:
        modal_delegates: list[RunningPromptDelegate] | None = getattr(
            self, "_modal_delegates", None
        )
        if modal_delegates is None:
            modal_delegates = []
            self._modal_delegates = modal_delegates
        if delegate in modal_delegates:
            return
        modal_delegates.append(delegate)
        self.invalidate()

    def detach_modal(self, delegate: RunningPromptDelegate) -> None:
        modal_delegates = getattr(self, "_modal_delegates", None)
        if not modal_delegates or delegate not in modal_delegates:
            return
        modal_delegates.remove(delegate)
        self.invalidate()

    def running_prompt_accepts_submission(self) -> bool:
        delegate = self._active_prompt_delegate()
        if delegate is None:
            return False
        return delegate.running_prompt_accepts_submission()

    async def _prompt_once(self, *, append_history: bool | None) -> UserInput:
        placeholder = None
        if (delegate := self._active_prompt_delegate()) is not None:
            placeholder = delegate.running_prompt_placeholder()
        # Consume one-shot prefill text if set
        default = getattr(self, "_prefill_text", None) or ""
        self._prefill_text = None
        self._staged_suggestion_prefill = None
        with patch_stdout(raw=True):
            command = str(
                await self._session.prompt_async(placeholder=placeholder, default=default)
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

        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            _ensure_private_history_path(self._history_file)
            fd = os.open(self._history_file, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(entry.model_dump_json(ensure_ascii=False) + "\n")
            with contextlib.suppress(OSError):
                os.chmod(self._history_file, 0o600)
            self._last_history_content = entry.content
        except OSError as exc:
            logger.warning(
                "Failed to append user history entry: {file} ({error})",
                file=self._history_file,
                error=exc,
            )

    def _prepend_update_notice(self, fragments: list[tuple[str, str]], columns: int) -> None:
        """Prepend a persistent yellow 'update available' line above the footer
        separator, so it renders directly under the prompt input. No-op when no
        update is pending; style-agnostic across both toolbar layouts."""
        provider = getattr(self, "_update_notice_provider", None)
        if provider is None:
            return
        text = provider()
        if not text:
            return
        line = _truncate_right(text, max(0, columns - 1))
        if not line:
            return
        tokens = _get_tui_tokens()
        style = f"fg:{tokens.warning or 'ansiyellow'} bold"
        fragments[:0] = [(style, line), ("", "\n")]

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

        # Pythinker footer dispatch. Mirrors components/footer.ts layout while
        # reusing the existing data sources so we never lose information vs
        # the legacy toolbar.
        from pythinker_code.ui.tui_config import is_card_style

        if is_card_style():
            return self._render_card_bottom_toolbar(columns)

        fragments: list[tuple[str, str]] = []
        tc = get_toolbar_colors()

        self._prepend_update_notice(fragments, columns)
        fragments.append((self._prompt_separator_style(tc.separator), _prompt_rule(columns)))
        fragments.append(("", "\n"))

        remaining = columns

        # Time-based tip rotation (every 30 s, independent of user submissions)
        now = time.monotonic()
        if now - self._last_tip_rotate_time >= _TIP_ROTATE_INTERVAL:
            self._tip_rotation_index += 1
            self._last_tip_rotate_time = now

        # Status flags: yolo / auto / plan
        status = self._status_provider()
        if status.yolo_enabled:
            fragments.extend([(tc.yolo_label, "yolo"), ("", "  ")])
            remaining -= 6  # "yolo" = 4, "  " = 2
        if status.auto_enabled:
            fragments.extend([(tc.auto_label, "auto"), ("", "  ")])
            remaining -= 6  # "auto" = 4, "  " = 2
        if status.plan_mode:
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
        try:
            cwd = _truncate_left(_shorten_cwd(str(HostPath.cwd())), _MAX_CWD_COLS)
        except OSError:
            # CWD no longer exists (e.g. external drive unplugged).  Ask
            # prompt_toolkit to exit; the raised exception will propagate out
            # of prompt_async() into the Shell's event router which prints a
            # crash report with session info and exits cleanly.
            app.exit(exception=CwdLostError())
            return FormattedText([])
        branch = _get_git_branch()
        if branch:
            dirty, ahead, behind = _get_git_status()
            branch = _truncate_right(branch, _MAX_BRANCH_COLS)
            badge = _format_git_badge(branch, dirty, ahead, behind)
            cwd_text = f"{cwd}  {badge}"
        else:
            cwd_text = cwd
        cwd_w = _display_width(cwd_text)
        if cwd_w > remaining - 2:
            cwd_text = cwd  # drop badge
            cwd_w = _display_width(cwd_text)
        if cwd_w > remaining - 2:
            cwd_text = _truncate_right(cwd, max(0, remaining - 2))
            cwd_w = _display_width(cwd_text)
        if cwd_text and remaining >= cwd_w + 2:
            fragments.extend([(tc.cwd, cwd_text), ("", "  ")])
            remaining -= cwd_w + 2

        # Active background task counts (bash + agent, each rendered as its own
        # badge). Order matters: bash renders first; if there isn't room for the
        # agent badge too, drop agent and keep bash.
        bg_counts = (
            self._background_task_count_provider()
            if self._background_task_count_provider
            else BgTaskCounts()
        )
        for kind_label, kind_count in (("bash", bg_counts.bash), ("agent", bg_counts.agent)):
            if kind_count <= 0:
                continue
            bg_text = f"◇ {kind_label}: {kind_count}"
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

        right_text = self._render_right_span(status)
        right_width = _display_width(right_text)

        left_toast = _current_toast("left")
        if left_toast is not None:
            max_left = max(0, columns - right_width - 2)
            if max_left > 0:
                left_text = left_toast.message
                if _display_width(left_text) > max_left:
                    left_text = _truncate_right(left_text, max_left)
                left_width = _display_width(left_text)
                fragments.append((left_toast.style or secondary_style, left_text))
            else:
                left_width = 0
        else:
            left_width = 0

        fragments.append(("", " " * max(0, columns - left_width - right_width)))
        fragments.append((secondary_style, right_text))

        return FormattedText(fragments)

    def _build_statusline_context(self, columns: int) -> StatusLineContext:
        from pythinker_code.ui.shell.statusline import (
            GitInfo,
            RateSampler,
            StatusFlags,
            StatusLineContext,
        )
        from pythinker_code.ui.terminal_capabilities import ascii_glyphs_enabled

        cfg = getattr(self, "_statusline_cfg", None) or StatusLineConfig()
        status = self._status_provider()
        now = time.monotonic()

        self._statusline_frame = getattr(self, "_statusline_frame", 0) + 1
        working = self._has_background_tasks()

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

        try:
            cwd_text = _truncate_left(_shorten_cwd(str(HostPath.cwd())), _MAX_CWD_COLS)
        except OSError as exc:
            raise CwdLostError() from exc

        git_info: GitInfo | None = None
        branch = _get_git_branch()
        if branch:
            dirty, ahead, behind = _get_git_status()
            git_info = GitInfo(
                branch=_truncate_right(branch, _MAX_BRANCH_COLS),
                dirty=dirty,
                ahead=ahead,
                behind=behind,
            )

        diff = _get_git_diffstat()
        diff_added, diff_removed = diff if diff is not None else (None, None)

        thinking_effort = getattr(self, "_thinking_effort", None)
        effort = thinking_effort if thinking_effort in ("high", "medium", "low") else None

        started = getattr(self, "_statusline_started_at", None)
        elapsed_s = (now - started) if started is not None else 0.0

        return StatusLineContext(
            columns=columns,
            working=working,
            frame=self._statusline_frame,
            model_name=self._model_name,
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
            ascii_only=ascii_glyphs_enabled(),
            style=cfg.style if cfg.enabled else "plain",
            bar_width=cfg.bar_width,
        )

    def _render_card_bottom_toolbar(self, columns: int) -> FormattedText:
        """Pythinker two-line footer (statusline v2).

        Line 1 + line-2 right are assembled from the segment registry; the
        line-2 left side keeps the command/extension/background/toast
        precedence from the legacy footer.
        """
        from pythinker_code.config import StatusLineConfig
        from pythinker_code.extensions import footer_statuses
        from pythinker_code.ui.shell.statusline import (
            DEFAULT_STATUSLINE_SEGMENTS,
            assemble_footer,
        )

        cfg = getattr(self, "_statusline_cfg", None) or StatusLineConfig()
        tc = get_toolbar_colors()
        tokens = _get_tui_tokens()
        secondary_style = f"fg:{tokens.muted}"

        fragments: list[tuple[str, str]] = []
        self._prepend_update_notice(fragments, columns)
        fragments.append((self._prompt_separator_style(tc.separator), _prompt_rule(columns)))
        fragments.append(("", "\n"))

        try:
            ctx = self._build_statusline_context(columns)
        except CwdLostError as exc:
            app = get_app_or_none()
            if app is not None:
                app.exit(exception=exc)
            return FormattedText([])

        segments = list(cfg.segments) if cfg.enabled else list(DEFAULT_STATUSLINE_SEGMENTS)
        line1, line2_right = assemble_footer(ctx, segments)
        fragments.extend(line1)
        fragments.append(("", "\n"))

        # Reserve the last column (like _prompt_rule) so writing the final cell
        # can't wrap the line on terminals such as Windows conhost / PowerShell.
        usable = max(0, columns - 1)

        right_text = "".join(t for _, t in line2_right)
        right_width = _display_width(right_text)
        if right_width > usable:
            right_text = _truncate_left(right_text, usable)
            line2_right = [(secondary_style, right_text)]
            right_width = _display_width(right_text)

        max_left_width = max(0, usable - right_width - 1)
        command_line = ""
        runner = getattr(self, "_statusline_runner", None)
        if cfg.enabled and "command" in segments and runner is not None:
            command_line = runner.current_line
        ext = footer_statuses()
        if command_line:
            command_line = _truncate_right(command_line, max_left_width)
            fragments.append((tc.tip, command_line))
            left_width = _display_width(command_line)
        elif ext:
            ordered = sorted(ext.items())
            ext_line = " ".join(f"{k}:{v}" for k, v in ordered)
            ext_line = _truncate_right(ext_line, max_left_width)
            fragments.append((tc.tip, ext_line))
            left_width = _display_width(ext_line)
        elif (
            bg_summary := _background_task_summary(
                self._background_task_count_provider()
                if self._background_task_count_provider
                else BgTaskCounts()
            )
        ) is not None:
            bg_summary = _truncate_right(bg_summary, max_left_width)
            fragments.append((tc.bg_tasks, bg_summary))
            left_width = _display_width(bg_summary)
        else:
            left_toast = _current_toast("left")
            if left_toast is not None:
                left_text = _truncate_right(left_toast.message, max_left_width)
                fragments.append((left_toast.style or secondary_style, left_text))
                left_width = _display_width(left_text)
            else:
                left_width = 0

        fragments.append(("", " " * max(0, usable - left_width - right_width)))
        fragments.extend(line2_right)
        return FormattedText(fragments)

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

    @staticmethod
    def _render_right_span(status: StatusSnapshot) -> str:
        current_toast = _current_toast("right")
        if current_toast is None:
            return format_context_status(
                status.context_usage,
                status.context_tokens,
                status.max_context_tokens,
            )
        return current_toast.message
