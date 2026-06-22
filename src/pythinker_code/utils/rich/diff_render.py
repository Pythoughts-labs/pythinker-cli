"""Unified diff rendering for CLI tool results and approval panels.

All diff rendering flows through this module:
- ``render_diff_panel``  — full diff with Panel, Table, background colors (tool results & pager)
- ``render_diff_preview`` — compact changed-lines-only preview (approval panel)
- ``collect_diff_hunks``  — shared data preparation from DiffDisplayBlocks
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum, auto
from functools import lru_cache

from rich.console import RenderableType
from rich.panel import Panel
from rich.style import Style as RichStyle
from rich.table import Table
from rich.text import Span, Text

from pythinker_code.tools.display import DiffDisplayBlock
from pythinker_code.ui.theme import get_diff_colors, tui_rich_style
from pythinker_code.utils.rich.syntax import (
    PythinkerSyntax,
    get_active_code_theme,
    resolve_code_theme,
)

_INLINE_DIFF_MIN_RATIO = 0.5  # skip inline diff when lines are too dissimilar

DIFF_CONTEXT_LINES = 3
DIFF_LINE_NUMBER_MIN_WIDTH = 2
MAX_PREVIEW_CHANGED_LINES = 6


def _expand_hint(remaining: int) -> str:
    """Return the shell's canonical expand hint without importing shell at module load."""
    try:
        from pythinker_code.ui.shell.render_constants import expand_hint
    except ImportError:
        plural = "s" if remaining != 1 else ""
        return f"… {remaining} more line{plural} (ctrl+o to expand)"
    return expand_hint(remaining)


# ---------------------------------------------------------------------------
# Data model — parsed diff lines
# ---------------------------------------------------------------------------


class DiffLineKind(Enum):
    CONTEXT = auto()
    ADD = auto()
    DELETE = auto()


@dataclass(slots=True)
class DiffLine:
    kind: DiffLineKind
    old_num: int  # 0 means "not applicable" (e.g. added line has no old number)
    new_num: int  # 0 means "not applicable" (e.g. deleted line has no new number)
    code: str
    content: Text | None = None  # filled after highlighting
    is_inline_paired: bool = False  # True if this line was paired for inline diff


# ---------------------------------------------------------------------------
# Core: build DiffLines directly from old_text / new_text via SequenceMatcher
# ---------------------------------------------------------------------------


def _build_diff_lines(
    old_text: str,
    new_text: str,
    old_start: int,
    new_start: int,
    n_context: int = DIFF_CONTEXT_LINES,
) -> list[list[DiffLine]]:
    """Build grouped DiffLine hunks directly from old/new text.

    Returns a list of hunks, where each hunk is a list of DiffLine objects.
    This replaces the format_unified_diff → parse roundtrip.
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)

    hunks: list[list[DiffLine]] = []
    for group in matcher.get_grouped_opcodes(n=n_context):
        hunk: list[DiffLine] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i2 - i1):
                    hunk.append(
                        DiffLine(
                            kind=DiffLineKind.CONTEXT,
                            old_num=old_start + i1 + k,
                            new_num=new_start + j1 + k,
                            code=old_lines[i1 + k],
                        )
                    )
            elif tag == "delete":
                for k in range(i2 - i1):
                    hunk.append(
                        DiffLine(
                            kind=DiffLineKind.DELETE,
                            old_num=old_start + i1 + k,
                            new_num=0,
                            code=old_lines[i1 + k],
                        )
                    )
            elif tag == "insert":
                for k in range(j2 - j1):
                    hunk.append(
                        DiffLine(
                            kind=DiffLineKind.ADD,
                            old_num=0,
                            new_num=new_start + j1 + k,
                            code=new_lines[j1 + k],
                        )
                    )
            elif tag == "replace":
                for k in range(i2 - i1):
                    hunk.append(
                        DiffLine(
                            kind=DiffLineKind.DELETE,
                            old_num=old_start + i1 + k,
                            new_num=0,
                            code=old_lines[i1 + k],
                        )
                    )
                for k in range(j2 - j1):
                    hunk.append(
                        DiffLine(
                            kind=DiffLineKind.ADD,
                            old_num=0,
                            new_num=new_start + j1 + k,
                            code=new_lines[j1 + k],
                        )
                    )
        if hunk:
            hunks.append(hunk)
    return hunks


# ---------------------------------------------------------------------------
# Syntax highlighting & inline diff (shared by approval panels and tool cards)
# ---------------------------------------------------------------------------


def make_diff_highlighter(path: str) -> PythinkerSyntax:
    """Create a :class:`PythinkerSyntax` highlighter for *path*'s file extension."""
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    lexer = ext if ext else "text"
    return _cached_diff_highlighter(lexer, get_active_code_theme())


@lru_cache(maxsize=64)
def _cached_diff_highlighter(lexer: str, theme: str) -> PythinkerSyntax:
    return PythinkerSyntax("", lexer, theme=resolve_code_theme(theme))


def _without_bg(style: RichStyle | str) -> RichStyle | str:
    """Return *style* with its background color dropped (foreground kept)."""
    if not isinstance(style, RichStyle) or style.bgcolor is None:
        return style
    return RichStyle(
        color=style.color,
        bold=style.bold,
        dim=style.dim,
        italic=style.italic,
        underline=style.underline,
        strike=style.strike,
        reverse=style.reverse,
    )


def _strip_background(text: Text) -> Text:
    """Drop syntax-theme backgrounds in place.

    The code theme paints its base color (e.g. catppuccin ``#1E1E2E``) onto
    every cell. Inside a diff that opaque block masks the green/red row tints
    and only blends on terminals whose own background happens to match it.
    Removing it lets the diff row's add/remove tint — and the terminal
    background on context lines — show through on every terminal.
    """
    if isinstance(text.style, RichStyle):
        text.style = _without_bg(text.style)
    text.spans = [Span(span.start, span.end, _without_bg(span.style)) for span in text.spans]
    return text


def highlight_diff_code(highlighter: PythinkerSyntax, code: str) -> Text:
    """Syntax-highlight a single diff code line (no row/inline diff styling)."""
    t = highlighter.highlight(code)
    # Pygments appends a trailing newline (ensurenl=True); strip only that,
    # not trailing whitespace which may be meaningful in diffs.
    if t.plain.endswith("\n"):
        t.right_crop(1)
    return _strip_background(t)


def apply_inline_diff_highlights(
    highlighter: PythinkerSyntax,
    old_code: str,
    new_code: str,
    old_text: Text,
    new_text: Text,
    *,
    min_ratio: float = _INLINE_DIFF_MIN_RATIO,
) -> bool:
    """Mark changed spans with del_hl/add_hl on syntax-highlighted text.

    Callers should apply row backgrounds with ``stylize_before`` *before*
    invoking this helper so the stack is: row tint, syntax foreground, inline
    highlight backgrounds on top.
    """
    sm = SequenceMatcher(None, old_code, new_code)
    if sm.ratio() < min_ratio:
        return False
    colors = get_diff_colors()
    tab_size = highlighter.tab_size
    old_map = _build_offset_map(old_code, old_text.plain, tab_size)
    new_map = _build_offset_map(new_code, new_text.plain, tab_size)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("delete", "replace"):
            old_text.stylize(colors.del_hl, old_map[i1], old_map[i2])
        if op in ("insert", "replace"):
            new_text.stylize(colors.add_hl, new_map[j1], new_map[j2])
    return True


def highlight_diff_inline_pair(
    highlighter: PythinkerSyntax,
    old_code: str,
    new_code: str,
    *,
    min_ratio: float = _INLINE_DIFF_MIN_RATIO,
) -> tuple[Text, Text, bool]:
    """Highlight a delete/add pair and optionally mark changed spans inline.

    Returns ``(removed_text, added_text, inline_paired)``. *inline_paired* is
    ``True`` when the lines were similar enough for word-level emphasis.

    Row backgrounds are *not* applied here; approval panels attach them at
    table render time. Tool cards should ``stylize_before`` row tints before
    calling :func:`apply_inline_diff_highlights`.
    """
    old_text = highlight_diff_code(highlighter, old_code)
    new_text = highlight_diff_code(highlighter, new_code)
    inline_paired = apply_inline_diff_highlights(
        highlighter,
        old_code,
        new_code,
        old_text,
        new_text,
        min_ratio=min_ratio,
    )
    return old_text, new_text, inline_paired


def _make_highlighter(path: str) -> PythinkerSyntax:
    return make_diff_highlighter(path)


def _highlight(highlighter: PythinkerSyntax, code: str) -> Text:
    return highlight_diff_code(highlighter, code)


def _build_offset_map(raw: str, rendered: str, tab_size: int) -> list[int]:
    """Build a mapping from raw-string indices to rendered-string indices.

    The highlighter expands tabs via ``str.expandtabs(tab_size)`` before
    tokenising.  We replicate the same column-aware expansion that the
    Python builtin defines (the only parameter is *tab_size*; the behaviour
    is fully specified in the language docs and has no external
    configurability).

    Returns a list of length ``len(raw) + 1`` where ``result[i]`` is the
    rendered offset corresponding to raw position *i*.
    """
    if raw == rendered:
        return list(range(len(raw) + 1))
    offsets: list[int] = []
    col = 0
    for ch in raw:
        offsets.append(col)
        if ch == "\t":
            col += tab_size - (col % tab_size)
        else:
            col += 1
    offsets.append(col)
    if col != len(rendered):
        # The highlighter transformed the text in a way we didn't expect.
        # Return a bounded, monotonic best-effort map so inline stylizing
        # can proceed without crashing or producing out-of-range offsets.
        rendered_len = len(rendered)
        raw_len = len(raw)
        if raw_len == 0:
            return [rendered_len]
        return [(i * rendered_len) // raw_len for i in range(raw_len)] + [rendered_len]
    return offsets


def _apply_inline_diff(
    highlighter: PythinkerSyntax,
    del_lines: list[DiffLine],
    add_lines: list[DiffLine],
) -> None:
    """Pair delete/add lines and apply word-level inline diff highlighting.

    Modifies DiffLine.content in place for paired lines.
    """
    paired = min(len(del_lines), len(add_lines))
    for j in range(paired):
        old_code = del_lines[j].code
        new_code = add_lines[j].code
        old_text, new_text, inline_paired = highlight_diff_inline_pair(
            highlighter, old_code, new_code
        )
        del_lines[j].content = old_text
        add_lines[j].content = new_text
        if inline_paired:
            del_lines[j].is_inline_paired = True
            add_lines[j].is_inline_paired = True


def _highlight_hunk(highlighter: PythinkerSyntax, hunk: list[DiffLine]) -> None:
    """Highlight all lines in a hunk, applying inline diff for paired -/+ blocks."""
    # First pass: find consecutive -/+ blocks and apply inline diff
    i = 0
    while i < len(hunk):
        if hunk[i].kind == DiffLineKind.DELETE:
            del_start = i
            while i < len(hunk) and hunk[i].kind == DiffLineKind.DELETE:
                i += 1
            add_start = i
            while i < len(hunk) and hunk[i].kind == DiffLineKind.ADD:
                i += 1
            _apply_inline_diff(
                highlighter,
                hunk[del_start:add_start],
                hunk[add_start:i],
            )
        else:
            i += 1

    # Second pass: highlight any lines not yet highlighted by inline diff
    for dl in hunk:
        if dl.content is None:
            dl.content = _highlight(highlighter, dl.code)


# ---------------------------------------------------------------------------
# Shared header builder
# ---------------------------------------------------------------------------


def _build_diff_header(path: str, added: int, removed: int) -> Text:
    """Build the file header text: stats + path."""
    header = Text()
    if added > 0:
        header.append(f"+{added} ", style=tui_rich_style("tool_diff_added") + RichStyle(bold=True))
    if removed > 0:
        header.append(
            f"-{removed} ", style=tui_rich_style("tool_diff_removed") + RichStyle(bold=True)
        )
    header.append(path)
    return header


# ---------------------------------------------------------------------------
# Public: collect hunks from DiffDisplayBlocks
# ---------------------------------------------------------------------------


def collect_diff_hunks(
    blocks: list[DiffDisplayBlock],
) -> tuple[list[list[DiffLine]], int, int]:
    """Build parsed DiffLine hunks and stats from a list of same-file DiffDisplayBlocks.

    Returns:
        (hunks, added_total, removed_total) where each hunk is a list of DiffLine.
    """
    all_hunks: list[list[DiffLine]] = []
    added = 0
    removed = 0
    for b in blocks:
        block_hunks = _build_diff_lines(
            b.old_text,
            b.new_text,
            b.old_start,
            b.new_start,
        )
        for hunk in block_hunks:
            for dl in hunk:
                if dl.kind == DiffLineKind.ADD:
                    added += 1
                elif dl.kind == DiffLineKind.DELETE:
                    removed += 1
            all_hunks.append(hunk)
    return all_hunks, added, removed


# ---------------------------------------------------------------------------
# Public: full diff panel (tool results & pager)
# ---------------------------------------------------------------------------


def render_diff_panel(
    path: str,
    hunks: list[list[DiffLine]],
    added: int,
    removed: int,
) -> RenderableType:
    """Render a diff as a bordered Panel with line numbers, background colors,
    syntax highlighting, and inline change markers."""
    title = Text()
    title.append(" ")
    title.append_text(_build_diff_header(path, added, removed))
    title.append(" ")

    highlighter = _make_highlighter(path)
    for hunk in hunks:
        _highlight_hunk(highlighter, hunk)

    # Compute line number column width
    max_ln = 0
    for hunk in hunks:
        for dl in hunk:
            max_ln = max(max_ln, dl.old_num, dl.new_num)
    num_width = max(len(str(max_ln)), DIFF_LINE_NUMBER_MIN_WIDTH)

    table = Table(
        show_header=False,
        box=None,
        padding=(0, 0),
        show_edge=False,
        expand=True,
    )
    table.add_column(justify="right", width=num_width, no_wrap=True)
    table.add_column(width=3, no_wrap=True)
    table.add_column(ratio=1)

    colors = get_diff_colors()
    for hunk_idx, hunk in enumerate(hunks):
        if hunk_idx > 0:
            table.add_row(Text("⋮", style="dim"), Text(""), Text(""))

        for dl in hunk:
            assert dl.content is not None
            if dl.kind == DiffLineKind.ADD:
                table.add_row(
                    Text(str(dl.new_num)),
                    Text(" + ", style=tui_rich_style("tool_diff_added")),
                    dl.content,
                    style=colors.add_bg,
                )
            elif dl.kind == DiffLineKind.DELETE:
                table.add_row(
                    Text(str(dl.old_num)),
                    Text(" - ", style=tui_rich_style("tool_diff_removed")),
                    dl.content,
                    style=colors.del_bg,
                )
            else:
                table.add_row(
                    Text(str(dl.new_num), style="dim"),
                    Text("   "),
                    dl.content,
                )

    return Panel(
        table,
        title=title,
        title_align="left",
        border_style=tui_rich_style("border_muted"),
        padding=(0, 1),
    )


# ---------------------------------------------------------------------------
# Public: compact preview (approval panels)
# ---------------------------------------------------------------------------


def render_diff_preview(
    path: str,
    hunks: list[list[DiffLine]],
    added: int,
    removed: int,
    max_lines: int = MAX_PREVIEW_CHANGED_LINES,
) -> tuple[list[RenderableType], int]:
    """Render a compact diff preview showing only changed lines (no context).

    Returns:
        (renderables, remaining_count) — list of Rich renderables and number of
        changed lines not shown.
    """
    highlighter = _make_highlighter(path)
    for hunk in hunks:
        _highlight_hunk(highlighter, hunk)

    # Collect only changed lines across all hunks
    changed: list[DiffLine] = []
    for hunk in hunks:
        for dl in hunk:
            if dl.kind != DiffLineKind.CONTEXT:
                changed.append(dl)

    total = len(changed)
    shown = changed[:max_lines]
    remaining = total - len(shown)

    # Compute line number width from shown lines
    max_ln = max(
        (dl.old_num if dl.kind == DiffLineKind.DELETE else dl.new_num for dl in shown),
        default=0,
    )
    num_width = max(len(str(max_ln)), DIFF_LINE_NUMBER_MIN_WIDTH)

    result: list[RenderableType] = [_build_diff_header(path, added, removed)]

    for dl in shown:
        assert dl.content is not None
        line = Text()
        ln = dl.old_num if dl.kind == DiffLineKind.DELETE else dl.new_num
        line.append(str(ln).rjust(num_width), style="dim")
        marker_style = tui_rich_style(
            "tool_diff_added" if dl.kind == DiffLineKind.ADD else "tool_diff_removed"
        )
        marker_char = "+" if dl.kind == DiffLineKind.ADD else "-"
        line.append(f" {marker_char} ", style=marker_style)
        line.append_text(dl.content)
        result.append(line)

    if remaining > 0:
        result.append(Text(_expand_hint(remaining), style="dim italic"))

    return result, remaining


# ---------------------------------------------------------------------------
# Public: summary renderers for huge files
# ---------------------------------------------------------------------------


def _summary_description(blocks: list[DiffDisplayBlock]) -> str:
    """Build a human-readable size description from summary blocks."""
    block = blocks[0]
    if block.old_text == "(0 lines)":
        return f"New file with {block.new_text.strip('()')}"
    if block.old_text == block.new_text:
        return block.old_text.strip("()")
    return f"{block.old_text.strip('()')} \u2192 {block.new_text.strip('()')}"


def render_diff_summary_panel(
    path: str,
    blocks: list[DiffDisplayBlock],
) -> RenderableType:
    """Render a summary panel for files too large for inline diff."""
    title = Text()
    title.append(" ")
    title.append(path)
    title.append(" ")

    body = Text()
    body.append("File too large for inline diff", style="dim italic")
    body.append("\n")
    body.append(_summary_description(blocks), style="dim")

    return Panel(
        body,
        title=title,
        title_align="left",
        border_style=tui_rich_style("border_muted"),
        padding=(1, 2),
    )


def render_diff_summary_preview(
    path: str,
    blocks: list[DiffDisplayBlock],
) -> list[RenderableType]:
    """Render a compact summary preview for approval panels."""
    header = Text()
    header.append(path)
    desc = Text()
    summary = _summary_description(blocks)
    desc.append(f"  File too large for inline diff ({summary})", style="dim italic")
    return [header, desc]
