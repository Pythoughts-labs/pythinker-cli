from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pythinker_core.tooling import BriefDisplayBlock, DisplayBlock
from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.style import Style, StyleType
from rich.text import Text

from pythinker_code.tools.display import (
    BackgroundTaskDisplayBlock,
    DiffDisplayBlock,
    TodoDisplayBlock,
)
from pythinker_code.ui.shell.components.markdown import PythinkerMarkdown as Markdown
from pythinker_code.ui.shell.components.render_utils import sanitize_ansi
from pythinker_code.ui.shell.design_system import ShellTone, StatusName, shell_style, status_icon
from pythinker_code.ui.shell.glyphs import TRANSCRIPT_ACTIVE_MARKER
from pythinker_code.ui.shell.motion import blink_visible
from pythinker_code.ui.shell.spacing import REPORT_PANEL_PADDING, WORKLOG_PANEL_PADDING
from pythinker_code.ui.theme import get_tui_tokens, tui_rich_style
from pythinker_code.ui.theme.spec import TUI_TOKEN_NAMES
from pythinker_code.utils.rich.columns import BulletColumns
from pythinker_code.utils.rich.diff_render import (
    collect_diff_hunks,
    render_diff_preview,
    render_diff_summary_panel,
)


class WorkLogState(Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class ToolStyle:
    label: str
    icon: str
    style: str


_TOOL_STYLES: dict[str, ToolStyle] = {
    "Read": ToolStyle("Read", "->", "accent"),
    "ReadFile": ToolStyle("Read", "->", "accent"),
    "Grep": ToolStyle("Search", "*", "accent"),
    "Glob": ToolStyle("Find", "*", "accent"),
    "Edit": ToolStyle("Edit", "<-", "accent"),
    "Replace": ToolStyle("Edit", "<-", "accent"),
    "StrReplaceFile": ToolStyle("Edit", "<-", "accent"),
    "Write": ToolStyle("Write", "<-", "accent"),
    "WriteFile": ToolStyle("Write", "<-", "accent"),
    "ApplyPatch": ToolStyle("Patch", "◆", "accent"),
    "Bash": ToolStyle("Shell", "$", "success"),
    "Shell": ToolStyle("Shell", "$", "success"),
    "SetTodoList": ToolStyle("Todo", "☑", "warning"),
    "TodoWrite": ToolStyle("Todo", "☑", "warning"),
    "Agent": ToolStyle("Subagent", TRANSCRIPT_ACTIVE_MARKER, "muted"),
    "RunAgents": ToolStyle("Subagents", TRANSCRIPT_ACTIVE_MARKER, "muted"),
    "Task": ToolStyle("Subagent", TRANSCRIPT_ACTIVE_MARKER, "muted"),
    "AskUser": ToolStyle("Ask", "?", "warning"),
    "AskUserQuestion": ToolStyle("Ask", "?", "warning"),
    "FetchURL": ToolStyle("Fetch", "%", "accent"),
    "WebFetch": ToolStyle("Fetch", "%", "accent"),
    "WebSearch": ToolStyle("Search", "◈", "accent"),
    "SearchWeb": ToolStyle("Search", "◈", "accent"),
    "TaskList": ToolStyle("Tasks", "☷", "accent"),
    "TaskOutput": ToolStyle("TaskOutput", "☷", "accent"),
    "TaskStop": ToolStyle("TaskStop", "■", "warning"),
    "ToolSearch": ToolStyle("Tools", "◇", "accent"),
    "ReadSkill": ToolStyle("Skill", "◇", "accent"),
    "Skill": ToolStyle("Skill", "◇", "accent"),
}


_STATE_STYLE = {
    WorkLogState.RUNNING: shell_style(ShellTone.ACCENT),
    WorkLogState.COMPLETED: shell_style(ShellTone.MUTED),
    WorkLogState.FAILED: shell_style(ShellTone.ERROR),
    WorkLogState.DENIED: shell_style(ShellTone.MUTED) + Style(strike=True),
    WorkLogState.INTERRUPTED: shell_style(ShellTone.WARNING),
}

_STATE_ICON: dict[WorkLogState, StatusName] = {
    WorkLogState.RUNNING: "running",
    WorkLogState.COMPLETED: "completed",
    WorkLogState.FAILED: "failed",
    WorkLogState.DENIED: "denied",
    WorkLogState.INTERRUPTED: "interrupted",
}


def _state_icon(state: WorkLogState) -> Text:
    icon = status_icon(_STATE_ICON[state])
    if state == WorkLogState.RUNNING and not blink_visible():
        return Text(" ", style=icon.style)
    return icon


def _tool_token_style(token_name: str) -> str:
    if token_name not in TUI_TOKEN_NAMES:
        raise ValueError(f"Unknown TUI token: {token_name!r}")
    tokens = get_tui_tokens()
    return getattr(tokens, token_name)


def tool_style(name: str) -> ToolStyle:
    style = _TOOL_STYLES.get(name, ToolStyle(name, "⚙", "accent"))
    return ToolStyle(style.label, style.icon, _tool_token_style(style.style))


def denied_error(message: str) -> bool:
    lowered = message.lower().strip()
    if lowered == "denied" or lowered.startswith("denied:"):
        return True
    return any(
        needle in lowered
        for needle in (
            "questionrejectederror",
            "rejected permission",
            "specified a rule",
            "user dismissed",
            "tool calls are disabled",
        )
    )


def render_worklog_entry(
    *,
    label: str,
    target: str | None = None,
    state: WorkLogState,
    detail: str | None = None,
    icon_renderable: RenderableType | None = None,
    children: list[RenderableType] | None = None,
) -> RenderableType:
    line = Text()
    if icon_renderable is None:
        line.append_text(_state_icon(state))
        line.append(" ")
    line.append(sanitize_ansi(label), style="bold")
    if target:
        line.append(" ")
        line.append(sanitize_ansi(target), style=tui_rich_style("muted"))
    line.append(" ")
    line.append(state.value, style=_STATE_STYLE[state])
    if detail:
        line.append(" · ", style=tui_rich_style("muted"))
        line.append(sanitize_ansi(detail), style=_STATE_STYLE[state])
    if icon_renderable is not None:
        return BulletColumns(
            line if not children else Group(line, *children),
            bullet=icon_renderable,
        )
    if not children:
        return line
    return Group(line, *children)


def render_worklog_card(
    title: str,
    body: RenderableType,
    *,
    subtitle: str | None = None,
    border_style: StyleType = "grey39",
    padding: tuple[int, int] = WORKLOG_PANEL_PADDING,
) -> Panel:
    return Panel(
        body,
        title=title,
        title_align="left",
        subtitle=subtitle,
        subtitle_align="left",
        border_style=border_style,
        box=box.ROUNDED,
        padding=padding,
        expand=False,
    )


def render_display_blocks(
    display: list[DisplayBlock], *, is_error: bool = False
) -> list[RenderableType]:
    rendered: list[RenderableType] = []
    idx = 0
    while idx < len(display):
        block = display[idx]
        if isinstance(block, DiffDisplayBlock):
            path = block.path
            diff_blocks: list[DiffDisplayBlock] = []
            while idx < len(display):
                candidate = display[idx]
                if not isinstance(candidate, DiffDisplayBlock) or candidate.path != path:
                    break
                diff_blocks.append(candidate)
                idx += 1
            if any(item.is_summary for item in diff_blocks):
                rendered.append(
                    render_worklog_card(
                        "Diff", render_diff_summary_panel(path, diff_blocks), subtitle=path
                    )
                )
                continue
            hunks, added_total, removed_total = collect_diff_hunks(diff_blocks)
            if hunks:
                preview_lines, _ = render_diff_preview(
                    path,
                    hunks,
                    added_total,
                    removed_total,
                    max_lines=8,
                )
                rendered.append(
                    render_worklog_card(
                        "Diff",
                        Group(*preview_lines),
                    )
                )
            continue
        if isinstance(block, BriefDisplayBlock):
            text = block.text.strip()
            if text:
                title = "Error" if is_error else "Report"
                style = tui_rich_style("error") if is_error else tui_rich_style("muted")
                # Error briefs carry a trailing stderr tail (the failing command's
                # last lines). Rendering that as Markdown reflows newlines and eats
                # shell metacharacters (backticks, '#', '*'), so render errors as
                # plain text and reserve Markdown for authored "Report" briefs.
                body: RenderableType = (
                    Text(text, style=style) if is_error else Markdown(text, style=style)
                )
                if "\n" in text or len(text) > 100:
                    rendered.append(
                        render_worklog_card(
                            title,
                            body,
                            border_style=tui_rich_style("error")
                            if is_error
                            else tui_rich_style("dim"),
                            padding=REPORT_PANEL_PADDING,
                        )
                    )
                else:
                    rendered.append(body)
            idx += 1
            continue
        if isinstance(block, TodoDisplayBlock):
            body = Text()
            for index, todo in enumerate(block.items):
                if index:
                    body.append("\n")
                match todo.status:
                    case "done":
                        body.append("✓", style=tui_rich_style("muted"))
                        body.append(" ")
                        body.append(todo.title, style=tui_rich_style("muted") + Style(strike=True))
                    case "in_progress":
                        body.append("→", style=tui_rich_style("activity_verb"))
                        body.append(" ")
                        body.append(
                            todo.title, style=tui_rich_style("activity_label") + Style(bold=True)
                        )
                    case _:
                        body.append("·", style=tui_rich_style("muted"))
                        body.append(" ")
                        body.append(todo.title, style=tui_rich_style("muted"))
            rendered.append(render_worklog_card("Todos", body))
            idx += 1
            continue
        if isinstance(block, BackgroundTaskDisplayBlock):
            rendered.append(
                render_worklog_card(
                    "Background task",
                    Text(
                        f"{block.task_id} [{block.status}] {block.kind}: {block.description}",
                        style=tui_rich_style("muted"),
                    ),
                )
            )
            idx += 1
            continue
        idx += 1
    return rendered
