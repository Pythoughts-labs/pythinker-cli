"""Structured parser/renderer for agent report prose: parent bullet + aligned fields."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.style import Style as RichStyle
from rich.table import Table
from rich.text import Text

from pythinker_code.ui.shell.markdown.audit import detect_audit_report
from pythinker_code.ui.shell.markdown.fences import FENCE_RE, FenceState
from pythinker_code.ui.shell.markdown.normalizers import (
    is_field_continuation_line,
    is_known_field_label,
    parse_aligned_field_line,
)
from pythinker_code.ui.shell.markdown.renderer import pythinker_markdown, pythinker_report_markdown
from pythinker_code.ui.theme import ThemeName, tui_rich_style

__all__ = [
    "AlignedFieldRow",
    "AlignedFindingBlock",
    "ReportSectionHeading",
    "render_report_prose_blocks",
    "split_report_prose",
]

_DOT = "●"
_PARENT_BULLET_RE = re.compile(r"^(\s*)[-•]\s+(.+)$")
_FENCE_LINE_RE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})")
_UNICODE_HEADING_RULE_RE = re.compile(r"^\s*[═─━]{3,}\s*$")

ChunkKind = Literal["markdown", "finding_block", "section_heading"]


@dataclass(frozen=True, slots=True)
class AlignedFieldRow:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class AlignedFindingBlock:
    title: str
    fields: tuple[AlignedFieldRow, ...]


@dataclass(frozen=True, slots=True)
class ReportSectionHeading:
    text: str


@dataclass(frozen=True, slots=True)
class ProseChunk:
    kind: ChunkKind
    text: str = ""
    finding: AlignedFindingBlock | None = None
    heading: ReportSectionHeading | None = None


ParsedFindingBlock = tuple[AlignedFindingBlock, int]


def parse_parent_bullet(line: str) -> tuple[str, str] | None:
    """Return ``(indent, title)`` for ``• title`` or ``- title`` parent bullets."""
    stripped = line.rstrip("\r\n")
    match = _PARENT_BULLET_RE.match(stripped)
    if match is None:
        return None
    indent, title = match.group(1), match.group(2).strip()
    if not title:
        return None
    return indent, title


def looks_like_report_section_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if parse_parent_bullet(line) is not None:
        return False
    if parse_aligned_field_line(line) is not None:
        return False
    if stripped.startswith(("-", "*", "+", "#", "|", ">", "`", "•")):
        return False
    if _FENCE_LINE_RE.match(stripped):
        return False
    if len(stripped.split()) > 12:
        return False
    if stripped[-1] in ".,;:":
        return False
    return stripped[0].isupper() or "/" in stripped


def _try_parse_aligned_finding_block(lines: list[str], start: int) -> ParsedFindingBlock | None:
    parent = parse_parent_bullet(lines[start])
    if parent is None:
        return None

    _, title = parent
    fields: list[AlignedFieldRow] = []
    index = start + 1

    while index < len(lines):
        body = lines[index].rstrip("\r\n")
        if not body.strip():
            break

        field = parse_aligned_field_line(body)
        if field is None:
            if fields and is_field_continuation_line(body):
                last = fields[-1]
                fields[-1] = AlignedFieldRow(last.label, f"{last.value} {body.strip()}")
                index += 1
                continue
            break

        _, label, value = field
        if not is_known_field_label(label):
            break

        fields.append(AlignedFieldRow(label=label, value=value))
        index += 1

        while index < len(lines) and is_field_continuation_line(lines[index].rstrip("\r\n")):
            last = fields[-1]
            fields[-1] = AlignedFieldRow(
                last.label,
                f"{last.value} {lines[index].rstrip().strip()}",
            )
            index += 1

    if len(fields) < 2:
        return None

    return AlignedFindingBlock(title=title, fields=tuple(fields)), index


def split_report_prose(text: str) -> list[ProseChunk]:
    """Split assistant prose into markdown spans, finding blocks, and section headings."""
    lines = text.split("\n")
    chunks: list[ProseChunk] = []
    markdown_buf: list[str] = []
    state = FenceState()
    index = 0

    def flush_markdown() -> None:
        if not markdown_buf:
            return
        body = "\n".join(markdown_buf).strip("\n")
        markdown_buf.clear()
        if body:
            chunks.append(ProseChunk(kind="markdown", text=body))

    while index < len(lines):
        line = lines[index]
        body = line.rstrip("\r\n")

        if state.active:
            markdown_buf.append(line)
            state.feed(body)
            index += 1
            continue

        fence_match = FENCE_RE.match(body)
        if fence_match is not None:
            state.feed(body)
            markdown_buf.append(line)
            index += 1
            continue

        finding = _try_parse_aligned_finding_block(lines, index)
        if finding is not None:
            flush_markdown()
            block, next_index = finding
            chunks.append(ProseChunk(kind="finding_block", finding=block))
            index = next_index
            continue

        # Unicode underlined heading: "Title\n═════" — detect before blank-line guard
        stripped = body.strip()
        if (
            stripped
            and index + 1 < len(lines)
            and not stripped.startswith(("-", "*", "+", "#", "|", ">", "`", "•"))
            and _UNICODE_HEADING_RULE_RE.match(lines[index + 1].rstrip("\r\n"))
        ):
            flush_markdown()
            chunks.append(
                ProseChunk(kind="section_heading", heading=ReportSectionHeading(text=stripped))
            )
            index += 2  # consume heading line + rule line
            continue

        # TL;DR is always a heading regardless of preceding blank
        if stripped.upper() in ("TL;DR", "TLDR"):
            flush_markdown()
            chunks.append(
                ProseChunk(kind="section_heading", heading=ReportSectionHeading(text=stripped))
            )
            index += 1
            continue

        prev_blank = index == 0 or not lines[index - 1].strip()
        if prev_blank and looks_like_report_section_heading(line):
            flush_markdown()
            chunks.append(
                ProseChunk(
                    kind="section_heading",
                    heading=ReportSectionHeading(text=body.strip()),
                )
            )
            index += 1
            continue

        markdown_buf.append(line)
        index += 1

    flush_markdown()
    return chunks


def _primary_style(theme: ThemeName | None) -> RichStyle:
    return tui_rich_style("text", theme=theme)


def _label_style(theme: ThemeName | None) -> RichStyle:
    return tui_rich_style("secondary", theme=theme)


def render_aligned_finding_block(
    block: AlignedFindingBlock,
    *,
    theme: ThemeName | None = None,
) -> RenderableType:
    """Render a parent bullet with per-block aligned field rows."""
    rows: list[RenderableType] = []
    primary = _primary_style(theme)
    label_style = _label_style(theme)

    title = Table.grid(padding=0)
    title.add_column(width=2, no_wrap=True)
    title.add_column(overflow="fold")
    title.add_row(Text(_DOT, style=primary), Text(block.title, style=primary))
    rows.append(title)

    label_width = max(len(field.label) for field in block.fields)

    for field in block.fields:
        label_cell = f"  {field.label.ljust(label_width + 2)}"
        field_row = Table.grid(padding=0)
        field_row.add_column(width=2, no_wrap=True)
        field_row.add_column(no_wrap=True)
        field_row.add_column(overflow="fold")
        field_row.add_row(
            Text(""),
            Text(label_cell, style=label_style),
            Text(field.value, style=primary),
        )
        rows.append(field_row)

    return Group(*rows)


def render_section_heading(
    heading: ReportSectionHeading,
    *,
    theme: ThemeName | None = None,
) -> RenderableType:
    border = tui_rich_style("border", theme=theme)
    title_style = tui_rich_style("tool_title", theme=theme)
    rule_width = max(4, cell_len(heading.text))
    return Group(
        Text(heading.text, style=title_style),
        Text("─" * rule_width, style=border),
    )


def _agent_markdown_chunk(text: str) -> RenderableType:
    if detect_audit_report(text):
        return pythinker_report_markdown(text, report_kind="audit")
    return pythinker_markdown(text)


def render_report_prose_blocks(
    text: str,
    *,
    theme: ThemeName | None = None,
) -> RenderableType | None:
    """Render prose with aligned finding blocks; ``None`` when no blocks detected."""
    chunks = split_report_prose(text)
    if not any(chunk.kind == "finding_block" for chunk in chunks):
        return None

    segments: list[RenderableType] = []
    for chunk in chunks:
        if chunk.kind == "finding_block" and chunk.finding is not None:
            segments.append(render_aligned_finding_block(chunk.finding, theme=theme))
        elif chunk.kind == "section_heading" and chunk.heading is not None:
            segments.append(render_section_heading(chunk.heading, theme=theme))
        elif chunk.kind == "markdown" and chunk.text.strip():
            segments.append(_agent_markdown_chunk(chunk.text))

    if not segments:
        return None

    spaced: list[RenderableType] = []
    for index, segment in enumerate(segments):
        if index:
            spaced.append(Text(""))
        spaced.append(segment)
    return Group(*spaced)
