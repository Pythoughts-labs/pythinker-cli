"""Pythinker renderer for the ``Agent`` (subagent) tool."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TypedDict, cast

from rich import box as rich_box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.style import Style as RichStyle
from rich.table import Table
from rich.text import Text

from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolRenderDefinition,
    ToolResultPayload,
)
from pythinker_code.ui.shell.tool_renderers._render_utils import (
    as_str,
    fg,
    format_lines_block,
    invalid_arg,
    loading_marker,
    missing_required_arg,
    pending_tool_call_header,
    running_spinner,
    tool_call_header,
)
from pythinker_code.ui.terminal_capabilities import ascii_glyphs_enabled
from pythinker_code.ui.theme import tui_rich_style

_TOOL_NAME = "Agent"
_RUN_AGENTS_TOOL_NAME = "RunAgents"
_DEFAULT_COLLAPSED_LINES = 6
_RUN_AGENTS_ERROR_COLLAPSED_LINES = 8
_RUN_AGENTS_SUMMARY_PREVIEW_CHARS = 160
_BACKGROUND_ACTIVE_STATUSES = frozenset({"created", "starting", "running", "awaiting_approval"})

# ---------------------------------------------------------------------------
# Review findings aggregation
# ---------------------------------------------------------------------------

_SEVERITY_LABELS = ("critical", "high", "medium", "low")
_REVIEW_AGENT_TYPES = frozenset({"code-reviewer", "security-reviewer", "review"})

# Structured severity markers only — never mid-sentence prose.
# Form 1: bullet with [SEVERITY] tag:  `- [HIGH] description`
_RE_BRACKET_BULLET = re.compile(r"^[-*•]\s+\[(critical|high|medium|low)\]", re.IGNORECASE)
# Form 2: bullet with severity before colon: `- **High**: desc` / `- High: desc`
_RE_SEVERITY_COLON_BULLET = re.compile(
    r"^[-*•]\s+\*{0,2}(critical|high|medium|low)\*{0,2}:\s", re.IGNORECASE
)
# Form 3: bold severity at line start (no bullet): `**Critical**: desc`
_RE_BOLD_SEVERITY = re.compile(r"^\*\*(critical|high|medium|low)\*\*:", re.IGNORECASE)
# Section headers
_RE_HEADER = re.compile(r"^#{1,4}\s+(.*)")
_RE_SEVERITY_IN_HEADER = re.compile(r"\b(critical|high|medium|low)\b(?!-)", re.IGNORECASE)
# Markdown table row starting with a severity cell: `| HIGH | description |`
_RE_TABLE_SEVERITY_ROW = re.compile(r"^\|\s*(critical|high|medium|low)\s*\|", re.IGNORECASE)
# Primary machine-readable format: ```report\n{"findings":[{"severity":"high",...}]}\n```
# ponytail: no line-start anchor — tolerates LLM fencing drift; tighten if reviewers stabilize
_RE_REPORT_BLOCK = re.compile(r"```report\s*\n(.*?)```", re.DOTALL)


class _ReportFinding(TypedDict, total=False):
    severity: str


class _ReportBlock(TypedDict, total=False):
    findings: list[_ReportFinding]


@dataclass
class ReviewFindingsSummary:
    critical: int
    high: int
    medium: int
    low: int
    unparsed_reports: int
    reporters: dict[str, list[str]]
    parsed_reports: int
    total_reports: int


def _is_review_agent(subagent_type: str) -> bool:
    return subagent_type.lower().replace("_", "-") in _REVIEW_AGENT_TYPES


def _is_review_run(agents: list[dict[str, str]]) -> bool:
    return any(
        _is_review_agent(a.get("subagent_type") or a.get("actual_subagent_type") or "")
        for a in agents
    )


def _parse_reviewer_findings(result_text: str) -> tuple[dict[str, int], bool]:
    """Parse severity counts from structured markers only (never mid-sentence prose).

    Returns (severity_counts, was_parsed). was_parsed is True when a structured
    report was found — including a valid ```report block with zero findings.
    False means the whole report is unreadable prose with no structured markers.

    Primary: ```report JSON block ({"findings": [{"severity": "high", ...}, ...]}).
    Any ```report block (even one with empty findings or malformed JSON) means this
    is a structured report, so the markdown fallback is skipped entirely.

    Fallback: line-by-line markdown markers for prose-formatted reports.
    The fallback only runs when no ```report block is present, so it never
    re-scans JSON block content and cannot miscount severity words inside JSON.
    """
    counts: dict[str, int] = {sev: 0 for sev in _SEVERITY_LABELS}

    # Primary: machine-readable ```report JSON block. Only a well-formed JSON
    # object counts as "parsed" — a malformed block must not report success with
    # zero findings; it falls through to the markdown fallback instead.
    json_blocks = list(_RE_REPORT_BLOCK.finditer(result_text))
    if json_blocks:
        parsed_valid = False
        for block_match in json_blocks:
            try:
                parsed = json.loads(block_match.group(1))
            except json.JSONDecodeError:
                continue  # malformed JSON — block found but not parseable
            if not isinstance(parsed, dict):
                continue  # wrong shape (array / scalar)
            data = cast(_ReportBlock, parsed)
            findings = data.get("findings")
            if not isinstance(findings, list):
                continue  # "findings" missing or not a list — nothing to count
            parsed_valid = True
            # The declared type promises dict findings, but the payload is
            # untrusted JSON; re-type as object so the runtime guard below is real.
            for finding in cast("list[object]", findings):
                if not isinstance(finding, dict):
                    continue  # skip non-object finding entries
                sev = str(cast("dict[str, object]", finding).get("severity", "")).lower()
                if sev in counts:
                    counts[sev] += 1
        # A report block was present: return its counts. ``parsed_valid`` is True
        # only when at least one block was a JSON object with a list ``findings``
        # (possibly empty); a malformed block or a wrong-shaped payload (e.g.
        # ``{"findings": "high"}``) is reported as unparsed, never as a false
        # "parsed with zero findings", and never re-scanned below.
        return counts, parsed_valid

    # Fallback: line-by-line markdown markers.
    # Only reached when no ```report block exists — result_text is JSON-free.
    found_any = False
    section_severity: str | None = None  # set when inside e.g. "### High Severity"

    for raw in result_text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue

        # Section headers: update context, don't count as findings.
        m = _RE_HEADER.match(stripped)
        if m:
            header_lower = m.group(1).lower()
            sm = _RE_SEVERITY_IN_HEADER.search(header_lower)
            section_severity = sm.group(1).lower() if sm else None
            continue

        # Form 1: `- [HIGH] description`
        m = _RE_BRACKET_BULLET.match(stripped)
        if m:
            counts[m.group(1).lower()] += 1
            found_any = True
            continue

        # Form 2: `- **High**: description` or `- High: description`
        m = _RE_SEVERITY_COLON_BULLET.match(stripped)
        if m:
            counts[m.group(1).lower()] += 1
            found_any = True
            continue

        # Form 3: `**Critical**: description` at line start (no bullet)
        m = _RE_BOLD_SEVERITY.match(stripped)
        if m:
            counts[m.group(1).lower()] += 1
            found_any = True
            continue

        # Form 4: plain bullet inside a named-severity subsection (e.g. `### High`)
        if section_severity and re.match(r"^[-*•]\s+\S", stripped):
            counts[section_severity] += 1
            found_any = True
            continue

        # Form 5: markdown table row `| HIGH | description |`
        m = _RE_TABLE_SEVERITY_ROW.match(stripped)
        if m:
            counts[m.group(1).lower()] += 1
            found_any = True

    return counts, found_any


def _aggregate_findings(agents: list[dict[str, str]]) -> ReviewFindingsSummary:
    """Aggregate severity counts across all reviewer agents."""
    counts: dict[str, int] = {sev: 0 for sev in _SEVERITY_LABELS}
    reporters: dict[str, list[str]] = {sev: [] for sev in _SEVERITY_LABELS}
    reporters["unknown"] = []
    unparsed = 0
    parsed = 0
    total = 0

    for agent in agents:
        subagent_type = agent.get("subagent_type") or agent.get("actual_subagent_type") or ""
        if not _is_review_agent(subagent_type):
            continue
        total += 1
        name = agent.get("name") or subagent_type
        result_text = agent.get("result_text", "")

        if not result_text:
            unparsed += 1
            reporters["unknown"].append(name)
            continue

        agent_counts, was_parsed = _parse_reviewer_findings(result_text)
        if not was_parsed:
            unparsed += 1
            reporters["unknown"].append(name)
        else:
            parsed += 1
            for sev in _SEVERITY_LABELS:
                if agent_counts[sev] > 0:
                    counts[sev] += agent_counts[sev]
                    reporters[sev].append(name)

    return ReviewFindingsSummary(
        critical=counts["critical"],
        high=counts["high"],
        medium=counts["medium"],
        low=counts["low"],
        unparsed_reports=unparsed,
        reporters=reporters,
        parsed_reports=parsed,
        total_reports=total,
    )


def _render_findings_table(summary: ReviewFindingsSummary) -> RenderableType:
    """Render a compact findings summary table for completed review runs."""
    box_style = rich_box.ASCII if ascii_glyphs_enabled() else rich_box.ROUNDED

    table = Table(
        box=None,
        show_header=True,
        header_style=tui_rich_style("muted"),
        show_edge=False,
        expand=False,
        padding=(0, 1),
    )
    table.add_column("Severity", min_width=9)
    table.add_column("Count", justify="right", min_width=5)
    table.add_column("Reported by")

    _sev_style = {
        "critical": "error",
        "high": "error",
        "medium": "warning",
        "low": "info",
    }

    def _reported_by(names: list[str]) -> Text:
        if not names:
            return Text("—", style=tui_rich_style("dim"))
        t = Text("— ", style=tui_rich_style("warning"))
        t.append(", ".join(names))
        return t

    for sev in _SEVERITY_LABELS:
        count = getattr(summary, sev)
        by = summary.reporters.get(sev, [])
        style = tui_rich_style(_sev_style[sev]) if count > 0 else tui_rich_style("dim")
        table.add_row(
            Text(sev.capitalize(), style=style),
            Text(str(count), style=style),
            _reported_by(by),
        )

    if summary.unparsed_reports > 0:
        by = summary.reporters.get("unknown", [])
        table.add_row(
            Text("Unknown", style=tui_rich_style("muted")),
            Text(str(summary.unparsed_reports), style=tui_rich_style("muted")),
            _reported_by(by),
        )

    n, total = summary.parsed_reports, summary.total_reports
    report_word = "report" if total == 1 else "reports"
    footer_str = f"Parsed {n}/{total} reviewer {report_word}"
    if summary.unparsed_reports > 0:
        u = summary.unparsed_reports
        suffix = "report" if u == 1 else "reports"
        footer_str += f" · {u} {suffix} kept as unparsed prose"
    footer = Text(footer_str, style=tui_rich_style("dim"))

    return Panel(
        Group(table, footer),
        title="Review Findings",
        title_align="left",
        border_style=tui_rich_style("border_muted"),
        box=box_style,
        padding=(0, 1),
        expand=False,
    )


def _subagent_loader(_ctx: ToolRenderContext) -> Text:
    """Return the muted pulsating transcript marker used for active subagent rows."""
    return loading_marker(style_token="muted")


def _render_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    subagent_type = as_str(args.get("subagent_type")) or "coder"
    description = as_str(args.get("description"))
    prompt = as_str(args.get("prompt"))
    resume = as_str(args.get("resume"))
    run_bg = bool(args.get("run_in_background"))
    model = as_str(args.get("model"))

    secondary_token = "thinking_text"
    summary = Text()
    summary.append_text(fg("border_accent", subagent_type))
    if description:
        summary.append_text(fg(secondary_token, f" · {description}"))
    if model:
        summary.append_text(fg("dim", f" · {model}"))
    if run_bg:
        summary.append_text(fg(secondary_token, " · background"))
    if resume:
        summary.append_text(fg(secondary_token, f" · resume {resume[:8]}"))

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    header = tool_call_header(
        "Agent", summary, style_token=style_token, summary_style_token=secondary_token
    )

    missing: list[RenderableType] = []
    if description is None and ctx.has_result:
        missing.append(missing_required_arg("description"))
    if prompt is None:
        if "prompt" in args:
            missing.append(invalid_arg())
        elif ctx.has_result:
            missing.append(missing_required_arg("prompt"))
        rendered = Group(header, *missing) if missing else header
        return running_spinner(
            rendered,
            execution_started=ctx.execution_started,
            has_result=ctx.has_result,
            marker_style_token="muted",
        )

    rendered = Group(header, *missing) if missing else header
    return running_spinner(
        rendered,
        execution_started=ctx.execution_started,
        has_result=ctx.has_result,
        marker_style_token="muted",
    )


def _line_value(text: str, key: str) -> str | None:
    prefix = f"{key}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def _background_status(text: str) -> str | None:
    kind = _line_value(text, "kind")
    status = _line_value(text, "status")
    if kind == "agent" and status in _BACKGROUND_ACTIVE_STATUSES:
        return status
    return None


def _compact_inline(text: str, *, max_chars: int = 80) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    if max_chars <= 1:
        return "…"
    return compact[: max_chars - 1].rstrip() + "…"


def _plural(count: int, singular: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


def _run_agent_arg_summaries(args: dict[str, object]) -> list[tuple[str, str]] | None:
    raw_agents_value = args.get("agents")
    if not isinstance(raw_agents_value, list):
        return None
    raw_agents = cast(list[object], raw_agents_value)
    summaries: list[tuple[str, str]] = []
    for index, raw_agent in enumerate(raw_agents, start=1):
        if not isinstance(raw_agent, dict):
            summaries.append((f"agent-{index}", "coder"))
            continue
        raw_agent_dict = cast(dict[str, object], raw_agent)
        name = as_str(raw_agent_dict.get("name")) or f"agent-{index}"
        subagent_type = as_str(raw_agent_dict.get("subagent_type")) or "coder"
        summaries.append(
            (
                _compact_inline(name, max_chars=24),
                _compact_inline(subagent_type, max_chars=24),
            )
        )
    return summaries


def _render_run_agents_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    agent_summaries = _run_agent_arg_summaries(args)
    summary_text = Text()
    if agent_summaries is None:
        if ctx.has_result:
            summary_text.append_text(missing_required_arg("agents"))
        else:
            header = pending_tool_call_header(_RUN_AGENTS_TOOL_NAME)
            return running_spinner(
                header,
                execution_started=ctx.execution_started,
                has_result=ctx.has_result,
                marker_style_token="muted",
            )
    else:
        summary_text.append_text(fg("border_accent", _plural(len(agent_summaries), "agent")))

    mode = "foreground" if args.get("run_in_background") is False else "background"
    if summary_text.plain:
        summary_text.append_text(fg("thinking_text", f" · {mode}"))

    run_summary = as_str(args.get("summary"))
    if run_summary:
        summary_text.append_text(fg("dim", f" · {_compact_inline(run_summary, max_chars=70)}"))

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    header = tool_call_header(
        _RUN_AGENTS_TOOL_NAME,
        summary_text if summary_text.plain else None,
        style_token=style_token,
    )

    children: list[RenderableType] = [header]
    missing: list[RenderableType] = []
    if ctx.has_result and run_summary is None:
        missing.append(missing_required_arg("summary"))
    if missing:
        children.extend(missing)
    if agent_summaries and not ctx.has_result:
        listed = ", ".join(f"{name}:{subagent_type}" for name, subagent_type in agent_summaries[:4])
        if len(agent_summaries) > 4:
            listed = f"{listed}, +{len(agent_summaries) - 4} more"
        children.append(fg("dim", f"agents: {listed}"))

    rendered: RenderableType = Group(*children) if len(children) > 1 else header
    return running_spinner(
        rendered,
        execution_started=ctx.execution_started,
        has_result=ctx.has_result,
        marker_style_token="muted",
    )


def _split_key_value(line: str) -> tuple[str | None, str | None]:
    if ":" not in line:
        return None, None
    key, value = line.split(":", 1)
    key = key.strip()
    if not key or " " in key:
        return None, None
    return key, value.strip()


def _run_agent_summary_preview(result_text: str) -> str | None:
    lines = result_text.splitlines()
    try:
        start = lines.index("[summary]") + 1
    except ValueError:
        start = 0

    metadata_keys = {
        "agent_id",
        "actual_subagent_type",
        "automatic_notification",
        "budget_seconds",
        "dependencies",
        "description",
        "isolation",
        "kind",
        "next_step",
        "resumed",
        "resume_hint",
        "synthesis_state",
        "task_id",
        "tool_status",
    }
    pieces: list[str] = []
    for raw_line in lines[start:]:
        line = raw_line.strip()
        if not line or line == "[summary]":
            continue
        key, _ = _split_key_value(line)
        if key is not None and key.lower() in metadata_keys:
            continue
        heading = line.strip("#*: ").upper()
        if not pieces and heading in {"SUMMARY", "FINDINGS", "RISKS", "OVERALL"}:
            continue
        pieces.append(line)
        if len(" ".join(pieces)) >= _RUN_AGENTS_SUMMARY_PREVIEW_CHARS or len(pieces) >= 2:
            break
    if not pieces:
        return None
    return _compact_inline(
        " ".join(pieces),
        max_chars=_RUN_AGENTS_SUMMARY_PREVIEW_CHARS,
    )


def _hydrate_run_agent_result(agent: dict[str, str]) -> None:
    result_text = agent.get("result_text", "")
    if not result_text:
        return
    for key in (
        "task_id",
        "agent_id",
        "kind",
        "status",
        "description",
        "actual_subagent_type",
    ):
        value = _line_value(result_text, key)
        if not value:
            continue
        if key == "status":
            agent["detail_status"] = value
        elif key == "actual_subagent_type" and not agent.get("subagent_type"):
            agent["subagent_type"] = value
        else:
            agent.setdefault(key, value)
    preview = _run_agent_summary_preview(result_text)
    if preview:
        agent["summary_preview"] = preview


def _parse_run_agents_output(text: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    top: dict[str, str] = {}
    agents: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    result_lines: list[str] = []
    in_result = False

    def finish_result() -> None:
        nonlocal in_result, result_lines
        if current is not None and result_lines:
            current["result_text"] = "\n".join(result_lines)
            _hydrate_run_agent_result(current)
        result_lines = []
        in_result = False

    def finish_agent() -> None:
        finish_result()
        if current is not None:
            agents.append(current)

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("- name: "):
            finish_agent()
            current = {"name": line.removeprefix("- name: ").strip()}
            continue

        if current is None:
            key, value = _split_key_value(line)
            if key is not None and value is not None:
                top[key] = value
            continue

        if in_result:
            if line == "":
                result_lines.append("")
                continue
            if line.startswith("    "):
                result_lines.append(line[4:])
                continue
            finish_result()

        if not line.startswith("  "):
            continue
        stripped = line[2:]
        if stripped == "result: |":
            in_result = True
            result_lines = []
            continue
        key, value = _split_key_value(stripped)
        if key is not None and value is not None:
            current[key] = value

    finish_agent()
    return top, agents


def _status_style_token(status: str) -> str:
    normalized = status.lower()
    if normalized in {"error", "failed", "failure"}:
        return "error"
    if normalized in {"completed", "success", "succeeded"}:
        return "success"
    if normalized in {"created", "starting", "running", "awaiting_approval", "launched"}:
        return "accent"
    return "muted"


def _status_glyph(status: str) -> str:
    normalized = status.lower()
    if normalized in {"error", "failed", "failure"}:
        return "✘"
    if normalized in {"completed", "success", "succeeded"}:
        return "✓"
    if normalized in {"created", "starting", "running", "awaiting_approval", "launched"}:
        return "●"
    return "○"


def _top_status_label(status: str) -> str:
    if status == "success":
        return "completed"
    if status == "failure":
        return "failed"
    return status or "completed"


def _render_run_agents_text_result(
    ctx: ToolRenderContext, result: ToolResultPayload
) -> RenderableType | None:
    text = (result.text or "").rstrip("\n")
    if not text:
        return None
    ctx.state["__suppress_generic_expand_hint__"] = True
    body, remaining = format_lines_block(
        text,
        expanded=ctx.expanded,
        collapsed_max_lines=_RUN_AGENTS_ERROR_COLLAPSED_LINES,
        style_token="error" if result.is_error else "tool_output",
    )
    if remaining > 0:
        return Group(body, fg("muted", f"… ({remaining} more lines preserved)"))
    return body


def _render_run_agents_result(
    ctx: ToolRenderContext, result: ToolResultPayload
) -> RenderableType | None:
    if not result.text:
        return None
    if result.is_error:
        return _render_run_agents_text_result(ctx, result)

    top, agents = _parse_run_agents_output(result.text)
    if not agents:
        return _render_run_agents_text_result(ctx, result)

    ctx.state["__suppress_generic_expand_hint__"] = True
    status = _top_status_label(top.get("tool_status", "success"))
    count = top.get("agent_count") or str(len(agents))
    mode = top.get("mode")
    approval = top.get("orchestration_approval")

    summary = Text()
    summary.append(f"agents {status}", style=tui_rich_style(_status_style_token(status)))
    summary.append(f" · {count} total", style=tui_rich_style("dim"))
    if mode:
        summary.append(f" · {mode}", style=tui_rich_style("dim"))
    if approval:
        summary.append(f" · approval {approval}", style=tui_rich_style("dim"))

    # Pre-compute per-agent fields so the variable-width label and status columns
    # can be padded to a shared width — sibling rows then line up their "· status"
    # and "· task_id" separators instead of stair-stepping with each name length.
    entries: list[dict[str, str]] = []
    for index, agent in enumerate(agents):
        subagent_type = agent.get("subagent_type") or agent.get("actual_subagent_type") or "coder"
        name = agent.get("name") or f"agent-{index + 1}"
        # A name identical to the subagent_type is redundant; show it only when it
        # carries information the type doesn't (e.g. "code_scan" vs "code-reviewer").
        extra = "" if name == subagent_type else name
        entries.append(
            {
                "subagent_type": subagent_type,
                "name_extra": extra,
                "status": agent.get("detail_status") or agent.get("status") or "unknown",
                "task_id": agent.get("task_id") or "",
                "summary_preview": agent.get("summary_preview") or "",
                "message": agent.get("message") or "",
                "brief": agent.get("brief") or "",
            }
        )

    def label_width(entry: dict[str, str]) -> int:
        # Display width of "type" or "type · name" — drives the shared label column.
        extra = entry["name_extra"]
        return len(entry["subagent_type"]) + (len(f" · {extra}") if extra else 0)

    label_col = max(label_width(entry) for entry in entries)
    # Only pad the status column when a later task_id column needs to align under it.
    status_col = max(
        (len(entry["status"]) for entry in entries if entry["task_id"]),
        default=0,
    )

    dim_style = tui_rich_style("dim")
    rows: list[RenderableType] = [summary]
    for index, entry in enumerate(entries):
        is_last = index == len(entries) - 1
        branch = "└─" if is_last else "├─"
        agent_status = entry["status"]
        status_token = _status_style_token(agent_status)

        row = Text(f"{branch} ", style=tui_rich_style("muted"))
        row.append(_status_glyph(agent_status), style=tui_rich_style(status_token))
        row.append(" ")
        row.append(entry["subagent_type"], style=tui_rich_style("muted"))
        if entry["name_extra"]:
            name_style = tui_rich_style("tool_title") + RichStyle(bold=True)
            row.append(f" · {entry['name_extra']}", style=name_style)
        # Pad the label region so every "· status" separator starts at one column.
        row.append(" " * (label_col - label_width(entry)))
        row.append(" · ", style=dim_style)
        status_text = agent_status.ljust(status_col) if entry["task_id"] else agent_status
        row.append(status_text, style=tui_rich_style(status_token))
        if entry["task_id"]:
            row.append(f" · {entry['task_id']}", style=dim_style)
        rows.append(row)

        if agent_status in {"error", "failed", "failure"}:
            preview = entry["message"] or entry["brief"] or entry["summary_preview"]
            if preview:
                prefix = "   " if is_last else "│  "
                rows.append(fg("dim", f"{prefix}{_compact_inline(preview, max_chars=100)}"))
        elif not _is_review_run(agents):
            preview = entry["brief"] or entry["summary_preview"]
            if preview:
                prefix = "   " if is_last else "│  "
                rows.append(fg("dim", f"{prefix}{_compact_inline(preview, max_chars=100)}"))

    if _is_review_run(agents):
        findings = _aggregate_findings(agents)
        rows.append(Text(""))
        rows.append(_render_findings_table(findings))

    return Group(*rows)


def _render_result(ctx: ToolRenderContext, result: ToolResultPayload) -> RenderableType | None:
    if not result.text:
        return None
    background_status = None if result.is_error else _background_status(result.text)
    if background_status is not None:
        description = as_str(ctx.args.get("description"))
        label = "background subagent working"
        if description:
            label = f"{label}: {description}"
        line = _subagent_loader(ctx)
        line.append(label, style=tui_rich_style("accent") + RichStyle(bold=True))
        # Hang-indent the detail row under the label (past the 2-cell marker)
        # so the block nests cleanly inside the result gutter.
        return Group(line, fg("dim", f"  status: {background_status}"))

    body, remaining = format_lines_block(
        result.text,
        expanded=ctx.expanded,
        collapsed_max_lines=_DEFAULT_COLLAPSED_LINES,
        style_token="error" if result.is_error else "tool_output",
    )
    if not body.plain:
        return fg("error", "Agent failed") if result.is_error else None
    if remaining > 0:
        more = fg("muted", f"... ({remaining} more lines, ctrl+o to expand)")
        return Group(body, more)
    return body


AGENT_RENDERER = ToolRenderDefinition(
    name=_TOOL_NAME,
    label="subagent",
    render_shell="default",
    render_call=_render_call,
    render_result=_render_result,
)

RUN_AGENTS_RENDERER = ToolRenderDefinition(
    name=_RUN_AGENTS_TOOL_NAME,
    label="subagents",
    render_shell="default",
    render_call=_render_run_agents_call,
    render_result=_render_run_agents_result,
)
