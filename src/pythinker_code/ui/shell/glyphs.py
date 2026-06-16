"""Canonical glyphs for shell TUI animations.

Single source for the loading-spinner frames and the reduced-motion glyph that
were previously duplicated between :mod:`pythinker_code.ui.shell.motion` and
:mod:`pythinker_code.ui.shell.spinner_words`.
"""

from __future__ import annotations

import sys
from typing import Final

from pythinker_code.ui.terminal_capabilities import ascii_glyphs_enabled

_ASCII_GLYPHS = ascii_glyphs_enabled()

#: Braille dotted spinner frames, rendered left-to-right per tick. ASCII mode is
#: available for legacy Windows code pages, ``TERM=dumb``, and users who set
#: ``PYTHINKER_TUI_GLYPHS=ascii``.
SPINNER_FRAMES: Final = (
    ("-", "\\", "|", "/") if _ASCII_GLYPHS else ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
)
#: Text-safe pulse for the Composing / Thinking activity lines instead of the
#: dotted braille spinner. Use the filled text circle so active transcript,
#: tool, and subagent markers read at the same weight as the reference CLI.
#: ASCII mode keeps legacy Windows code pages, ``TERM=dumb``, and explicit safe
#: glyph requests on a plain star. The blank frame keeps the label column stable
#: while making the marker appear and disappear.
SHAPE_FRAMES: Final = ("*", " ") if _ASCII_GLYPHS else ("●", " ")
#: Seconds each braille spinner frame stays on screen.
SPINNER_FRAME_INTERVAL_S: Final = 0.08
#: Seconds each text-safe pulse frame stays on screen.
SHAPE_FRAME_INTERVAL_S: Final = 0.45
#: Static stand-in used when motion is disabled.
REDUCED_MOTION_GLYPH: Final = "*" if _ASCII_GLYPHS else "●"
#: Braille dotted frames for the active task/status marker beside pinned todos.
ACTIVE_MARKER_FRAMES: Final = SPINNER_FRAMES
#: Seconds each active-marker braille frame stays on screen.
ACTIVE_MARKER_FRAME_INTERVAL_S: Final = SPINNER_FRAME_INTERVAL_S
#: Backward-compatible aliases for the historical star spinner names.
STAR_SPINNER_FRAMES: Final = ACTIVE_MARKER_FRAMES
STAR_SPINNER_FRAME_INTERVAL_S: Final = ACTIVE_MARKER_FRAME_INTERVAL_S

#: Transcript row marker for assistant/tool-call lines. U+23FA (record button)
#: matches the reference CLI look on macOS/Linux terminals; some Windows
#: terminals render it as a blue emoji tile, so Windows keeps the monochrome
#: text circle and ASCII mode keeps the star.
TRANSCRIPT_ASSISTANT_MARKER: Final = (
    "*" if _ASCII_GLYPHS else ("●" if sys.platform == "win32" else "⏺")
)
#: Transcript prompt marker for submitted user input.
TRANSCRIPT_PROMPT_MARKER: Final = ">" if _ASCII_GLYPHS else "❯"
#: Transcript marker for completed thinking/status timing rows.
TRANSCRIPT_STATUS_MARKER: Final = "*" if _ASCII_GLYPHS else "✻"
#: Transcript marker for active task/status rows when motion is disabled.
TRANSCRIPT_ACTIVE_MARKER: Final = REDUCED_MOTION_GLYPH
#: Transcript gutter marker for tool results.
TRANSCRIPT_TOOL_GUTTER: Final = "|" if _ASCII_GLYPHS else "⎿"
#: List/detail bullet (U+2022) used in status panels; falls back to an asterisk
#: under ASCII mode so legacy code pages and ``TERM=dumb`` stay clean.
LIST_BULLET: Final = "*" if _ASCII_GLYPHS else "•"
#: Canonical glyph shown before a question prompt (AskUserQuestion tool).
#: Unambiguously signals "this is a question" in every context: transcript
#: inline view, interactive dialog body, pager, and ``prompt_other_input``.
#: ASCII mode falls back to plain ``?`` for legacy terminals.
QUESTION_MARKER: Final = "?" if _ASCII_GLYPHS else "❓"
#: Report finding location rows (file path + line refs).
REPORT_FILE_MARKER: Final = "+" if _ASCII_GLYPHS else "⌁"

__all__ = [
    "SPINNER_FRAMES",
    "SHAPE_FRAMES",
    "ACTIVE_MARKER_FRAMES",
    "SPINNER_FRAME_INTERVAL_S",
    "SHAPE_FRAME_INTERVAL_S",
    "ACTIVE_MARKER_FRAME_INTERVAL_S",
    "STAR_SPINNER_FRAMES",
    "STAR_SPINNER_FRAME_INTERVAL_S",
    "REDUCED_MOTION_GLYPH",
    "TRANSCRIPT_ASSISTANT_MARKER",
    "TRANSCRIPT_PROMPT_MARKER",
    "TRANSCRIPT_STATUS_MARKER",
    "TRANSCRIPT_ACTIVE_MARKER",
    "TRANSCRIPT_TOOL_GUTTER",
    "LIST_BULLET",
    "QUESTION_MARKER",
    "REPORT_FILE_MARKER",
]
