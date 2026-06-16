"""Pythinker markdown renderer with bordered code blocks and themed accents.

Wraps Rich's ``Markdown`` element with three changes:

1. Fenced code blocks are framed by ``╭─ {lang}`` / ``╰─`` rules in the
   markdown palette's ``code_block_border`` color and tinted with the
   ``code_block_bg`` background.
2. Inline elements (headings, strong, emphasis, links, inline code, block
   quotes) resolve against ``pythinker_code.ui.theme.get_markdown_colors``
   so dark/light themes share the same renderer.
3. The public function :func:`markdown_commit_boundary` returns the safe
   commit offset for streamed markdown; the production rendering path in
   ``_ContentBlock._flush_committed`` calls it directly.  The lower-level
   :class:`PythinkerMarkdownStream` class is an internal/testing helper and
   is not part of the public API.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from markdown_it import MarkdownIt

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.padding import Padding
from rich.panel import Panel
from rich.style import Style as RichStyle
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from pythinker_code.ui.shell.components.render_utils import sanitize_ansi
from pythinker_code.ui.shell.glyphs import TRANSCRIPT_ASSISTANT_MARKER
from pythinker_code.ui.shell.render_constants import MAX_HIGHLIGHT_BYTES, MAX_HIGHLIGHT_LINES
from pythinker_code.ui.shell.spacing import CODE_BLOCK_PADDING, blank_row
from pythinker_code.ui.theme import ThemeName, get_markdown_colors
from pythinker_code.ui.theme.adapters.markdown import markdown_style_overrides
from pythinker_code.utils.rich.markdown import CodeBlock, Markdown, TableElement

_MARKDOWN_ICON_REPLACEMENTS: dict[str, str] = {
    # Model text mimicking the CLI transcript keeps the row-marker look; on
    # platforms where U+23FA degrades (Windows emoji tile, ASCII mode) it
    # normalizes to that platform's marker glyph.
    "⏺": TRANSCRIPT_ASSISTANT_MARKER,
    "✅": "✓",
    "☑️": "✓",
    "☑": "✓",
    "✔️": "✓",
    "✔": "✓",
    "❌": "×",
    "✖️": "×",
    "✖": "×",
    "🚫": "×",
    "⚠️": "!",
    "⚠": "!",
    "🔴": "●",
    "🟠": "●",
    "🟡": "●",
    "🟢": "●",
    "🔵": "●",
    "🟣": "●",
    "⚫": "●",
    "⚪": "○",
    "🔍": "⌕",
    "🔎": "⌕",
    "📋": "▣",
    "📝": "▣",
    "📌": "•",
}
_MARKDOWN_ICON_KEYS: tuple[str, ...] = tuple(
    sorted(_MARKDOWN_ICON_REPLACEMENTS, key=len, reverse=True)
)
_FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})")
_OL_ITEM_RE = re.compile(r"^\d+\.\s")
_PRIORITY_MATRIX_ROW_RE = re.compile(
    r"^\s*(?P<id>[A-Z]{1,3}\d+)\s*(?:[─━—-]|\s){2,}\s*"
    r"(?P<severity>CRITICAL|HIGH|MEDIUM|LOW|INFO)\s*$",
    re.IGNORECASE,
)
_PRIORITY_MATRIX_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
# A GFM table delimiter run, e.g. ``|---|:--:|---|``. Two or more dashes per
# cell keeps stray inline ``|-|`` out of the match.
_DELIM_RUN_RE = re.compile(r"\|(?:\s*:?-{2,}:?\s*\|)+")
# A header line: optional prose prefix, then a trailing run of pipe cells.
_HEADER_RE = re.compile(r"^(?P<prefix>.*?)(?P<cells>(?:\|[^\n|]*)+\|)\s*$")


__all__ = [
    "PythinkerMarkdown",
    "pythinker_markdown",
]


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


def _is_table_separator_line(line: str) -> bool:
    return _TABLE_SEPARATOR_RE.match(line) is not None


def _is_table_header_fragment(fragment: str) -> bool:
    stripped = fragment.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return False
    if _is_table_separator_line(stripped):
        return False
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    return len(cells) >= 2 and any(cells)


def _find_crammed_table_header_start(line: str) -> int | None:
    if line.lstrip().startswith("|"):
        return None
    for index, char in enumerate(line):
        if char != "|" or not line[:index].strip():
            continue
        if _is_table_header_fragment(line[index:]):
            return index
    return None


def _repair_crammed_markdown_tables(markup: str) -> str:
    """Split report headings accidentally glued to a following Markdown table.

    Streaming model output occasionally drops the newline between a section
    title and a table header, producing text such as ``Medium| # | File |``.
    Markdown then treats the whole table as a paragraph and renders dense report
    rows as crammed prose. Repair only this narrow, table-separator-confirmed
    shape, and never touch fenced code.
    """
    if "|" not in markup:
        return markup

    lines = markup.splitlines(keepends=True)
    if len(lines) < 2:
        return markup

    repaired: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        eol = line[len(body) :]
        match = _FENCE_RE.match(body)
        if in_fence:
            repaired.append(line)
            if match is not None:
                fence = match.group("fence")
                if fence.startswith(fence_char) and len(fence) >= fence_len:
                    in_fence = False
                    fence_char = ""
                    fence_len = 0
            continue
        if match is not None:
            fence = match.group("fence")
            in_fence = True
            fence_char = fence[0]
            fence_len = len(fence)
            repaired.append(line)
            continue

        split_at: int | None = None
        if index + 1 < len(lines):
            next_body = lines[index + 1].rstrip("\r\n")
            if _is_table_separator_line(next_body):
                split_at = _find_crammed_table_header_start(body)
        if split_at is None:
            repaired.append(line)
            continue

        prefix = body[:split_at].rstrip()
        header = body[split_at:].lstrip()
        if prefix:
            repaired.append(f"{prefix}\n")
        repaired.append(f"{header}{eol}")

    return "".join(repaired)


_MARKDOWN_FENCE_INFOS = frozenset({"md", "markdown"})


def _contains_markdown_table(lines: list[str]) -> bool:
    """Whether *lines* hold a pipe-table header immediately above a delimiter row."""
    previous: str | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            previous = None
            continue
        if (
            previous is not None
            and _is_table_separator_line(line)
            and _is_table_header_fragment(previous)
        ):
            return True
        previous = line
    return False


def _unwrap_fenced_markdown_tables(markup: str) -> str:
    """Unwrap ```` ```md ```` fences whose body contains a markdown table.

    Models sometimes wrap a whole markdown answer — tables included — in a
    ``md``/``markdown`` fence, which renders the table as opaque code. Apply
    a conservative heuristic: only fences explicitly tagged ``md`` or
    ``markdown`` *and* containing a header+delimiter pair are unwrapped.
    Other languages, untagged fences, md fences without tables, and unclosed
    fences pass through unchanged.
    """
    if "```" not in markup and "~~~" not in markup:
        return markup

    lines = markup.splitlines(keepends=True)
    out: list[str] = []
    in_other_fence = False
    other_char = ""
    other_len = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        body = line.rstrip("\r\n")
        match = _FENCE_RE.match(body)
        if in_other_fence:
            out.append(line)
            if match is not None:
                fence = match.group("fence")
                # CommonMark closing fences carry no info string; without that
                # check a "```python" line inside the open fence would end it.
                if (
                    fence[0] == other_char
                    and len(fence) >= other_len
                    and not body[match.end() :].strip()
                ):
                    in_other_fence = False
            i += 1
            continue
        if match is None:
            out.append(line)
            i += 1
            continue
        fence = match.group("fence")
        info = body[match.end() :].strip().lower()
        if info not in _MARKDOWN_FENCE_INFOS:
            in_other_fence = True
            other_char = fence[0]
            other_len = len(fence)
            out.append(line)
            i += 1
            continue

        # ``md`` fence: find the matching close (same char, same-or-longer
        # marker, no info string — CommonMark closing-fence rules).
        close_index: int | None = None
        for j in range(i + 1, len(lines)):
            inner_body = lines[j].rstrip("\r\n")
            inner_match = _FENCE_RE.match(inner_body)
            if (
                inner_match is not None
                and inner_match.group("fence")[0] == fence[0]
                and len(inner_match.group("fence")) >= len(fence)
                and not inner_body[inner_match.end() :].strip()
            ):
                close_index = j
                break
        if close_index is None:
            out.append(line)
            i += 1
            continue

        fenced_body = lines[i + 1 : close_index]
        if not _contains_markdown_table([raw.rstrip("\r\n") for raw in fenced_body]):
            out.extend(lines[i : close_index + 1])
            i = close_index + 1
            continue

        # Unwrap: drop the fence markers and keep the body as block-level
        # markdown, padded with blank lines so adjacent prose can't glue on.
        if out and out[-1].strip():
            out.append("\n")
        out.extend(fenced_body)
        next_line = lines[close_index + 1] if close_index + 1 < len(lines) else None
        ends_blank = bool(fenced_body) and not fenced_body[-1].strip()
        if next_line is not None and next_line.strip() and not ends_blank:
            out.append("\n")
        i = close_index + 1
    return "".join(out)


class _ReportTableElement(TableElement):
    """Markdown tables that stay readable in long reports.

    Compact, low-column tables keep the normal bordered grid. Wide report
    tables become stacked records so long paths and prose wrap in one generous
    value column instead of being sliced across many narrow grid cells.
    """

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


class _BorderedCodeBlock(CodeBlock):
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
        # ``self.theme`` is a str only for an opted-in stock Pygments style; the
        # default ANSI theme resolves to a ``SyntaxTheme`` instance. For a stock
        # style, paint the panel with the style's own background so the code and
        # its padding share one uniform dark block (Aider-style); otherwise keep
        # the calm ``code_block_bg`` tint.
        if isinstance(self.theme, str):
            # Stock opt-in style (e.g. monokai): paint the panel and code with the
            # style's own background so they form one uniform solid block.
            panel_style = Syntax.get_theme(self.theme).get_background_style()
            syntax_bg: str | None = None
        else:
            # Default path (Catppuccin adaptive / ANSI sentinel): keep the calm
            # code_block_bg tint on the panel and render the syntax transparently
            # so only its foreground colors land (the "skip background" approach).
            panel_style = (
                RichStyle(bgcolor=colors.code_block_bg) if colors.code_block_bg else RichStyle()
            )
            syntax_bg = "default"

        lexer_name = self.lexer_name.strip()
        title = lexer_name if lexer_name and lexer_name != "text" else None
        # Size guard: skip Pygments for very large blocks so a
        # pathological fence cannot stall the renderer. ``len()`` counts
        # characters (a lower bound on UTF-8 bytes), which is enough for a
        # guard heuristic without paying for an encode of the whole block.
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
        # Frame the code block with a blank row above and below so it reads as a
        # distinct section instead of crowding the surrounding prose. Canonical
        # ``blank_row()`` (an empty ``Text``) never picks up the panel's tint.
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


def _markdown_style_overrides(theme: ThemeName | None = None) -> dict[str, RichStyle]:
    """Translate the active markdown palette into Rich style names."""
    return markdown_style_overrides(theme)


def _replace_report_icons(text: str) -> str:
    """Replace large/color emoji status icons with compact monochrome glyphs."""
    if not any(icon in text for icon in _MARKDOWN_ICON_KEYS):
        return text
    out: list[str] = []
    i = 0
    inline_code_ticks = 0
    while i < len(text):
        if text[i] == "`":
            j = i
            while j < len(text) and text[j] == "`":
                j += 1
            tick_count = j - i
            out.append(text[i:j])
            if inline_code_ticks == 0:
                inline_code_ticks = tick_count
            elif inline_code_ticks == tick_count:
                inline_code_ticks = 0
            i = j
            continue
        if inline_code_ticks:
            out.append(text[i])
            i += 1
            continue
        for icon in _MARKDOWN_ICON_KEYS:
            if text.startswith(icon, i):
                out.append(_MARKDOWN_ICON_REPLACEMENTS[icon])
                i += len(icon)
                break
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _simplify_markdown_report_icons(markup: str) -> str:
    """Simplify report/status emoji outside fenced code blocks."""
    if not any(icon in markup for icon in _MARKDOWN_ICON_KEYS):
        return markup

    lines: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in markup.splitlines(keepends=True):
        match = _FENCE_RE.match(line)
        if in_fence:
            lines.append(line)
            if match is not None:
                fence = match.group("fence")
                if fence.startswith(fence_char) and len(fence) >= fence_len:
                    in_fence = False
                    fence_char = ""
                    fence_len = 0
            continue
        if match is not None:
            fence = match.group("fence")
            in_fence = True
            fence_char = fence[0]
            fence_len = len(fence)
            lines.append(line)
            continue
        lines.append(_replace_report_icons(line))
    return "".join(lines)


# Inline code-span repair for table rows: a run of N backticks, lazily-matched
# body, then the first later run containing those same N backticks. This is a
# tolerant LLM-output repair heuristic, not a complete GFM code-span parser.
_CODE_SPAN_RE = re.compile(r"(?P<ticks>`+)(?P<body>.*?)(?P=ticks)")


def _escape_code_span_pipes(text: str) -> str:
    r"""Escape raw ``|`` inside inline code spans as ``\|`` so GFM keeps the table
    cell intact (LLMs frequently emit unescaped pipes inside code spans). Only the
    code-span interior is touched; table-delimiter pipes outside spans are left
    alone, and an already-escaped ``\|`` is not double-escaped. Call only on
    table-row text (see :func:`_normalize_table_block`); applying it to prose
    inline-code would leave a literal backslash in the rendered span.
    """

    def _repl(match: re.Match[str]) -> str:
        ticks = match.group("ticks")
        body = re.sub(r"(?<!\\)\|", r"\\|", match.group("body"))
        return f"{ticks}{body}{ticks}"

    return _CODE_SPAN_RE.sub(_repl, text)


def _split_pipe_cells(segment: str) -> list[str]:
    """Split a ``| a | b |`` run into stripped inner cells (drops the frame)."""
    parts = re.split(r"(?<!\\)\|", segment)
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [part.strip() for part in parts]


def _is_pipe_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _delimiter_markers(run: str) -> list[str]:
    """Return per-column alignment markers (``---``, ``:---``, ``---:``, ``:---:``)."""
    markers: list[str] = []
    for cell in _split_pipe_cells(run):
        left = cell.startswith(":")
        right = cell.endswith(":")
        if left and right:
            markers.append(":---:")
        elif right:
            markers.append("---:")
        elif left:
            markers.append(":---")
        else:
            markers.append("---")
    return markers


def _normalize_table_block(text: str) -> str:
    """Repair malformed GFM tables in a fence-free block of markdown.

    Models occasionally glue a table header onto preceding prose, drop the
    newline between the header and the ``|---|`` delimiter, or cram data rows
    onto the delimiter line — markdown-it then renders the whole thing as raw
    text. Anchored on the delimiter run, this rebuilds each region it is
    *confident* is a table (delimiter at line start, header and data cell counts
    both equal to the delimiter's column count) and passes everything else
    through untouched. Well-formed tables are rebuilt to identical-rendering
    markdown, so the pass is safe to apply unconditionally.
    """
    out = ""
    while True:
        match = _DELIM_RUN_RE.search(text)
        if match is None:
            return out + text
        markers = _delimiter_markers(match.group(0))
        n_cols = len(markers)
        head = text[: match.start()]
        tail = text[match.end() :]

        # The delimiter must start its own line — guards against inline ``|-|``.
        # Any leading whitespace is the table's indentation (e.g. nested under a
        # list item); preserve it when re-emitting so we never promote an
        # indented table to top level.
        indent = head[head.rfind("\n") + 1 :]
        if n_cols < 2 or indent.strip() != "":
            out += text[: match.end()]
            text = tail
            continue

        head_lines = head.split("\n")
        while head_lines and head_lines[-1] == "":
            head_lines.pop()
        header_match = _HEADER_RE.match(head_lines[-1]) if head_lines else None
        header_cells = (
            _split_pipe_cells(_escape_code_span_pipes(header_match.group("cells")))
            if header_match
            else []
        )
        if header_match is None or len(header_cells) != n_cols:
            out += text[: match.end()]
            text = tail
            continue

        # Data rows: the same-line remainder after the delimiter plus any
        # following pipe rows, re-chunked into rows of ``n_cols`` cells.
        tail_lines = tail.split("\n")
        data_segments = [tail_lines[0]] if tail_lines[0].strip() else []
        consumed = 1
        for line in tail_lines[1:]:
            if _is_pipe_row(line):
                data_segments.append(line)
                consumed += 1
            else:
                break
        data_rows: list[list[str]] = []
        bail = False
        for segment in data_segments:
            cells = _split_pipe_cells(_escape_code_span_pipes(segment))
            if not cells:
                continue
            if len(cells) % n_cols != 0:
                bail = True  # ambiguous (e.g. glued rows with empty cells) — leave as-is
                break
            for i in range(0, len(cells), n_cols):
                data_rows.append(cells[i : i + n_cols])
        if bail:
            out += text[: match.end()]
            text = tail
            continue

        preamble = head_lines[:-1]
        prose = header_match.group("prefix").rstrip()
        if preamble:
            out += "\n".join(preamble) + "\n"
        if prose:
            out += prose + "\n"
        # A GFM table must be preceded by a blank line (it cannot interrupt a
        # paragraph), so ensure one before emitting the header.
        if out and not out.endswith("\n\n"):
            out += "\n" if out.endswith("\n") else "\n\n"
        out += f"{indent}| " + " | ".join(header_cells) + " |\n"
        out += f"{indent}| " + " | ".join(markers) + " |\n"
        for row in data_rows:
            out += f"{indent}| " + " | ".join(row) + " |\n"

        remainder = "\n".join(tail_lines[consumed:])
        if not remainder.strip():
            return out
        text = remainder if remainder.startswith("\n") else "\n" + remainder


def _loosen_tight_ordered_lists(markup: str) -> str:
    """Insert a blank line before each ordered-list item that immediately follows another."""
    lines = markup.splitlines(keepends=True)
    out: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    prev_was_ol = False

    for line in lines:
        m = _FENCE_RE.match(line)
        if in_fence:
            out.append(line)
            if m:
                fence = m.group("fence")
                if fence.startswith(fence_char) and len(fence) >= fence_len:
                    in_fence = False
                    fence_char = ""
                    fence_len = 0
            prev_was_ol = False
            continue
        if m:
            in_fence = True
            fence_char = m.group("fence")[0]
            fence_len = len(m.group("fence"))
            out.append(line)
            prev_was_ol = False
            continue

        is_ol = bool(_OL_ITEM_RE.match(line))
        if is_ol and prev_was_ol:
            out.append("\n")
        out.append(line)
        prev_was_ol = is_ol

    return "".join(out)


def _normalize_markdown_tables(markup: str) -> str:
    """Apply :func:`_normalize_table_block` to every fence-free span of markup."""
    if "|" not in markup or "-" not in markup:
        return markup

    out: list[str] = []
    buffer: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    def flush() -> None:
        if buffer:
            out.append(_normalize_table_block("\n".join(buffer)))
            buffer.clear()

    for line in markup.splitlines():
        match = _FENCE_RE.match(line)
        if in_fence:
            fence = match.group("fence") if match else ""
            if fence and fence[0] == fence_char and len(fence) >= fence_len:
                in_fence = False
            out.append(line)
            continue
        if match:
            flush()
            in_fence = True
            fence_char = match.group("fence")[0]
            fence_len = len(match.group("fence"))
            out.append(line)
            continue
        buffer.append(line)
    flush()

    result = "\n".join(out)
    if markup.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


class PythinkerMarkdown(Markdown):
    """Drop-in replacement for ``rich.markdown.Markdown`` with the Pythinker palette.

    Markup is run through :func:`sanitize_ansi` first so terminal control
    sequences embedded in model/user/custom text cannot reach the terminal
    (cursor moves, color leaks) when rendered as Markdown. Large emoji status
    icons are then normalized to compact monochrome glyphs for calmer reports.
    """

    elements = {
        **Markdown.elements,
        "fence": _BorderedCodeBlock,
        "code_block": _BorderedCodeBlock,
        "table_open": _ReportTableElement,
    }

    def __init__(self, markup: str, *args: Any, **kwargs: Any) -> None:
        safe_markup = sanitize_ansi(markup)
        unwrapped_markup = _unwrap_fenced_markdown_tables(safe_markup)
        repaired_markup = _repair_crammed_markdown_tables(unwrapped_markup)
        normalized_markup = _normalize_markdown_tables(repaired_markup)
        loosened_markup = _loosen_tight_ordered_lists(normalized_markup)
        super().__init__(_simplify_markdown_report_icons(loosened_markup), *args, **kwargs)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        overrides = _markdown_style_overrides()
        with console.use_theme(Theme(overrides, inherit=True)):
            yield from super().__rich_console__(console, options)


def pythinker_markdown(text: str, *, code_theme: str | None = None) -> PythinkerMarkdown:
    """Build a :class:`PythinkerMarkdown` with the palette pre-wired.

    ``code_theme=None`` defers to the active code theme (``config.tui.code_theme``).
    """
    return PythinkerMarkdown(text, code_theme=code_theme)


# ---------------------------------------------------------------------------
# Streaming boundary helper
# ---------------------------------------------------------------------------


_SENTENCE_END = (".", "!", "?", ";", ":")
_SELF_CLOSING_BLOCKS = frozenset(("fence", "code_block", "hr", "html_block"))

# Lazy-initialized markdown-it parser for incremental token commitment.
_md_parser: MarkdownIt | None = None


def _get_md_parser() -> MarkdownIt:
    global _md_parser
    if _md_parser is None:
        from markdown_it import MarkdownIt

        # Match the extensions used by the rendering path
        # (pythinker_code.utils.rich.markdown.Markdown) so that block
        # boundaries are detected consistently.
        _md_parser = MarkdownIt().enable("strikethrough").enable("table")
    return _md_parser


@functools.lru_cache(maxsize=64)
def _markdown_commit_boundary_cached(text: str) -> int | None:
    md = _get_md_parser()
    tokens = md.parse(text)

    block_maps: list[list[int]] = []
    depth = 0
    for token in tokens:
        if token.nesting == 1:
            if depth == 0 and token.map is not None:
                block_maps.append(token.map)
            depth += 1
        elif token.nesting == -1:
            depth -= 1
        elif depth == 0 and token.type in _SELF_CLOSING_BLOCKS and token.map is not None:
            block_maps.append(token.map)

    if len(block_maps) < 2:
        return None

    target_line = block_maps[-2][1]
    offset = 0
    for _ in range(target_line):
        offset = text.index("\n", offset) + 1
    return offset


def markdown_commit_boundary(text: str) -> int | None:
    """Return the offset up to which streamed markdown can be committed.

    The last top-level block is treated as still mutable, so callers only
    permanently print completed blocks. Nested tokens (list items, blockquote
    children, table rows) stay with their parent block.
    """
    if not text:
        return None
    return _markdown_commit_boundary_cached(text)


def _find_stream_safe_boundary(text: str) -> int | None:
    """Return an index in ``text`` that is safe to flush, or ``None``.

    Parser-backed block commitment handles multi-line markdown constructs such
    as tables, lists, and fenced code. A sentence-final fallback is kept only
    for single-line prose so long plain paragraphs still become visible without
    waiting for a blank line or a second markdown block.
    """
    boundary = markdown_commit_boundary(text)
    if boundary is not None:
        return boundary

    if "\n" in text:
        return None
    stripped = text.rstrip()
    if stripped.endswith(_SENTENCE_END):
        return len(text)
    return None


@dataclass(slots=True)
class PythinkerMarkdownStream:
    """Buffer streamed markdown deltas and yield safe-to-render slices.

    Internal testing fixture for markdown boundary behavior. The production
    rendering path in ``_ContentBlock._flush_committed`` calls
    :func:`markdown_commit_boundary` directly.
    """

    pending: str = field(default="")

    def push(self, delta: str) -> str | None:
        """Append ``delta`` and return the next ready slice, or ``None``."""
        self.pending += delta
        cut = _find_stream_safe_boundary(self.pending)
        if cut is None or cut == 0:
            return None
        ready = self.pending[:cut]
        self.pending = self.pending[cut:]
        return ready

    def flush(self) -> str | None:
        """Return any remaining buffered markdown, clearing the buffer."""
        if not self.pending.strip():
            self.pending = ""
            return None
        pending = self.pending
        self.pending = ""
        return pending
