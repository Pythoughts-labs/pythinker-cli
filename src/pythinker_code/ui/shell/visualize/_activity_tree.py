"""Compact active agent and task rows."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from rich.console import Group, RenderableType
from rich.text import Text

from pythinker_code.ui.shell.components.render_utils import cell_width, truncate_to_width
from pythinker_code.ui.shell.design_system import ShellTone, shell_style, status_icon
from pythinker_code.ui.shell.motion import blink_visible

ActivityState = Literal["running", "completed", "failed", "waiting", "denied", "interrupted"]


@dataclass(frozen=True, slots=True)
class ActivityRow:
    label: str
    detail: str
    state: ActivityState = "running"
    identity: str | None = None


def _row_marker(state: ActivityState, now: float) -> Text:
    """Status marker for an activity row, pulsing the running dot in step with
    the rest of the shell (`loading_marker`/`_worklog`) on a 0.8s cadence."""
    icon = status_icon(state)
    if state == "running" and not blink_visible(now):
        return Text(" ", style=icon.style)
    return icon


def render_activity_tree(
    rows: list[ActivityRow], *, width: int, max_rows: int = 4, now: float | None = None
) -> RenderableType:
    clock = time.monotonic() if now is None else now
    rendered: list[RenderableType] = []
    visible = rows[-max_rows:]
    hidden = max(0, len(rows) - len(visible))
    for index, row in enumerate(visible):
        branch = "└─" if index == len(visible) - 1 else "├─"
        label = row.label if row.identity is None else f"{row.label} {row.identity}"
        prefix = f"{branch} {label} "
        available = max(1, width - cell_width(prefix) - 4)
        text = Text()
        text.append_text(_row_marker(row.state, clock))
        text.append(" ")
        text.append(prefix, style=shell_style(ShellTone.MUTED))
        detail = truncate_to_width(row.detail, available)
        text.append(detail, style=shell_style(ShellTone.MUTED))
        rendered.append(text)
    if hidden:
        rendered.insert(
            0,
            Text(f"… {hidden} older agent activities hidden", style=shell_style(ShellTone.MUTED)),
        )
    return Group(*rendered)
