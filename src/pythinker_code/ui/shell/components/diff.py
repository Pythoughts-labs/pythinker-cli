"""Pythinker diff component.


plus the diff-string generator from ``edit-diff.ts``.

Two entry points:

* :func:`compute_edit_diff_string` — given ``old_text`` / ``new_text``,
  produce Pythinker's per-line diff format (``+123 content``, ``-123 content``,
  `` 123 content``, with `` ... `` skip markers).
* :func:`render_diff` — colorize a Pythinker-format diff string into a column-
  split Rich table (line number, ``+``/``-`` marker, code body) with intra-line
  word highlighting on single-line edits and correct wrap alignment.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.measure import Measurement
from rich.style import StyleType
from rich.table import Table
from rich.text import Text

from pythinker_code.ui.shell.render_constants import (
    DIFF_CONTEXT_LINES,
    DIFF_LINE_NUMBER_MIN_WIDTH,
)
from pythinker_code.ui.terminal_capabilities import colors_disabled
from pythinker_code.ui.theme import get_diff_colors, tui_rich_style
from pythinker_code.utils.rich.diff_render import (
    apply_inline_diff_highlights,
    highlight_diff_code,
    make_diff_highlighter,
)
from pythinker_code.utils.rich.syntax import PythinkerSyntax

__all__ = [
    "EditDiffResult",
    "compute_edit_diff_string",
    "render_diff",
]

_DEFAULT_CONTEXT_LINES = DIFF_CONTEXT_LINES
_TAB_REPLACEMENT = "   "
_DIFF_LINE_RE = re.compile(r"^([+\-\s])(\s*\d*)\s(.*)$")
_SIGN_COL_WIDTH = 3


@dataclass(frozen=True, slots=True)
class EditDiffResult:
    """Output of :func:`compute_edit_diff_string`."""

    diff: str
    first_changed_line: int | None


@dataclass(slots=True)
class _LogicalDiffRow:
    line_num: str
    sign: str
    body: Text
    row_style: StyleType
    sign_style: StyleType
    line_num_style: StyleType = "dim"


def _wrap_body_chunks(body: Text, console: Console, width: int) -> list[Text]:
    """Wrap *body* to *width*, preserving syntax/inline styles on each chunk.

    Rich's ``Text.wrap`` does not split Pygments-highlighted text reliably, so
    wrap the plain string and slice styled spans for each visual chunk.
    """
    if not body.plain:
        return [Text("")]
    plain_chunks = list(Text(body.plain).wrap(console, max(1, width)))
    if not plain_chunks:
        return [Text("")]
    if len(plain_chunks) == 1 and len(plain_chunks[0].plain) >= len(body.plain):
        return [body]
    chunks: list[Text] = []
    offset = 0
    for plain_chunk in plain_chunks:
        chunk_len = len(plain_chunk.plain)
        chunks.append(body[offset : offset + chunk_len])
        offset += chunk_len
    return chunks


class _CompactDiffGrid:
    """Three-column diff layout: line number | sign | code body.

    Long code bodies are pre-wrapped at render time so continuation rows repeat
    the ``+``/``-`` sign while the line-number column stays blank.
    """

    def __init__(self, rows: list[_LogicalDiffRow], *, line_num_width: int) -> None:
        self._rows = rows
        self._line_num_width = line_num_width

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(0, options.max_width or console.width or 80)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        max_width = options.max_width or console.width or 80
        fixed = self._line_num_width + _SIGN_COL_WIDTH
        content_width = max(1, max_width - fixed)

        table = Table.grid(padding=0, expand=True)
        table.add_column(width=self._line_num_width, no_wrap=True, justify="right")
        table.add_column(width=_SIGN_COL_WIDTH, no_wrap=True)
        table.add_column(ratio=1, no_wrap=True)

        blank_ln = " " * self._line_num_width
        for row in self._rows:
            chunks = _wrap_body_chunks(row.body, console, content_width)
            for index, chunk in enumerate(chunks):
                if index == 0 and row.line_num:
                    ln_cell = Text(
                        row.line_num.rjust(self._line_num_width), style=row.line_num_style
                    )
                else:
                    ln_cell = Text(blank_ln, style=row.line_num_style)
                sign_cell = Text(
                    {"+": " + ", "-": " - ", " ": "   "}.get(row.sign, "   "),
                    style=row.sign_style,
                )
                table.add_row(ln_cell, sign_cell, chunk, style=row.row_style)

        yield from console.render(table, options)


def _replace_tabs(text: str) -> str:
    return text.replace("\t", _TAB_REPLACEMENT)


def compute_edit_diff_string(
    old_text: str,
    new_text: str,
    *,
    context_lines: int = _DEFAULT_CONTEXT_LINES,
    old_start: int = 1,
    new_start: int = 1,
) -> EditDiffResult:
    """Build Pythinker's custom diff format from ``old_text``/``new_text``.

    Format per line:

    * ``+<n> <content>`` — added line at new file line ``n``
    * ``-<n> <content>`` — removed line at old file line ``n``
    * `` <n> <content>`` — context line
    * `` <pad> ...``   — collapsed-context marker

    ``old_start`` / ``new_start`` let callers render bounded diff hunks with
    the real source-file line numbers, matching the structured diff display in
    the reference UI. Defaults preserve the old tool-input-only behavior.

    Returns ``("", None)`` when the texts are identical.
    """
    if old_text == new_text:
        return EditDiffResult(diff="", first_changed_line=None)

    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    last_old = old_start + max(0, len(old_lines) - 1)
    last_new = new_start + max(0, len(new_lines) - 1)
    line_num_width = max(DIFF_LINE_NUMBER_MIN_WIDTH, len(str(max(last_old, last_new))))

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    output: list[str] = []
    old_lineno = old_start
    new_lineno = new_start
    first_changed: int | None = None
    last_was_change = False
    opcodes = matcher.get_opcodes()

    def _pad(n: int) -> str:
        return str(n).rjust(line_num_width)

    def _blank_pad() -> str:
        return " " * line_num_width

    for idx, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag in ("replace", "delete", "insert"):
            if first_changed is None:
                first_changed = new_lineno
            for line in old_lines[i1:i2] if tag != "insert" else []:
                output.append(f"-{_pad(old_lineno)} {line}")
                old_lineno += 1
            for line in new_lines[j1:j2] if tag != "delete" else []:
                output.append(f"+{_pad(new_lineno)} {line}")
                new_lineno += 1
            last_was_change = True
            continue

        # tag == "equal" — context block.
        block = old_lines[i1:i2]
        next_change = idx < len(opcodes) - 1 and opcodes[idx + 1][0] != "equal"
        leading = last_was_change
        trailing = next_change

        if leading and trailing:
            if len(block) <= context_lines * 2:
                for line in block:
                    output.append(f" {_pad(old_lineno)} {line}")
                    old_lineno += 1
                    new_lineno += 1
            else:
                head = block[:context_lines]
                tail = block[-context_lines:]
                skipped = len(block) - len(head) - len(tail)
                for line in head:
                    output.append(f" {_pad(old_lineno)} {line}")
                    old_lineno += 1
                    new_lineno += 1
                output.append(f" {_blank_pad()} ...")
                old_lineno += skipped
                new_lineno += skipped
                for line in tail:
                    output.append(f" {_pad(old_lineno)} {line}")
                    old_lineno += 1
                    new_lineno += 1
        elif leading:
            shown = block[:context_lines]
            skipped = len(block) - len(shown)
            for line in shown:
                output.append(f" {_pad(old_lineno)} {line}")
                old_lineno += 1
                new_lineno += 1
            if skipped > 0:
                output.append(f" {_blank_pad()} ...")
                old_lineno += skipped
                new_lineno += skipped
        elif trailing:
            skipped = max(0, len(block) - context_lines)
            if skipped > 0:
                output.append(f" {_blank_pad()} ...")
                old_lineno += skipped
                new_lineno += skipped
            for line in block[skipped:]:
                output.append(f" {_pad(old_lineno)} {line}")
                old_lineno += 1
                new_lineno += 1
        else:
            old_lineno += len(block)
            new_lineno += len(block)

        last_was_change = False

    return EditDiffResult(diff="\n".join(output), first_changed_line=first_changed)


def _parse_diff_line(line: str) -> tuple[str, str, str] | None:
    match = _DIFF_LINE_RE.match(line)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _line_number_width(lines: list[str]) -> int:
    max_val = 0
    for line in lines:
        parsed = _parse_diff_line(line)
        if parsed is None:
            continue
        raw = parsed[1].strip()
        if raw.isdigit():
            max_val = max(max_val, int(raw))
    if max_val:
        return max(DIFF_LINE_NUMBER_MIN_WIDTH, len(str(max_val)))
    return DIFF_LINE_NUMBER_MIN_WIDTH


def _display_line_num(raw: str) -> str:
    stripped = raw.strip()
    return stripped if stripped else ""


def _intra_line_diff(old_content: str, new_content: str) -> tuple[Text, Text]:
    """Word-level highlighting on changed tokens.

    Returns ``(removed_text, added_text)`` with changed tokens carrying the
    theme's brighter add/del highlight backgrounds (GitHub-style word
    emphasis), but *not* yet wrapped in red/green — callers add the
    row-level style. Reverse video is deliberately avoided: it reads as
    glaring blocks on dark terminals.
    """
    colors = get_diff_colors()
    removed_hl = colors.del_hl
    added_hl = colors.add_hl

    def _tokenize(s: str) -> list[str]:
        return re.findall(r"\s+|\S+", s)

    old_tokens = _tokenize(old_content)
    new_tokens = _tokenize(new_content)
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    removed = Text()
    added = Text()
    first_removed = True
    first_added = True
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            piece = "".join(old_tokens[i1:i2])
            removed.append(piece)
            added.append(piece)
            if piece.strip():
                first_removed = False
                first_added = False
            continue
        # delete / insert / replace
        if tag in ("delete", "replace"):
            piece = "".join(old_tokens[i1:i2])
            if first_removed:
                stripped = piece.lstrip()
                leading = piece[: len(piece) - len(stripped)]
                if leading:
                    removed.append(leading)
                if stripped:
                    removed.append(stripped, style=removed_hl)
                if stripped:
                    first_removed = False
            else:
                removed.append(piece, style=removed_hl)
        if tag in ("insert", "replace"):
            piece = "".join(new_tokens[j1:j2])
            if first_added:
                stripped = piece.lstrip()
                leading = piece[: len(piece) - len(stripped)]
                if leading:
                    added.append(leading)
                if stripped:
                    added.append(stripped, style=added_hl)
                if stripped:
                    first_added = False
            else:
                added.append(piece, style=added_hl)
    return removed, added


def _similarity_ratio(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()


def _render_diff_content(
    content: str,
    row_style: StyleType,
    *,
    highlighter: PythinkerSyntax | None,
) -> Text:
    """Render one diff body line with optional syntax highlighting."""
    normalized = _replace_tabs(content)
    if highlighter is None:
        return Text(normalized, style=row_style)
    inner = highlight_diff_code(highlighter, normalized)
    inner.stylize_before(row_style)
    return inner


def _append_row(
    rows: list[_LogicalDiffRow],
    *,
    line_num: str,
    sign: str,
    body: Text,
    row_style: StyleType,
    sign_style: StyleType,
    line_num_style: StyleType = "dim",
) -> None:
    rows.append(
        _LogicalDiffRow(
            line_num=line_num,
            sign=sign,
            body=body,
            row_style=row_style,
            sign_style=sign_style,
            line_num_style=line_num_style,
        )
    )


def render_diff(diff_text: str, *, path: str | None = None) -> RenderableType:
    """Colorize a Pythinker-format diff string.

    ``diff_text`` is whatever :func:`compute_edit_diff_string` produced (or
    any string in the same format). Lines that don't match the prefix
    pattern are rendered as dim context.

    Output uses a three-column grid (line number | ``+``/``-`` | code body)
    so wrapped continuation rows stay aligned under the code column and
    repeat the diff sign.

    When *path* is provided, code lines are syntax-highlighted with the
    active ``tui.code_theme`` (same pipeline as approval/pager diffs). Style
    layering per changed line is: syntax foreground, row ``add_bg``/``del_bg``
    underneath via ``stylize_before``, then inline ``add_hl``/``del_hl`` on
    top. Syntax highlighting is skipped when terminal colors are disabled
    (``NO_COLOR``, ``PYTHINKER_NO_COLOR``, ``TERM=dumb``, etc.).
    """
    if not diff_text:
        return Text("")

    colors = get_diff_colors()
    # Added/removed rows are distinguished by background tint only; line numbers,
    # +/- markers, and code content all use the terminal's default foreground
    # so the diff reads as light text on deep green/red rather than recolored
    # green/red glyphs.
    added_sign = colors.add_bg
    removed_sign = colors.del_bg
    added_body = colors.add_bg
    removed_body = colors.del_bg
    context_style = tui_rich_style("tool_diff_context")
    highlighter = make_diff_highlighter(path) if path and not colors_disabled() else None

    lines = diff_text.split("\n")
    line_num_width = _line_number_width(lines)
    rows: list[_LogicalDiffRow] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        parsed = _parse_diff_line(line)
        if parsed is None:
            _append_row(
                rows,
                line_num="",
                sign=" ",
                body=Text(line, style=context_style),
                row_style=context_style,
                sign_style=context_style,
                line_num_style=context_style,
            )
            i += 1
            continue
        prefix, line_num, content = parsed
        display_ln = _display_line_num(line_num)

        if prefix == "-":
            removed_block: list[tuple[str, str]] = []
            while i < len(lines):
                p = _parse_diff_line(lines[i])
                if p is None or p[0] != "-":
                    break
                removed_block.append((p[1], p[2]))
                i += 1
            added_block: list[tuple[str, str]] = []
            while i < len(lines):
                p = _parse_diff_line(lines[i])
                if p is None or p[0] != "+":
                    break
                added_block.append((p[1], p[2]))
                i += 1

            use_word_level = False
            if len(removed_block) == 1 and len(added_block) == 1:
                rcontent = removed_block[0][1]
                acontent = added_block[0][1]
                if highlighter is not None:
                    rln, _ = removed_block[0]
                    aln, _ = added_block[0]
                    rtab = _replace_tabs(rcontent)
                    atab = _replace_tabs(acontent)
                    rem_inner = highlight_diff_code(highlighter, rtab)
                    add_inner = highlight_diff_code(highlighter, atab)
                    rem_inner.stylize_before(removed_body)
                    add_inner.stylize_before(added_body)
                    apply_inline_diff_highlights(highlighter, rtab, atab, rem_inner, add_inner)
                    _append_row(
                        rows,
                        line_num=_display_line_num(rln),
                        sign="-",
                        body=rem_inner,
                        row_style=removed_body,
                        sign_style=removed_sign,
                    )
                    _append_row(
                        rows,
                        line_num=_display_line_num(aln),
                        sign="+",
                        body=add_inner,
                        row_style=added_body,
                        sign_style=added_sign,
                    )
                    continue
                use_word_level = _similarity_ratio(rcontent, acontent) >= 0.5
            if use_word_level:
                rln, rcontent = removed_block[0]
                aln, acontent = added_block[0]
                rem_inner, add_inner = _intra_line_diff(
                    _replace_tabs(rcontent),
                    _replace_tabs(acontent),
                )
                rem_inner.stylize_before(removed_body)
                add_inner.stylize_before(added_body)
                _append_row(
                    rows,
                    line_num=_display_line_num(rln),
                    sign="-",
                    body=rem_inner,
                    row_style=removed_body,
                    sign_style=removed_sign,
                )
                _append_row(
                    rows,
                    line_num=_display_line_num(aln),
                    sign="+",
                    body=add_inner,
                    row_style=added_body,
                    sign_style=added_sign,
                )
            else:
                for ln, block_content in removed_block:
                    _append_row(
                        rows,
                        line_num=_display_line_num(ln),
                        sign="-",
                        body=_render_diff_content(
                            block_content, removed_body, highlighter=highlighter
                        ),
                        row_style=removed_body,
                        sign_style=removed_sign,
                    )
                for ln, block_content in added_block:
                    _append_row(
                        rows,
                        line_num=_display_line_num(ln),
                        sign="+",
                        body=_render_diff_content(
                            block_content, added_body, highlighter=highlighter
                        ),
                        row_style=added_body,
                        sign_style=added_sign,
                    )
        elif prefix == "+":
            _append_row(
                rows,
                line_num=display_ln,
                sign="+",
                body=_render_diff_content(content, added_body, highlighter=highlighter),
                row_style=added_body,
                sign_style=added_sign,
            )
            i += 1
        else:
            if content == "...":
                _append_row(
                    rows,
                    line_num="",
                    sign=" ",
                    body=Text("...", style="dim"),
                    row_style=context_style,
                    sign_style=context_style,
                    line_num_style=context_style,
                )
            elif highlighter is None:
                _append_row(
                    rows,
                    line_num=display_ln,
                    sign=" ",
                    body=Text(_replace_tabs(content), style=context_style),
                    row_style=context_style,
                    sign_style=context_style,
                    line_num_style="dim",
                )
            else:
                _append_row(
                    rows,
                    line_num=display_ln,
                    sign=" ",
                    body=highlight_diff_code(highlighter, _replace_tabs(content)),
                    row_style="",
                    sign_style="dim",
                    line_num_style="dim",
                )
            i += 1

    return _CompactDiffGrid(rows, line_num_width=line_num_width)
