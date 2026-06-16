"""Rich markdown element overrides for Pythinker reports."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.padding import Padding
from rich.panel import Panel
from rich.style import Style as RichStyle
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from pythinker_code.ui.shell.render_constants import MAX_HIGHLIGHT_BYTES, MAX_HIGHLIGHT_LINES
from pythinker_code.ui.shell.spacing import CODE_BLOCK_PADDING, blank_row
from pythinker_code.ui.theme import get_markdown_colors
from pythinker_code.utils.rich.markdown import CodeBlock, TableElement

if TYPE_CHECKING:
    pass

_PRIORITY_MATRIX_ROW_RE = re.compile(
    r"^\s*(?P<id>[A-Z]{1,3}\d+)\s*(?:[─━—-]|\s){2,}\s*"
    r"(?P<severity>CRITICAL|HIGH|MEDIUM|LOW|INFO)\s*$",
    re.IGNORECASE,
)
_PRIORITY_MATRIX_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")


def _priority_matrix_rows(code_text: str) -> list[tuple[str, str]] | None:
    rows: list[tuple[str, str]] = []
    meaningful_lines = 0
    for line in code_text.splitlines():
        stripped = line.strip()
        if not stripped or set(stripped) <= {"─", "━", "—", "-", " "}:
            continue
        meaningful_lines += 1
        match = _PRIORITY_MATRIX_ROW_RE.match(stripped)
        if match is None:
            return None
        rows.append((match.group("id"), match.group("severity").upper()))
    if len(rows) < 3 or meaningful_lines != len(rows):
        return None
    return rows


def _render_priority_matrix(rows: list[tuple[str, str]]) -> Table:
    grouped: dict[str, list[str]] = {severity: [] for severity in _PRIORITY_MATRIX_SEVERITIES}
    for item_id, severity in rows:
        grouped.setdefault(severity, []).append(item_id)

    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", no_wrap=True)
    table.add_column(no_wrap=False)
    for severity in _PRIORITY_MATRIX_SEVERITIES:
        items = grouped.get(severity) or []
        if not items:
            continue
        table.add_row(Text(severity.title(), style="bold"), "  ".join(items))
    return table


class ReportTableElement(TableElement):
    """Markdown tables that stay readable in long reports."""

    def _header_cells(self) -> list[Text]:
        if self.header is None or self.header.row is None:
            return []
        return [cell.content for cell in self.header.row.cells]

    def _body_rows(self) -> list[list[Text]]:
        if self.body is None:
            return []
        return [[cell.content for cell in row.cells] for row in self.body.rows]

    def _should_stack(self, options: ConsoleOptions) -> bool:
        headers = self._header_cells()
        rows = self._body_rows()
        column_count = len(headers)
        if column_count <= 2 or not rows:
            return False

        column_widths = [len(header.plain.strip()) for header in headers]
        for row in rows:
            for index, cell in enumerate(row[:column_count]):
                column_widths[index] = max(column_widths[index], len(cell.plain.strip()))
        longest_cell = max(column_widths, default=0)
        estimated_grid_width = sum(min(width, 24) for width in column_widths) + column_count * 3 + 1
        available_width = options.max_width or 80

        if column_count >= 4:
            return longest_cell >= 24 or estimated_grid_width > available_width
        return longest_cell >= 36

    def _render_stacked(self) -> RenderResult:
        headers = self._header_cells()
        rows = self._body_rows()
        detail_headers = headers[1:]
        label_width = min(
            max((len(header.plain.strip()) for header in detail_headers), default=0),
            22,
        )

        for index, row in enumerate(rows):
            if index:
                yield blank_row()

            title = Text("• ", style="markdown.item.bullet")
            if row:
                title_value = row[0].copy()
                title.append_text(title_value)
                title.stylize("markdown.strong", 2, len(title))
            detail_grid = Table.grid(expand=True, padding=(0, 2))
            detail_grid.add_column(width=max(1, label_width), no_wrap=True)
            detail_grid.add_column(ratio=1, overflow="fold")

            has_details = False
            for header, cell in zip(detail_headers, row[1:], strict=False):
                label = header.plain.strip()
                value = cell.copy()
                if not value.plain.strip():
                    value = Text("—", style="markdown.block_quote")
                detail_grid.add_row(Text(label, style="markdown.strong"), value)
                has_details = True

            if has_details:
                yield Group(title, Padding(detail_grid, (0, 0, 0, 2)))
            else:
                yield title

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if self._should_stack(options):
            yield from self._render_stacked()
            return
        yield from super().__rich_console__(console, options)


class BorderedCodeBlock(CodeBlock):
    """Code block with an aligned rounded frame and calm report styling."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        code_text = str(self.text).rstrip("\n")
        if self.lexer_name.strip().lower() in {"", "text", "plain", "markdown"}:
            matrix_rows = _priority_matrix_rows(code_text)
            if matrix_rows is not None:
                yield blank_row()
                yield _render_priority_matrix(matrix_rows)
                yield blank_row()
                return

        colors = get_markdown_colors()
        border_style = RichStyle(color=colors.code_block_border, bold=True)
        if isinstance(self.theme, str):
            panel_style = Syntax.get_theme(self.theme).get_background_style()
            syntax_bg: str | None = None
        else:
            panel_style = (
                RichStyle(bgcolor=colors.code_block_bg) if colors.code_block_bg else RichStyle()
            )
            syntax_bg = "default"

        lexer_name = self.lexer_name.strip()
        title = lexer_name if lexer_name and lexer_name != "text" else None
        line_count = code_text.count("\n") + 1
        if line_count > MAX_HIGHLIGHT_LINES or len(code_text) > MAX_HIGHLIGHT_BYTES:
            highlighted = Text(code_text)
            skip_notice = f"highlighting skipped ({line_count:,} lines)"
            title = f"{title} · {skip_notice}" if title else skip_notice
        else:
            syntax = Syntax(
                code_text,
                self.lexer_name,
                theme=self.theme,
                word_wrap=True,
                padding=0,
                background_color=syntax_bg,
            )
            highlighted = syntax.highlight(code_text)
            highlighted.rstrip()
        yield blank_row()
        yield Panel(
            highlighted,
            title=title,
            title_align="left",
            box=box.ROUNDED,
            border_style=border_style,
            padding=CODE_BLOCK_PADDING,
            expand=True,
            style=panel_style,
        )
        yield blank_row()


# Backward-compatible aliases for tests importing private names.
_ReportTableElement = ReportTableElement
_BorderedCodeBlock = BorderedCodeBlock
