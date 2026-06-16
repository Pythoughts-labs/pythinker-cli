"""Pythinker Rich markdown renderer."""

from __future__ import annotations

from typing import Any, Literal

from rich.console import Console, ConsoleOptions, RenderResult
from rich.style import Style as RichStyle
from rich.theme import Theme

from pythinker_code.ui.shell.markdown.audit import detect_audit_report
from pythinker_code.ui.shell.markdown.elements import BorderedCodeBlock, ReportTableElement
from pythinker_code.ui.shell.markdown.normalizers import normalize_model_markdown
from pythinker_code.ui.theme import ThemeName
from pythinker_code.ui.theme.adapters.markdown import markdown_style_overrides
from pythinker_code.utils.rich.markdown import Markdown

ReportKind = Literal["default", "audit"]


def _markdown_style_overrides(theme: ThemeName | None = None) -> dict[str, RichStyle]:
    return markdown_style_overrides(theme)


class PythinkerMarkdown(Markdown):
    """Drop-in replacement for ``rich.markdown.Markdown`` with the Pythinker palette."""

    elements = {
        **Markdown.elements,
        "fence": BorderedCodeBlock,
        "code_block": BorderedCodeBlock,
        "table_open": ReportTableElement,
    }

    def __init__(
        self,
        markup: str,
        *args: Any,
        report: bool = False,
        audit: bool = False,
        **kwargs: Any,
    ) -> None:
        self._report_mode = report
        use_audit = audit or (report and detect_audit_report(markup))
        normalized = normalize_model_markdown(markup, report=report, audit=use_audit)
        assert isinstance(normalized, str)
        super().__init__(normalized, *args, **kwargs)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        from pythinker_code.ui.theme.adapters.markdown import report_markdown_style_overrides

        overrides = (
            report_markdown_style_overrides() if self._report_mode else _markdown_style_overrides()
        )
        with console.use_theme(Theme(overrides, inherit=True)):
            yield from super().__rich_console__(console, options)


def pythinker_markdown(
    text: str,
    *,
    code_theme: str | None = None,
    audit: bool = False,
) -> PythinkerMarkdown:
    """Build a :class:`PythinkerMarkdown` with the palette pre-wired."""
    return PythinkerMarkdown(text, code_theme=code_theme, audit=audit)


def pythinker_report_markdown(
    text: str,
    *,
    code_theme: str | None = None,
    style: str | RichStyle = "none",
    report_kind: ReportKind = "default",
) -> PythinkerMarkdown:
    """Report-body markdown: only H1 headings render bold."""
    audit = report_kind == "audit"
    return PythinkerMarkdown(
        text,
        code_theme=code_theme,
        style=style,
        report=True,
        audit=audit,
    )
