"""Standardized report renderer.

One structured shape and one muted, roomy rendering for every report Pythinker
produces (code review, verify, security review, …). Reports reach the shell two
ways:

* Python callers build a :class:`Report` and call :func:`render_report`.
* Skills/agents emit a ```` ```report ```` fenced block of JSON; the shell
  splits it out of the surrounding markdown via :func:`render_agent_body` and
  renders it through the same path. A malformed block is never swallowed — it
  falls back to ordinary markdown (shown as a code block).

Styling reuses the existing theme tokens (:func:`tui_rich_style`), so the
"clear, not bright" palette and dark/light support come for free.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

if TYPE_CHECKING:
    from markdown_it import MarkdownIt

from rich import box
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.style import Style as RichStyle
from rich.table import Table
from rich.text import Text

from pythinker_code.ui.shell.components.report_update import (
    parse_report_update,
    render_report_update,
)
from pythinker_code.ui.shell.glyphs import REPORT_FILE_MARKER
from pythinker_code.ui.shell.markdown.audit import detect_audit_report
from pythinker_code.ui.shell.markdown.normalizers import (
    parse_aligned_field_line as _parse_aligned_field_line,
)
from pythinker_code.ui.shell.markdown.renderer import (
    pythinker_markdown,
    pythinker_report_markdown,
)
from pythinker_code.ui.shell.spacing import REPORT_PANEL_PADDING
from pythinker_code.ui.theme import ThemeName, get_tui_tokens, tui_rich_style

_log = logging.getLogger(__name__)


def _agent_markdown(text: str) -> RenderableType:
    """Render assistant markdown, using audit profile for dense parity reports."""
    if detect_audit_report(text):
        return pythinker_report_markdown(text, report_kind="audit")
    return pythinker_markdown(text)


__all__ = [
    "Report",
    "ReportFinding",
    "Severity",
    "has_report_block",
    "parse_report_block",
    "render_agent_body",
    "render_report",
]

Severity = Literal["critical", "high", "medium", "low", "info"]

# Most-severe first — drives both grouping order and the summary tally.
_SEVERITY_ORDER: tuple[Severity, ...] = get_args(Severity)
_SEVERITY_SET = frozenset(_SEVERITY_ORDER)

# severity -> (token name, bold). Report panels keep body text regular weight;
# only the panel title (H1 equivalent) uses bold.
_SEVERITY_TOKEN: dict[Severity, tuple[str, bool]] = {
    "critical": ("error", False),
    "high": ("error", False),
    "medium": ("warning", False),
    "low": ("accent", False),
    "info": ("activity_spinner", False),
}

_DOT = "●"


# A markdown-it parser is reused so report-fence extraction is fence-aware: a
# ```report block nested inside an outer fence is part of that outer fence's
# content and is therefore NOT a top-level fence token (Principle #5: parse,
# don't pattern-match).
_md_parser: MarkdownIt | None = None


def _get_report_parser() -> MarkdownIt:
    global _md_parser
    if _md_parser is None:
        from markdown_it import MarkdownIt

        _md_parser = MarkdownIt()
    return _md_parser


def _iter_report_payloads(text: str) -> list[tuple[int, int, str]]:
    """Yield (start_line, end_line, payload) for each TOP-LEVEL ```report fence.

    Line indices are 0-based half-open ([start, end)) into ``text``'s lines,
    matching markdown-it ``token.map``. Nested fences never appear as top-level
    ``fence`` tokens, so they are structurally excluded.
    """
    md = _get_report_parser()
    blocks: list[tuple[int, int, str]] = []
    for token in md.parse(text):
        if (
            token.type == "fence"
            and token.level == 0
            and token.map is not None
            and token.info.strip() == "report"
        ):
            blocks.append((token.map[0], token.map[1], token.content))
    return blocks


@dataclass(frozen=True, slots=True)
class ReportFinding:
    """One finding in a report."""

    title: str
    severity: Severity
    location: str | None = None  # e.g. "src/foo.py:42-58"
    body: str = ""  # markdown prose


@dataclass(frozen=True, slots=True)
class Report:
    """A standardized report. The summary tally is derived, never supplied."""

    title: str
    scope: str | None = None
    findings: tuple[ReportFinding, ...] = ()
    note: str | None = None  # closing "most actionable" line


@dataclass(frozen=True, slots=True)
class _ReportProseSection:
    """One top-level ``Label: body`` section in report-like assistant prose."""

    title: str
    body: str


@dataclass(frozen=True, slots=True)
class _ReportProse:
    preamble: str
    sections: tuple[_ReportProseSection, ...]


_REPORT_LABEL_RE = re.compile(
    r"""
    ^\s*
    (?:
        \*\*(?P<bold_colon>[^*\n]{3,120}?):\*\* |
        \*\*(?P<bold>[^*\n:]{3,120}?)\*\*: |
        (?P<plain>[^:\n|]{3,120}?):
    )
    \s*(?P<body>.*)$
    """,
    re.VERBOSE,
)
_FENCE_LINE_RE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})")


def _clean_report_label(line: str) -> tuple[str, str] | None:
    """Return ``(title, first_body)`` for a top-level report label line.

    This is deliberately conservative. It ignores lists, block quotes, tables,
    paths/URLs, and lowercase prose labels so normal chat paragraphs such as
    ``note: ...`` remain ordinary Markdown.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith(("- ", "* ", "+ ", ">", "|")):
        return None
    if _FENCE_LINE_RE.match(stripped):
        return None
    # Space-column parity/inventory rows (`Reference line      path:1-2`) are not
    # top-level report section labels; their path colons must not split prose.
    if line != line.lstrip() or _parse_aligned_field_line(line) is not None:
        return None

    match = _REPORT_LABEL_RE.match(line)
    if match is None:
        return None

    title = match.group("bold_colon") or match.group("bold") or match.group("plain") or ""
    title = title.strip()
    body = match.group("body").strip()
    if not title or "://" in title or title.startswith(("/", "./", "../", "~")):
        return None
    if not any(ch.isalpha() for ch in title):
        return None
    if not (line.lstrip().startswith("**") or title[0].isupper()):
        return None
    # Avoid turning whole sentences into fake headings. Report labels are short
    # phrases: "Exit codes", "Residual unknowns", "Next step suggestion", etc.
    if len(title.split()) > 12:
        return None
    return title, body


def _parse_report_prose(text: str) -> _ReportProse | None:
    """Parse dense final-answer prose into top-level report sections.

    LLMs often produce report summaries as adjacent ``**Label:** body`` lines.
    Markdown renders those as crammed paragraphs. When there are multiple such
    labels, treat them as sections with real vertical rhythm and body indentation.
    """
    lines = text.splitlines()
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    in_fence = False
    fence_char = ""
    fence_len = 0

    def target_lines() -> list[str]:
        return preamble if current is None else current[1]

    for line in lines:
        fence_match = _FENCE_LINE_RE.match(line)
        if in_fence:
            target_lines().append(line)
            if fence_match is not None:
                fence = fence_match.group("fence")
                if fence.startswith(fence_char) and len(fence) >= fence_len:
                    in_fence = False
                    fence_char = ""
                    fence_len = 0
            continue
        if fence_match is not None:
            fence = fence_match.group("fence")
            in_fence = True
            fence_char = fence[0]
            fence_len = len(fence)
            target_lines().append(line)
            continue

        label = _clean_report_label(line)
        if label is not None:
            title, first_body = label
            body_lines = [first_body] if first_body else []
            current = (title, body_lines)
            sections.append(current)
            continue
        target_lines().append(line)

    if len(sections) < 2:
        return None
    return _ReportProse(
        preamble="\n".join(preamble).strip("\n"),
        sections=tuple(
            _ReportProseSection(title=title, body="\n".join(body).strip("\n"))
            for title, body in sections
        ),
    )


def _render_report_prose(text: str, *, theme: ThemeName | None = None) -> RenderableType | None:
    report = _parse_report_prose(text)
    if report is None:
        return None

    rows: list[RenderableType] = []
    if report.preamble.strip():
        rows.append(pythinker_report_markdown(report.preamble))

    body_style = tui_rich_style("text", theme=theme)
    for section in report.sections:
        if rows:
            rows.append(Text(""))
        rows.append(pythinker_report_markdown(f"# {section.title}"))
        if section.body.strip():
            rows.append(
                Padding(
                    pythinker_report_markdown(section.body.strip(), style=body_style),
                    (0, 0, 0, 2),
                )
            )

    return Group(*rows)


def _counts(findings: tuple[ReportFinding, ...]) -> dict[Severity, int]:
    counts: dict[Severity, int] = dict.fromkeys(_SEVERITY_ORDER, 0)
    for finding in findings:
        counts[finding.severity] += 1
    return counts


def _severity_style(severity: Severity, theme: ThemeName | None) -> RichStyle:
    token, bold = _SEVERITY_TOKEN[severity]
    style = tui_rich_style(token, theme=theme)
    return style + RichStyle(bold=True) if bold else style


def _strong_style(theme: ThemeName | None) -> RichStyle:
    return tui_rich_style("tool_title", theme=theme) + RichStyle(bold=True)


def _primary_style(theme: ThemeName | None) -> RichStyle:
    return tui_rich_style("text", theme=theme)


def _secondary_style(theme: ThemeName | None) -> RichStyle:
    return tui_rich_style("secondary", theme=theme)


def _muted_style(theme: ThemeName | None) -> RichStyle:
    return tui_rich_style("muted", theme=theme)


def _summary_line(counts: dict[Severity, int], theme: ThemeName | None) -> Text:
    line = Text()
    pill_bg = get_tui_tokens(theme).tool_pending_bg
    bg = RichStyle(bgcolor=pill_bg)
    first = True
    for severity in _SEVERITY_ORDER:
        count = counts[severity]
        if not count:
            continue
        if not first:
            line.append("  ")
        first = False
        line.append(f" {_DOT} ", style=_severity_style(severity, theme) + bg)
        line.append(f"{count} {severity} ", style=_primary_style(theme) + bg)
    if not counts["critical"] and not counts["high"]:
        prefix = "  " if not first else ""
        line.append(f"{prefix}no critical or high", style=_secondary_style(theme))
    return line


def _render_section_header(severity: Severity, theme: ThemeName | None) -> Group:
    border = tui_rich_style("border", theme=theme)
    return Group(
        Text(severity.capitalize(), style=tui_rich_style("tool_title", theme=theme)),
        Rule(style=border, characters="─"),
    )


def _render_finding(finding: ReportFinding, theme: ThemeName | None) -> RenderableType:
    rows: list[RenderableType] = []

    # Hang-indent the title: the ● marker sits alone in a 2-wide gutter and the
    # title text (and its wrapped lines) align at column 2 — the same column as
    # the finding's location/body below. A flat ``Text`` wraps back under the
    # marker, which flattened the hierarchy and made a wrapped title read like a
    # new finding.
    title = Table.grid(padding=0)
    title.add_column(width=2, no_wrap=True)
    title.add_column(overflow="fold")
    title.add_row(
        Text(_DOT, style=_severity_style(finding.severity, theme)),
        Text(finding.title, style=_primary_style(theme)),
    )
    rows.append(title)

    if finding.location:
        rows.append(Text(""))
        muted = _muted_style(theme)
        location = Table.grid(padding=0)
        location.add_column(width=2, no_wrap=True)
        location.add_column(overflow="fold")
        location.add_row(
            Text(REPORT_FILE_MARKER, style=muted),
            Text(finding.location, style=muted),
        )
        rows.append(location)

    if finding.body.strip():
        body_style = _primary_style(theme)
        rows.append(
            Padding(
                pythinker_report_markdown(finding.body.strip(), style=body_style),
                (0, 0, 0, 2),
            )
        )

    return Group(*rows)


def render_report(report: Report, *, theme: ThemeName | None = None) -> RenderableType:
    """Render *report* as a padded, syntax-friendly Rich report panel."""
    counts = _counts(report.findings)
    border = tui_rich_style("border", theme=theme)
    blank = Text("")

    rows: list[RenderableType] = []
    if report.scope:
        rows += [Text(report.scope, style=_secondary_style(theme)), blank]
    rows.append(_summary_line(counts, theme))

    for severity in _SEVERITY_ORDER:
        group = [f for f in report.findings if f.severity == severity]
        if not group:
            continue
        rows.append(blank)
        rows.append(_render_section_header(severity, theme))
        for finding in group:
            rows.append(blank)
            rows.append(_render_finding(finding, theme))

    if report.note:
        rows += [
            blank,
            Rule(style=border, characters="─"),
            Text(report.note, style=_secondary_style(theme)),
        ]

    title = Text(report.title, style=_strong_style(theme))
    return Panel(
        Group(*rows),
        title=title,
        title_align="left",
        border_style=border,
        box=box.ROUNDED,
        padding=REPORT_PANEL_PADDING,
        expand=True,
    )


def parse_report_block(payload: str) -> Report | None:
    """Deserialize a ```` ```report ```` block's JSON into a :class:`Report`.

    Returns ``None`` on any malformed payload so callers can fall back to
    rendering the raw text — a bad block must never be swallowed.
    """
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError) as exc:
        _log.debug(
            "parse_report_block: JSON decode failed (type=%s len=%d)",
            type(payload).__name__,
            len(payload),
            exc_info=exc,
        )
        return None
    if not isinstance(parsed, dict):
        return None
    data = cast(dict[str, Any], parsed)

    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return None

    scope = data.get("scope")
    scope = scope if isinstance(scope, str) and scope.strip() else None
    note = data.get("note")
    note = note if isinstance(note, str) and note.strip() else None

    raw_findings = data.get("findings")
    if raw_findings is not None and not isinstance(raw_findings, list):
        return None

    findings: list[ReportFinding] = []
    for raw in cast("list[Any]", raw_findings or []):
        if not isinstance(raw, dict):
            return None
        entry = cast(dict[str, Any], raw)
        f_title = entry.get("title")
        severity = entry.get("severity")
        if not isinstance(f_title, str) or not f_title.strip():
            return None
        if severity not in _SEVERITY_SET:
            return None
        location = entry.get("location")
        location = location if isinstance(location, str) and location.strip() else None
        body = entry.get("body")
        body = body if isinstance(body, str) else ""
        findings.append(
            ReportFinding(title=f_title, severity=severity, location=location, body=body)
        )

    return Report(title=title, scope=scope, findings=tuple(findings), note=note)


def has_report_block(text: str) -> bool:
    """Whether *text* contains at least one well-formed top-level ` ```report ` block.

    Used by output surfaces (e.g. the headless final-text printer) to decide
    whether to route through :func:`render_agent_body` instead of emitting the
    raw text. Only matches blocks that actually parse, so a malformed fence
    leaves output unchanged. A ` ```report ` example nested inside an outer
    documentation fence is not a top-level fence token, so it is not matched.
    """
    return any(
        parse_report_block(payload) is not None for _, _, payload in _iter_report_payloads(text)
    )


def render_agent_body(text: str, *, theme: ThemeName | None = None) -> RenderableType:
    """Render assistant text, promoting top-level ` ```report ` blocks to reports.

    Non-report text renders via :func:`pythinker_markdown`; a valid top-level
    report block renders via :func:`render_report`; an invalid or nested block is
    left in place so the surrounding markdown shows it as an ordinary code block.
    """
    # Split on "\n" only (NOT str.splitlines, which also breaks on \f, \v, \x85,
    #  ,  ): markdown-it's token.map counts only "\n", so any other
    # split character would shift our line indices out of sync with the parser
    # and leak fence delimiters into the surrounding prose.
    lines = text.split("\n")
    segments: list[RenderableType] = []
    cursor = 0  # line index
    for start, end, payload in _iter_report_payloads(text):
        report = parse_report_block(payload)
        if report is None:
            continue  # malformed — leave it for the markdown renderer
        before = "\n".join(lines[cursor:start]).strip("\n")
        if before:
            segments.append(_agent_markdown(before))
        segments.append(render_report(report, theme=theme))
        cursor = end

    if not segments:
        report_update = parse_report_update(text)
        if report_update is not None:
            return render_report_update(report_update, theme=theme)
        report_prose = _render_report_prose(text, theme=theme)
        if report_prose is not None:
            return report_prose
        return _agent_markdown(text)

    rest = "\n".join(lines[cursor:]).strip("\n")
    if rest:
        segments.append(_agent_markdown(rest))

    spaced: list[RenderableType] = []
    for i, segment in enumerate(segments):
        if i:
            spaced.append(Text(""))
        spaced.append(segment)
    return Group(*spaced)
