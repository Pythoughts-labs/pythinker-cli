"""Shared shell dialog chrome for approvals, questions, and modal-like panels."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.style import StyleType
from rich.text import Text

from pythinker_code.ui.shell.design_system import dialog_title
from pythinker_code.ui.shell.spacing import DIALOG_PANEL_PADDING, blank_row
from pythinker_code.ui.theme import tui_rich_style

DialogKind = Literal["approval", "question", "warning", "info"]


@dataclass(frozen=True, slots=True)
class DialogOption:
    label: str
    selected: bool = False
    key: str | None = None
    description: str | None = None


def _render_option(option: DialogOption) -> Text:
    prefix = "→" if option.selected else " "
    key = f"[{option.key}] " if option.key else ""
    style = tui_rich_style("accent") if option.selected else tui_rich_style("muted")
    text = Text(f"{prefix} {key}{option.label}", style=style)
    if option.description:
        text.append(f"  {option.description}", style="dim")
    return text


def render_dialog(
    *,
    kind: DialogKind,
    title: str,
    body: list[RenderableType],
    options: list[DialogOption],
    footer: RenderableType | None = None,
    border_style: StyleType | None = None,
    width: int | None = None,
) -> RenderableType:
    if border_style is None:
        border_style = tui_rich_style("border_muted")
    lines: list[RenderableType] = []
    lines.extend(body)
    if body and options:
        lines.append(blank_row())
    lines.extend(_render_option(option) for option in options)
    if footer is not None:
        lines.append(blank_row())
        lines.append(footer)
    return Panel(
        Group(*lines),
        title=dialog_title("approval" if kind == "approval" else "question", title),
        title_align="left",
        border_style=border_style,
        box=box.ROUNDED,
        padding=DIALOG_PANEL_PADDING,
        width=width,
    )
