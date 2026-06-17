"""Rich markdown style overrides derived from the active theme."""

from __future__ import annotations

from rich.style import Style as RichStyle

from ..registry import get_markdown_colors
from ..spec import ThemeName


def markdown_style_overrides(theme: ThemeName | None = None) -> dict[str, RichStyle]:
    colors = get_markdown_colors(theme)
    return {
        "markdown.h1": RichStyle(color=colors.heading, bold=True),
        "markdown.h1.border": RichStyle(color=colors.heading),
        "markdown.h1.underline": RichStyle(color=colors.heading),
        "markdown.h2": RichStyle(color=colors.heading, bold=True, underline=True),
        "markdown.h3": RichStyle(color=colors.heading, bold=True),
        "markdown.h4": RichStyle(color=colors.heading, bold=True, dim=True),
        "markdown.strong": RichStyle(color=colors.strong, bold=True),
        "markdown.em": RichStyle(color=colors.emphasis, italic=True),
        "markdown.emph": RichStyle(color=colors.emphasis, italic=True),
        "markdown.code": RichStyle(color=colors.inline_code, bold=False),
        "markdown.link": RichStyle(color=colors.link, underline=True),
        "markdown.link_url": RichStyle(color=colors.link, underline=True, dim=True),
        "markdown.block_quote": RichStyle(color=colors.quote, italic=True),
        "markdown.hr": RichStyle(color=colors.code_block_border),
        "markdown.code_block": RichStyle(color=colors.inline_code, bold=False),
        "markdown.code_block.border": RichStyle(color=colors.code_block_border, bold=True),
        "markdown.item.bullet": RichStyle(color=colors.unordered_marker, bold=True),
        "markdown.item.number": RichStyle(color=colors.ordered_marker, bold=True),
    }


def report_markdown_style_overrides(theme: ThemeName | None = None) -> dict[str, RichStyle]:
    """Report body palette: only ``markdown.h1`` stays bold; all other roles are regular weight."""
    overrides = markdown_style_overrides(theme)
    report: dict[str, RichStyle] = {}
    for name, style in overrides.items():
        if name == "markdown.h1":
            report[name] = style
            continue
        report[name] = RichStyle(
            color=style.color,
            bgcolor=style.bgcolor,
            bold=False,
            italic=style.italic,
            underline=style.underline,
            dim=style.dim,
        )
    return report
