# tests/ui_and_conv/_md_contract_helpers.py
"""Shared helpers for Markdown + report contract tests.

DRY home for the two capture modes the repo already uses (plain text and
ANSI-preserving) plus an idempotency comparator. Mirrors the console
configuration in tests/ui_and_conv/test_tui_render_snapshots.py and
tests/ui_and_conv/test_report.py so captured output matches the rest of the
suite.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Console, RenderableType

from pythinker_code.ui.shell.components.render_utils import sanitize_ansi

# Widths that exercise reflow boundaries: very narrow, a normal width, and an
# exactly-typical report width. Add the exact-full-width case per test.
WIDTHS: tuple[int, ...] = (24, 40, 80)
THEMES: tuple[str, ...] = ("dark", "light")


def render_plain(renderable: RenderableType, *, width: int = 80) -> str:
    """Capture *renderable* as plain text (no color), like test_report._plain."""
    console = Console(width=width, no_color=True, legacy_windows=False)
    with console.capture() as cap:
        console.print(renderable)
    return sanitize_ansi(cap.get())


def render_ansi(renderable: RenderableType, *, width: int = 80) -> str:
    """Capture *renderable* keeping ANSI escapes, like test_tui_render_snapshots._ansi."""
    console = Console(
        width=width,
        record=True,
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
    )
    console.print(renderable)
    return console.export_text(styles=True)


def render_twice_identical(build: Callable[[], RenderableType], *, width: int = 80) -> bool:
    """Render a freshly-built renderable twice; True iff byte-identical.

    `build` returns a NEW renderable each call so we test render determinism,
    not object identity.
    """
    return render_ansi(build(), width=width) == render_ansi(build(), width=width)
