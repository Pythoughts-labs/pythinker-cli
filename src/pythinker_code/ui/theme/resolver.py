"""Central style resolution — the only place that understands theme + terminal."""

from __future__ import annotations

from rich.style import Style as RichStyle

from .capabilities import TerminalCapabilities, get_terminal_capabilities
from .spec import (
    TUI_TOKEN_NAMES,
    BrandToken,
    CoreToken,
    MarkdownAnsiToken,
    PromptToken,
    ThemeSpec,
)


class StyleResolver:
    """Resolve semantic tokens to Rich / prompt_toolkit style fragments."""

    def __init__(
        self,
        theme: ThemeSpec,
        capabilities: TerminalCapabilities | None = None,
    ) -> None:
        self.theme = theme
        self.capabilities = capabilities or get_terminal_capabilities()

    def core_hex(self, token: CoreToken | str) -> str:
        key = token.value if isinstance(token, CoreToken) else token
        if key not in TUI_TOKEN_NAMES:
            msg = f"Unknown core token {key!r}"
            raise ValueError(msg)
        return getattr(self.theme.tokens, key)

    def prompt_hex(self, token: PromptToken) -> str:
        return self.theme.prompt[token]

    def brand_hex(self, token: BrandToken) -> str:
        return self.theme.brand[token]

    def color(
        self,
        token: CoreToken | PromptToken | BrandToken | MarkdownAnsiToken | str,
    ) -> str:
        if not self.capabilities.color_enabled:
            return ""
        if isinstance(token, CoreToken):
            return self.core_hex(token)
        if isinstance(token, PromptToken):
            return self.prompt_hex(token)
        if isinstance(token, BrandToken):
            return self.brand_hex(token)
        if isinstance(token, MarkdownAnsiToken):
            return self.theme.markdown_ansi[token]
        if token in TUI_TOKEN_NAMES:
            return self.core_hex(token)
        return token

    def rich_style(
        self,
        token: CoreToken | str,
        *,
        bold: bool = False,
        italic: bool = False,
        underline: bool = False,
        bgcolor: CoreToken | str | None = None,
    ) -> RichStyle:
        if not self.capabilities.color_enabled:
            return RichStyle(bold=bold, italic=italic, underline=underline)
        fg = self.color(token)
        bg = self.color(bgcolor) if bgcolor is not None else None
        key = token.value if isinstance(token, CoreToken) else token
        if key.endswith("_bg"):
            return RichStyle(bgcolor=fg or None, bold=bold, italic=italic, underline=underline)
        if bg and fg:
            return RichStyle(
                color=fg,
                bgcolor=bg,
                bold=bold,
                italic=italic,
                underline=underline,
            )
        if bg:
            return RichStyle(bgcolor=bg, bold=bold, italic=italic, underline=underline)
        if fg:
            return RichStyle(color=fg, bold=bold, italic=italic, underline=underline)
        return RichStyle(bold=bold, italic=italic, underline=underline)

    def ptk_fg(self, token: PromptToken | CoreToken) -> str:
        color = self.color(token)
        return f"fg:{color}" if color else ""

    def markdown_inline_code_style(self) -> RichStyle:
        """Inline code: accent color only — no bold (headers stay bold elsewhere)."""
        style = self.rich_style(CoreToken.ACCENT)
        return RichStyle(color=style.color, bold=False)

    def markdown_heading_style(self, *, level: int = 1) -> RichStyle:
        style = self.rich_style(CoreToken.TOOL_TITLE, bold=True)
        if level == 2:
            return RichStyle(color=style.color, bold=True, underline=True)
        if level == 4:
            return RichStyle(color=style.color, bold=True, dim=True)
        return style
