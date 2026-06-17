"""Width-aware horizontal border primitive for shell components.

Width-aware horizontal rule for shell cards: reflows to the available terminal
width and uses a semantic Pythinker theme token for its color.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text

from pythinker_code.ui.theme import tui_rich_style

__all__ = ["DynamicBorder", "render_dynamic_border"]


@dataclass(frozen=True, slots=True)
class DynamicBorder:
    """Renderable horizontal border that fills the current console width."""

    token: str = "border"
    glyph: str = "─"

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        _ = console
        width = max(1, options.max_width)
        glyph = self.glyph or "─"
        line = (glyph * width)[:width]
        yield Text(line, style=tui_rich_style(self.token))


def render_dynamic_border(*, token: str = "border", glyph: str = "─") -> DynamicBorder:
    """Return a :class:`DynamicBorder` renderable."""
    return DynamicBorder(token=token, glyph=glyph)
