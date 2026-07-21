"""Cross-config render matrix: width x NO_COLOR x reduced-motion.

Guards the spacing/diff/markdown work in the TUI render tracks against
config-specific regressions (narrow terminals, NO_COLOR, reduced motion).
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from pythinker_core.message import ToolCall
from rich.console import Console

from pythinker_code.ui.shell.components import render_diff
from pythinker_code.ui.shell.components.markdown import pythinker_markdown
from pythinker_code.ui.shell.components.render_utils import cell_width
from pythinker_code.ui.shell.motion import ActivitySnapshot, activity_status_line
from pythinker_code.ui.shell.tool_renderers import (
    clear_tool_renderers,
    register_builtin_renderers,
)
from pythinker_code.ui.shell.visualize import _LiveView
from pythinker_code.wire.types import (
    StatusUpdate,
    SubagentEvent,
    ToolExecutionStarted,
    TurnBegin,
)
from pythinker_code.wire.types import (
    ToolCall as WireToolCall,
)

WIDTHS = [40, 80, 120]
_DIFF = "  10 import time\n+ 11 import asyncio\n- 13 old = 1\n+ 13 new = 2"
_BOX_CHARS = set("╭╮╰╯│─┌┐└┘├┤┬┴┼")


def _render(renderable, *, width: int, no_color: bool) -> str:
    console = Console(width=width, record=True, highlight=False, no_color=no_color)
    console.print(renderable)
    return console.export_text()


@pytest.fixture
def _run_agents_renderer_registry() -> Generator[None, None, None]:
    clear_tool_renderers()
    register_builtin_renderers()
    yield
    clear_tool_renderers()


def _render_run_agents_live(*, width: int, no_color: bool) -> str:
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(
        WireToolCall(
            id="run-agents-1",
            function=WireToolCall.FunctionBody(
                name="RunAgents",
                arguments=(
                    '{"summary":"scan","agents":['
                    '{"title":"Find TODO comments","subagent_type":"explore","prompt":"grep secret"}'
                    "]}"
                ),
            ),
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="agent-1",
            subagent_type="explore",
            description="Find TODO comments",
            event=ToolCall(
                id="nested-1",
                function=ToolCall.FunctionBody(name="Grep", arguments='{"pattern":"TODO"}'),
            ),
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="agent-1",
            subagent_type="explore",
            description="Find TODO comments",
            event=ToolExecutionStarted(tool_call_id="nested-1"),
        )
    )
    return _render(view.compose(), width=width, no_color=no_color)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("no_color", [False, True])
def test_inline_diff_renders_boxless_across_configs(width: int, no_color: bool) -> None:
    out = _render(render_diff(_DIFF), width=width, no_color=no_color)
    assert out.strip()
    assert not (_BOX_CHARS & set(out)), "inline diff must stay boxless (the screenshot look)"
    assert "+" in out and "-" in out  # markers survive


@pytest.mark.parametrize("width", WIDTHS)
def test_inline_diff_content_stable_under_color_toggle(width: int) -> None:
    colored = _render(render_diff(_DIFF), width=width, no_color=False)
    plain = _render(render_diff(_DIFF), width=width, no_color=True)
    assert colored == plain  # NO_COLOR changes styling, never content


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("no_color", [False, True])
def test_markdown_renders_across_configs(width: int, no_color: bool) -> None:
    md = pythinker_markdown("# Title\n## Section\n\n- item one\n- `code`\n\n> quote\n")
    out = _render(md, width=width, no_color=no_color)
    assert "Title" in out and "Section" in out and "item one" in out


@pytest.mark.parametrize("width", WIDTHS)
def test_markdown_code_fences_use_aligned_panel_frame(width: int) -> None:
    out = _render(
        pythinker_markdown("```bash\npythinker mcp list\n```"),
        width=width,
        no_color=True,
    )
    lines = [line for line in out.splitlines() if line]

    assert lines[0].startswith("╭─ bash ")
    assert lines[0].endswith("╮")
    assert lines[1].startswith("│ pythinker mcp list")
    assert lines[1].endswith("│")
    assert lines[2].startswith("╰")
    assert lines[2].endswith("╯")


@pytest.mark.parametrize("width", WIDTHS)
def test_activity_line_reduced_motion_uses_static_glyph(width: int) -> None:
    snap = ActivitySnapshot(label="Working", elapsed_s=3.0, reduced_motion=True)
    out = _render(activity_status_line(snap, width=width), width=width, no_color=True)
    assert "●" in out  # static text bullet, not an animated braille frame
    assert "Working" in out


@pytest.mark.parametrize("width", WIDTHS)
def test_activity_line_full_motion_uses_braille_frame(width: int) -> None:
    snap = ActivitySnapshot(label="Working", elapsed_s=0.0, reduced_motion=False)
    out = _render(activity_status_line(snap, width=width), width=width, no_color=True)
    assert "●" not in out
    assert any(frame in out for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("no_color", [False, True])
def test_run_agents_activity_tree_renders_across_configs(
    _run_agents_renderer_registry: None, width: int, no_color: bool
) -> None:
    out = _render_run_agents_live(width=width, no_color=no_color)
    assert out.count("Agents") == 1
    assert "RunAgents(" not in out
    assert "Explore" in out
    assert out.count("Find TODO comments") == 1
    assert "running" in out
    assert "searching…" in out
    assert "├─" in out or "└─" in out
    assert "⎿" in out
    assert "agent searching" not in out
    assert "grep secret" not in out
    for line in out.splitlines():
        assert cell_width(line) <= width


@pytest.mark.parametrize("width", WIDTHS)
def test_run_agents_reduced_motion_uses_static_running_glyph(
    _run_agents_renderer_registry: None, monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    monkeypatch.setenv("PYTHINKER_REDUCED_MOTION", "1")
    out = _render_run_agents_live(width=width, no_color=True)
    assert "●" in out
    assert "running" in out
    assert not any(frame in out for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


@pytest.mark.parametrize("width", WIDTHS)
def test_run_agents_full_motion_uses_animated_running_glyph(
    _run_agents_renderer_registry: None, monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    monkeypatch.delenv("PYTHINKER_REDUCED_MOTION", raising=False)
    out = _render_run_agents_live(width=width, no_color=True)
    assert "running" in out
    assert any(frame in out for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
