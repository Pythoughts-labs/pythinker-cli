"""Tests for the large-diff guard and head-tail tool-output truncation."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.text import Text

import pytest

from pythinker_code.ui.shell.components import (
    ToolExecutionComponent,
    render_plain,
)
from pythinker_code.ui.shell.render_constants import DIFF_EXPANDED_MAX_LINES
from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderDefinition,
    ToolResultPayload,
    clear_tool_renderers,
)
from pythinker_code.ui.shell.tool_renderers._file_diff import diff_frame


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    clear_tool_renderers()
    yield
    clear_tool_renderers()


# ---------------------------------------------------------------------------
# Expanded diff guard
# ---------------------------------------------------------------------------


def _synthetic_diff(line_count: int) -> str:
    return "\n".join(f"+{i} added line {i}" for i in range(1, line_count + 1))


def test_expanded_diff_is_capped_with_head_and_tail() -> None:
    total = DIFF_EXPANDED_MAX_LINES + 600
    out = render_plain(diff_frame(_synthetic_diff(total), width=100, expanded=True), width=120)
    assert "added line 1 " in out or "added line 1\n" in out
    assert f"added line {total}" in out
    omitted = total - DIFF_EXPANDED_MAX_LINES
    assert f"… {omitted} middle lines omitted (diff too large to render fully)" in out


def test_small_expanded_diff_renders_fully() -> None:
    out = render_plain(diff_frame(_synthetic_diff(20), width=100, expanded=True), width=120)
    assert "middle lines omitted" not in out
    assert "added line 20" in out


def test_collapsed_diff_keeps_expand_hint() -> None:
    state: dict[str, object] = {}
    out = render_plain(
        diff_frame(_synthetic_diff(40), width=100, expanded=False, state=state), width=120
    )
    assert "more line" in out
    assert state["__has_expandable_payload__"] is True


# ---------------------------------------------------------------------------
# Head-tail truncation of generic tool output
# ---------------------------------------------------------------------------


def _generic_component(text: str, *, expanded: bool = False) -> ToolExecutionComponent:
    comp = ToolExecutionComponent(
        "Anything", "t1", definition=ToolRenderDefinition(name="Anything", label="Anything")
    )
    comp.mark_execution_started()
    comp.set_result(ToolResultPayload(text=text))
    if expanded:
        comp.toggle_expanded()
    return comp


def test_long_tool_output_keeps_head_and_tail() -> None:
    text = "\n".join(f"line {i}" for i in range(1, 201))
    out = render_plain(_generic_component(text).render(), width=120)
    assert "line 1\n" in out or "line 1 " in out
    assert "line 200" in out
    assert "140 lines omitted" in out
    assert "full result preserved in session" in out


def test_single_giant_line_keeps_both_ends() -> None:
    # Assert on the composed Text (render_plain would ellipsis-crop the long
    # line at card width, hiding the tail character from the plain dump).
    text = "S" + "x" * 9000 + "E"
    body = _generic_component(text)._result_fallback()
    assert body is not None
    plain = body.plain  # type: ignore[union-attr]
    assert plain.startswith("S")
    assert "… middle omitted …" in plain
    assert "E" in plain.split("middle omitted")[1]
    assert len(plain) < len(text)


def test_short_tool_output_is_untouched() -> None:
    out = render_plain(_generic_component("just one line").render(), width=120)
    assert "omitted" not in out
    assert "truncated" not in out


def test_expanded_tool_output_is_never_truncated() -> None:
    text = "\n".join(f"line {i}" for i in range(1, 201))
    out = render_plain(_generic_component(text, expanded=True).render(), width=120)
    assert "omitted" not in out
    assert "line 137" in out


def _render_diff_text(*args: object, **kwargs: object) -> Text:
    """Flatten render_diff output into Rich Text for span assertions."""
    from rich.console import Console
    from rich.text import Text

    from pythinker_code.ui.shell.components.diff import render_diff

    renderable = render_diff(*args, **kwargs)  # type: ignore[arg-type]
    cons = Console(
        width=120,
        record=True,
        force_terminal=True,
        _environ={"TERM": "xterm-256color"},
    )
    segments = list(cons.render(renderable, cons.options.update_width(120)))
    text = Text()
    for seg in segments:
        if seg.text not in {"\n", "\r\n"}:
            text.append(seg.text, style=seg.style)
    return text


def test_word_level_diff_highlight_gated_on_similarity() -> None:
    """Mostly-similar single-line edits get word-level highlight tints; heavy
    rewrites render as plain rows so the row palette stays consistent."""
    from pythinker_code.ui.theme import get_diff_colors, set_active_theme

    set_active_theme("dark")
    hl_bg = get_diff_colors().add_hl.bgcolor

    similar = _render_diff_text("-1 alpha beta gamma\n+1 alpha beta delta")
    similar_bgs = {
        (span.style.bgcolor if not isinstance(span.style, str) else None) for span in similar.spans
    }
    assert hl_bg in similar_bgs

    rewrite = _render_diff_text("-1 alpha beta gamma\n+1 zzz qqq xxx yyy www vvv")
    rewrite_bgs = {
        (span.style.bgcolor if not isinstance(span.style, str) else None) for span in rewrite.spans
    }
    assert hl_bg not in rewrite_bgs
