"""Structured renderer for agent report-update completion messages.

Agents often finish report-editing work with dense prose: file lists, numbered
corrections with ``Severity / Item / Fix`` field blocks, and follow-up flags.
That shape is fine for chat transcripts but wastes terminal height when rendered
as generic Markdown. This module parses the common layout and renders compact
summary cards with an expandable correction ledger (``ctrl+o``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, get_args

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from pythinker_code.ui.shell.components.key_hints import key_hint
from pythinker_code.ui.shell.markdown.audit import compact_known_paths
from pythinker_code.ui.shell.markdown.normalizers import parse_aligned_field_line
from pythinker_code.ui.shell.spacing import REPORT_PANEL_PADDING
from pythinker_code.ui.theme import ThemeName, tui_rich_style

__all__ = [
    "ChangedFile",
    "Correction",
    "ReportUpdate",
    "ReportUpdateComponent",
    "looks_like_report_update",
    "parse_report_update",
    "render_report_update",
]

SeverityLabel = Literal["critical", "high", "med", "low", "info"]
_SEVERITY_ORDER: tuple[SeverityLabel, ...] = get_args(SeverityLabel)

_BULLET_RE = re.compile(r"^\s*[-•]\s+(.+)$")
_SECTION_FILES_RE = re.compile(r"^files?\s+modified\s*:?\s*$", re.I)
_SECTION_CORRECTIONS_RE = re.compile(r"^corrections?\s+applied\s*:?\s*$", re.I)
_SECTION_FOLLOWUPS_RE = re.compile(
    r"^(?:out[- ]of[- ]scope\s+flags?|follow[- ]ups?)\s*(?:\([^)]*\))?\s*:?\s*$",
    re.I,
)
_REPORT_HEADER_RE = re.compile(
    r"^report\s+update\s+complete(?:\.\s*(?P<subtitle>.+))?$",
    re.I,
)
_FILE_MODIFIED_RE = re.compile(
    r"^(?P<path>.+?)\s*\((?P<detail>\d+\s*→\s*\d+\s*lines?)\)\s*$",
    re.I,
)
_FILE_CREATED_RE = re.compile(
    r"^(?P<path>.+?)\s*\((?:new,\s*)?(?P<detail>\d+\s*lines?)\)\s*$",
    re.I,
)
_BRANCH_COMMIT_RE = re.compile(
    r"(?P<branch>[\w./-]+)\s*@\s*(?P<commit>[0-9a-f]{6,40})\b",
    re.I,
)
_COMPACT_CORRECTION_RE = re.compile(
    r"^(?P<sev>critical|crit|high|med|medium|low|info)\s+"
    r"(?P<num>\d+)\s+"
    r"(?P<item>.+?)(?:\s+[—–-]\s+(?P<fix>.+))?$",
    re.I,
)

_SEV_DISPLAY: dict[SeverityLabel, str] = {
    "critical": "Crit",
    "high": "High",
    "med": "Med",
    "low": "Low",
    "info": "Info",
}

_SEV_ALIASES: dict[str, SeverityLabel] = {
    "critical": "critical",
    "crit": "critical",
    "high": "high",
    "med": "med",
    "medium": "med",
    "low": "low",
    "info": "info",
}


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    kind: Literal["modified", "created"]
    detail: str


@dataclass(frozen=True, slots=True)
class Correction:
    number: int
    severity: SeverityLabel
    item: str
    fix: str


@dataclass(frozen=True, slots=True)
class ReportUpdate:
    title: str
    subtitle: str | None
    report_name: str | None
    files: tuple[ChangedFile, ...]
    corrections: tuple[Correction, ...]
    followups: tuple[str, ...]
    branch: str | None
    commit: str | None
    method: str | None
    scope: str | None


def looks_like_report_update(text: str) -> bool:
    """Whether *text* is building toward a report-update completion message."""
    return "report update complete" in text.lower()


def _normalize_severity(raw: str) -> SeverityLabel | None:
    return _SEV_ALIASES.get(raw.strip().lower())


def _short_path(path: str) -> str:
    compact = compact_known_paths(path.strip())
    for prefix in (".pythinker/reports/", "reports/"):
        if compact.startswith(prefix):
            compact = compact[len(prefix) :]
    return compact


def _parse_file_bullet(body: str) -> ChangedFile | None:
    body = body.strip()
    match = _FILE_MODIFIED_RE.match(body)
    if match is not None:
        return ChangedFile(
            path=match.group("path").strip(),
            kind="modified",
            detail=match.group("detail").strip(),
        )
    match = _FILE_CREATED_RE.match(body)
    if match is not None:
        detail = match.group("detail").strip()
        if not detail.lower().startswith("new"):
            detail = f"new, {detail}"
        return ChangedFile(
            path=match.group("path").strip(),
            kind="created",
            detail=detail,
        )
    return None


def _parse_branch_commit(text: str) -> tuple[str | None, str | None]:
    match = _BRANCH_COMMIT_RE.search(text)
    if match is None:
        return None, None
    return match.group("branch"), match.group("commit")


def _infer_scope(files: tuple[ChangedFile, ...], text: str) -> str | None:
    lower = text.lower()
    if "no source files modified" in lower or "report-only" in lower or "reports only" in lower:
        return "reports only; no source files changed"
    if files and all(
        ".pythinker/reports/" in f.path or f.path.startswith("reports/") for f in files
    ):
        return "reports only; no source files changed"
    return None


def _infer_report_name(text: str, files: tuple[ChangedFile, ...]) -> str | None:
    for raw in text.splitlines():
        if "verification memo" in raw.lower() and "—" in raw:
            _, _, tail = raw.partition("—")
            name = tail.strip()
            if name:
                return name
    for file in files:
        base = _short_path(file.path)
        if base.endswith(".md"):
            return base.removesuffix(".md").replace("-", " ").title()
    return None


def parse_report_update(text: str) -> ReportUpdate | None:
    """Parse a report-update completion message into structured data."""
    if not looks_like_report_update(text):
        return None

    lines = text.splitlines()
    title = "Report update complete"
    subtitle: str | None = None
    files: list[ChangedFile] = []
    corrections: list[Correction] = []
    followups: list[str] = []
    method: str | None = None

    section: str | None = None
    pending_correction: dict[str, str] | None = None
    pending_number: int | None = None
    pending_fix_lines: list[str] = []

    def flush_correction() -> None:
        nonlocal pending_correction, pending_number, pending_fix_lines
        if pending_correction is None or pending_number is None:
            pending_correction = None
            pending_number = None
            pending_fix_lines = []
            return
        severity_raw = pending_correction.get("severity", pending_correction.get("Severity", ""))
        item = pending_correction.get("item", pending_correction.get("Item", "")).strip()
        fix = pending_correction.get("fix", pending_correction.get("Fix", "")).strip()
        if pending_fix_lines:
            extra = " ".join(pending_fix_lines).strip()
            fix = f"{fix} {extra}".strip() if fix else extra
        severity = _normalize_severity(severity_raw)
        if severity and item:
            corrections.append(
                Correction(number=pending_number, severity=severity, item=item, fix=fix)
            )
        pending_correction = None
        pending_number = None
        pending_fix_lines = []

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if pending_fix_lines and pending_correction is not None:
                pending_fix_lines.append("")
            continue

        header_match = _REPORT_HEADER_RE.match(stripped)
        if header_match is not None:
            subtitle = header_match.group("subtitle")
            continue

        if _SECTION_FILES_RE.match(stripped):
            flush_correction()
            section = "files"
            continue
        if _SECTION_CORRECTIONS_RE.match(stripped):
            flush_correction()
            section = "corrections"
            continue
        if _SECTION_FOLLOWUPS_RE.match(stripped):
            flush_correction()
            section = "followups"
            continue

        if stripped.lower().startswith("method:"):
            flush_correction()
            section = "method"
            method = stripped.split(":", 1)[1].strip() or None
            continue

        bullet = _BULLET_RE.match(line)
        if bullet is not None:
            body = bullet.group(1).strip()
            if section == "files":
                parsed_file = _parse_file_bullet(body)
                if parsed_file is not None:
                    files.append(parsed_file)
                continue
            if section == "followups":
                followups.append(body)
                continue
            if section == "corrections":
                compact = _COMPACT_CORRECTION_RE.match(body)
                if compact is not None:
                    flush_correction()
                    severity = _normalize_severity(compact.group("sev"))
                    if severity is None:
                        continue
                    corrections.append(
                        Correction(
                            number=int(compact.group("num")),
                            severity=severity,
                            item=compact.group("item").strip(),
                            fix=(compact.group("fix") or "").strip(),
                        )
                    )
                    continue
                if body.isdigit():
                    flush_correction()
                    pending_number = int(body)
                    pending_correction = {}
                    pending_fix_lines = []
                    continue
                continue

        field = parse_aligned_field_line(line)
        if field is not None and section == "corrections" and pending_correction is not None:
            _, label, value = field
            pending_correction[label.lower()] = value
            continue

        if section == "corrections" and pending_correction is not None and line.startswith("  "):
            pending_fix_lines.append(stripped)
            continue

        if section == "method" and method is not None:
            method = f"{method} {stripped}".strip()

    flush_correction()

    if not files and not corrections:
        return None

    branch, commit = _parse_branch_commit(text)
    scope = _infer_scope(tuple(files), text)
    report_name = _infer_report_name(text, tuple(files))

    return ReportUpdate(
        title=title,
        subtitle=subtitle,
        report_name=report_name,
        files=tuple(files),
        corrections=tuple(corrections),
        followups=tuple(followups),
        branch=branch,
        commit=commit,
        method=method,
        scope=scope,
    )


def _label(theme: ThemeName | None, text: str) -> Text:
    out = Text()
    out.append(text, style=tui_rich_style("tool_title", theme=theme))
    return out


def _kv_row(theme: ThemeName | None, key: str, value: str) -> RenderableType:
    row = Table.grid(padding=0)
    row.add_column(width=10, no_wrap=True)
    row.add_column(overflow="fold")
    row.add_row(_label(theme, key), Text(value, style=tui_rich_style("text", theme=theme)))
    return row


def _severity_style(severity: SeverityLabel, theme: ThemeName | None):
    token = {
        "critical": "error",
        "high": "error",
        "med": "warning",
        "low": "accent",
        "info": "activity_spinner",
    }[severity]
    return tui_rich_style(token, theme=theme)


def _group_summary_line(
    severity: SeverityLabel,
    items: list[Correction],
    theme: ThemeName | None,
) -> RenderableType:
    row = Table.grid(padding=0)
    row.add_column(width=6, no_wrap=True)
    row.add_column(width=4, no_wrap=True)
    row.add_column(overflow="fold")
    if len(items) == 1:
        item = items[0]
        detail = item.item
        if item.fix:
            detail = f"{item.item} — {item.fix}"
    else:
        detail = "; ".join(item.item for item in items[:3])
        if len(items) > 3:
            detail = f"{detail}; …"
    row.add_row(
        Text(_SEV_DISPLAY[severity], style=_severity_style(severity, theme)),
        Text(str(len(items)), style=tui_rich_style("text", theme=theme)),
        Text(detail, style=tui_rich_style("text", theme=theme)),
    )
    return row


def _render_summary_panel(update: ReportUpdate, *, theme: ThemeName | None) -> Panel:
    rows: list[RenderableType] = []
    if update.report_name:
        rows.append(_kv_row(theme, "Report", update.report_name))
    parts: list[str] = []
    if update.files:
        noun = "file" if len(update.files) == 1 else "files"
        parts.append(f"{len(update.files)} {noun} updated")
    if update.corrections:
        noun = "correction" if len(update.corrections) == 1 else "corrections"
        parts.append(f"{len(update.corrections)} {noun} applied")
    if update.followups:
        noun = "follow-up" if len(update.followups) == 1 else "follow-ups"
        parts.append(f"{len(update.followups)} {noun}")
    if parts:
        rows.append(_kv_row(theme, "Result", " · ".join(parts)))
    if update.scope:
        rows.append(_kv_row(theme, "Scope", update.scope))
    if update.method:
        rows.append(_kv_row(theme, "Method", update.method))
    if update.branch and update.commit:
        rows.append(_kv_row(theme, "Verified", f"{update.branch} @ {update.commit}"))
    elif update.branch:
        rows.append(_kv_row(theme, "Branch", update.branch))
    border = tui_rich_style("border", theme=theme)
    title = Text("Summary", style=tui_rich_style("tool_title", theme=theme))
    return Panel(
        Group(*rows),
        title=title,
        title_align="left",
        border_style=border,
        box=box.ROUNDED,
        padding=REPORT_PANEL_PADDING,
        expand=True,
    )


def _render_files_panel(update: ReportUpdate, *, theme: ThemeName | None) -> Panel | None:
    if not update.files:
        return None
    table = Table.grid(padding=(0, 1))
    table.add_column(width=2, no_wrap=True)
    table.add_column(ratio=1, overflow="fold")
    table.add_column(no_wrap=True, justify="right")
    for file in update.files:
        marker = "~" if file.kind == "modified" else "+"
        table.add_row(
            Text(marker, style=tui_rich_style("accent", theme=theme)),
            Text(_short_path(file.path), style=tui_rich_style("text", theme=theme)),
            Text(file.detail, style=tui_rich_style("muted", theme=theme)),
        )
    border = tui_rich_style("border", theme=theme)
    return Panel(
        table,
        title=Text("Changes", style=tui_rich_style("tool_title", theme=theme)),
        title_align="left",
        border_style=border,
        box=box.ROUNDED,
        padding=REPORT_PANEL_PADDING,
        expand=True,
    )


def _render_corrections_panel(
    update: ReportUpdate,
    *,
    theme: ThemeName | None,
    expanded: bool,
) -> Panel | None:
    if not update.corrections:
        return None
    rows: list[RenderableType] = []
    if expanded:
        table = Table.grid(padding=(0, 1))
        table.add_column(width=4, no_wrap=True)
        table.add_column(width=6, no_wrap=True)
        table.add_column(ratio=2, overflow="fold")
        table.add_column(ratio=3, overflow="fold")
        table.add_row(
            _label(theme, "#"),
            _label(theme, "Sev"),
            _label(theme, "Item"),
            _label(theme, "Fix"),
        )
        for correction in update.corrections:
            sev_style = _severity_style(correction.severity, theme)
            table.add_row(
                Text(str(correction.number), style=tui_rich_style("muted", theme=theme)),
                Text(_SEV_DISPLAY[correction.severity], style=sev_style),
                Text(correction.item, style=tui_rich_style("text", theme=theme)),
                Text(correction.fix, style=tui_rich_style("text", theme=theme)),
            )
        rows.append(table)
    else:
        grouped: dict[SeverityLabel, list[Correction]] = {s: [] for s in _SEVERITY_ORDER}
        for correction in update.corrections:
            grouped[correction.severity].append(correction)
        for severity in _SEVERITY_ORDER:
            items = grouped[severity]
            if not items:
                continue
            rows.append(_group_summary_line(severity, items, theme))
        rows.append(Text(""))
        expand_hint = key_hint(
            "app.tools.expand",
            f"expand all {len(update.corrections)} corrections",
        )
        rows.append(expand_hint)
    border = tui_rich_style("border", theme=theme)
    panel_title = "Correction summary" if not expanded else "Corrections applied"
    return Panel(
        Group(*rows),
        title=Text(panel_title, style=tui_rich_style("tool_title", theme=theme)),
        title_align="left",
        border_style=border,
        box=box.ROUNDED,
        padding=REPORT_PANEL_PADDING,
        expand=True,
    )


def _render_followups_panel(update: ReportUpdate, *, theme: ThemeName | None) -> Panel | None:
    if not update.followups:
        return None
    table = Table.grid(padding=(0, 1))
    table.add_column(width=2, no_wrap=True)
    table.add_column(overflow="fold")
    for item in update.followups:
        table.add_row(
            Text("!", style=tui_rich_style("warning", theme=theme)),
            Text(item, style=tui_rich_style("text", theme=theme)),
        )
    border = tui_rich_style("border", theme=theme)
    return Panel(
        table,
        title=Text("Follow-ups", style=tui_rich_style("tool_title", theme=theme)),
        title_align="left",
        border_style=border,
        box=box.ROUNDED,
        padding=REPORT_PANEL_PADDING,
        expand=True,
    )


def render_report_update(
    update: ReportUpdate,
    *,
    theme: ThemeName | None = None,
    expanded: bool = False,
) -> RenderableType:
    """Render *update* as stacked summary cards."""
    header = Text("✓ ", style=tui_rich_style("success", theme=theme))
    header.append(update.title, style=tui_rich_style("tool_title", theme=theme))
    rows: list[RenderableType] = [header, Text("")]
    rows.append(_render_summary_panel(update, theme=theme))
    files_panel = _render_files_panel(update, theme=theme)
    if files_panel is not None:
        rows.extend([Text(""), files_panel])
    corrections_panel = _render_corrections_panel(update, theme=theme, expanded=expanded)
    if corrections_panel is not None:
        rows.extend([Text(""), corrections_panel])
    followups_panel = _render_followups_panel(update, theme=theme)
    if followups_panel is not None:
        rows.extend([Text(""), followups_panel])
    return Group(*rows)


class ReportUpdateComponent:
    """Expandable report-update card for the streaming content block."""

    def __init__(self, update: ReportUpdate, *, theme: ThemeName | None = None) -> None:
        self._update = update
        self._theme: ThemeName | None = theme
        self._expanded = False

    @property
    def expanded(self) -> bool:
        return self._expanded

    @property
    def can_expand(self) -> bool:
        return len(self._update.corrections) > 0

    def toggle_expanded(self) -> None:
        self._expanded = not self._expanded

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded

    def render(self) -> RenderableType:
        return render_report_update(self._update, theme=self._theme, expanded=self._expanded)
