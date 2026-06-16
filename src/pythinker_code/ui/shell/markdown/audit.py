"""Audit-report normalization for dense agent final answers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pythinker_code.ui.shell.markdown.fences import FENCE_RE, FenceState
from pythinker_code.ui.shell.markdown.normalizers import (
    UNICODE_RULE_LINE_RE as _UNICODE_RULE_LINE_RE,
)
from pythinker_code.ui.shell.markdown.normalizers import (
    parse_aligned_field_line,
)

PROJECT_PATH_PREFIXES: tuple[str, ...] = (
    "src/pythinker_code/",
    "/Users/panda/Projects/active/Projects/pythinker-code-main/src/pythinker_code/",
    "/Users/panda/Projects/active/Projects/pythinker-code-main/",
)

_QUOTE_GUTTER_RE = re.compile(r"^(\s*)▌\s?")
_UNDERLINE_HEADING_RE = re.compile(
    r"^(Reference|Pythinker|Rationale|Spec|Verdict|Command|Expected|Result|"
    r"Checks|Gate command|Lint command)\s*$",
    re.I,
)
_BULLET_RE = re.compile(r"^(\s*)[-•]\s+(.+)$")
_SECTION_HEADING_RE = re.compile(r"^(\s*)(?:#{1,2}\s+)?(\d+)\.\s+(.+)$")
_FIELD_LINE_RE = re.compile(r"^(\s*)-\s+([^:]+):\s*(.+)$")
_STATUS_EXACT_RE = re.compile(r"✓|exact", re.I)
_STATUS_DIVERGE_RE = re.compile(r"!|diverge|intentional", re.I)
_STATUS_GAP_RE = re.compile(r"×|gap|missing|undocumented", re.I)

_PARITY_SECTION_HINTS = ("parity matrix", "behavioural contract", "behavioral contract")
_COLLAPSE_PARITY_MIN_ITEMS = 5

_GROUP_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Lifecycle / Process",
        (
            "spawn",
            "initialize",
            "startup",
            "restart",
            "timeout",
            "handshake",
            "lazy",
            "generation",
            "process",
        ),
    ),
    ("Diagnostics", ("diagnostic", "dedup", "volume", "handler", "publish", "registry", "passive")),
    (
        "Tool / Input",
        (
            "tool",
            "unc",
            "file size",
            "git check",
            "maxresult",
            "operation",
            "symbol",
            "readonly",
            "concurrency",
        ),
    ),
    (
        "Integration",
        (
            "edit",
            "write",
            "runtime",
            "cleanup",
            "provider",
            "plugin",
            "manifest",
            "recommendation",
            "injection",
            "subagent",
        ),
    ),
    (
        "File sync / Routing",
        ("extension", "routing", "open/", "change", "save", "close", "workspace", "configuration"),
    ),
)


@dataclass(slots=True)
class _ParityItem:
    title: str
    fields: dict[str, str] = field(default_factory=lambda: {})

    @property
    def status(self) -> str:
        raw = self.fields.get("Status", "")
        if _STATUS_GAP_RE.search(raw):
            return "gap"
        if _STATUS_DIVERGE_RE.search(raw):
            return "diverge"
        if _STATUS_EXACT_RE.search(raw):
            return "exact"
        return "other"

    @property
    def group(self) -> str:
        title = self.title.lower()
        for group_name, keywords in _GROUP_KEYWORDS:
            if any(keyword in title for keyword in keywords):
                return group_name
        return "Contracts"


def detect_audit_report(markup: str) -> bool:
    """Whether *markup* looks like a dense parity/inventory audit report."""
    if "•" not in markup and "Reference line" not in markup:
        return False
    field_rows = sum(
        1 for line in markup.splitlines() if parse_aligned_field_line(line) is not None
    )
    status_rows = sum(
        1
        for line in markup.splitlines()
        if parse_aligned_field_line(line) is not None and "status" in line.lower()
    )
    return field_rows >= 4 and status_rows >= 2


def compact_known_paths(text: str) -> str:
    """Shorten common repo prefixes for terminal readability."""
    for prefix in PROJECT_PATH_PREFIXES:
        if prefix in text:
            text = text.replace(prefix, "")
    return text


def normalize_quote_gutters(markup: str) -> str:
    """Convert ``▌`` quote markers into Markdown blockquotes."""
    if "▌" not in markup:
        return markup
    out: list[str] = []
    state = FenceState()
    for line in markup.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        eol = line[len(body) :]
        if state.active:
            out.append(line)
            state.feed(body)
            continue
        if FENCE_RE.match(body) is not None:
            state.feed(body)
            out.append(line)
            continue
        match = _QUOTE_GUTTER_RE.match(body)
        if match is not None:
            indent = match.group(1)
            quote = body[match.end() :].strip()
            out.append(f"{indent}> {quote}{eol}")
            continue
        out.append(line)
    return "".join(out)


def normalize_unicode_underline_headings(markup: str) -> str:
    """Convert ``Heading`` + underline rule lines into markdown headings."""
    lines = markup.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        body = lines[index].rstrip("\r\n")
        if (
            index + 1 < len(lines)
            and body.strip()
            and _UNDERLINE_HEADING_RE.match(body.strip())
            and _UNICODE_RULE_LINE_RE.match(lines[index + 1].strip())
        ):
            out.append(f"### {body.strip()}")
            index += 2
            continue
        if (
            index + 1 < len(lines)
            and body.strip()
            and not body.lstrip().startswith("#")
            and _UNICODE_RULE_LINE_RE.match(lines[index + 1].strip())
            and len(body.strip()) <= 120
        ):
            out.append(f"# {body.strip()}")
            index += 2
            continue
        if _UNICODE_RULE_LINE_RE.match(body.strip()):
            index += 1
            continue
        out.append(body)
        index += 1
    result = "\n".join(out)
    if markup.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _compact_field_value(label: str, value: str) -> str:
    if label in {"Pythinker location", "Reference line", "Reference equivalent"}:
        return compact_known_paths(value)
    return value


def _field_table(title: str, fields: dict[str, str]) -> list[str]:
    if not fields:
        return [f"- ✓ {title}"]
    rows = [
        f"| {label} | {_compact_field_value(label, value)} |" for label, value in fields.items()
    ]
    return [
        f"**{title}**",
        "",
        "| | |",
        "| --- | --- |",
        *rows,
        "",
    ]


def _parse_parity_items(lines: list[str], start: int, end: int) -> list[_ParityItem]:
    items: list[_ParityItem] = []
    current: _ParityItem | None = None
    for line in lines[start:end]:
        stripped = line.strip()
        if not stripped:
            continue
        field_md = _FIELD_LINE_RE.match(line)
        if field_md is not None:
            if current is not None:
                current.fields[field_md.group(2).strip()] = field_md.group(3).strip()
            continue
        field = parse_aligned_field_line(line)
        if field is not None:
            if current is not None:
                _, label, value = field
                current.fields[label] = value
            continue
        bullet = _BULLET_RE.match(line)
        if bullet is not None:
            current = _ParityItem(title=bullet.group(2).strip())
            items.append(current)
    return items


def _render_collapsed_parity(items: list[_ParityItem]) -> list[str]:
    exact = sum(1 for item in items if item.status == "exact")
    diverge = sum(1 for item in items if item.status == "diverge")
    gaps = sum(1 for item in items if item.status == "gap")
    out = [
        "| Status | Count |",
        "| --- | --- |",
        f"| ✓ Exact parity | {exact} |",
        f"| ! Intentional divergence | {diverge} |",
        f"| × Undocumented gaps | {gaps} |",
        "",
    ]
    grouped: dict[str, list[_ParityItem]] = {}
    for item in items:
        grouped.setdefault(item.group, []).append(item)

    for group_name, group_items in grouped.items():
        out.append(f"#### {group_name}")
        out.append("")
        for item in group_items:
            status_glyph = {"exact": "✓", "diverge": "!", "gap": "×"}.get(item.status, "•")
            out.append(f"- {status_glyph} {item.title}")
        out.append("")
    return out


def collapse_parity_matrix(markup: str) -> str:
    """Summarize large parity matrices as counts plus grouped checklists."""
    lines = markup.splitlines()
    section_ranges: list[tuple[int, int, str]] = []
    section_start: int | None = None
    section_title = ""
    for index, line in enumerate(lines):
        section_match = _SECTION_HEADING_RE.match(line)
        if section_match is not None:
            if section_start is not None:
                section_ranges.append((section_start, index, section_title))
            section_start = index + 1
            section_title = section_match.group(3).strip().lower()
            continue
        if line.startswith("## ") and section_start is not None:
            section_ranges.append((section_start, index, section_title))
            section_start = None
            section_title = ""
    if section_start is not None:
        section_ranges.append((section_start, len(lines), section_title))

    if not section_ranges:
        return markup

    out: list[str] = []
    cursor = 0
    changed = False
    for start, end, title in section_ranges:
        out.extend(lines[cursor:start])
        if not any(hint in title for hint in _PARITY_SECTION_HINTS):
            out.extend(lines[start:end])
            cursor = end
            continue
        items = _parse_parity_items(lines, start, end)
        if len(items) < _COLLAPSE_PARITY_MIN_ITEMS:
            out.extend(lines[start:end])
            cursor = end
            continue
        changed = True
        out.extend(_render_collapsed_parity(items))
        cursor = end
    out.extend(lines[cursor:])
    if not changed:
        return markup
    result = "\n".join(out)
    if markup.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def normalize_field_blocks(markup: str) -> str:
    """Render parity/inventory field groups as compact tables instead of nested bullets."""
    lines = markup.splitlines()
    out: list[str] = []
    index = 0
    changed = False
    while index < len(lines):
        line = lines[index]
        bullet = _BULLET_RE.match(line)
        if bullet is None or parse_aligned_field_line(line) is not None:
            out.append(line)
            index += 1
            continue
        title = bullet.group(2).strip()
        fields: dict[str, str] = {}
        cursor = index + 1
        while cursor < len(lines):
            candidate = lines[cursor]
            if not candidate.strip():
                break
            if _BULLET_RE.match(candidate) and not parse_aligned_field_line(candidate):
                break
            if _SECTION_HEADING_RE.match(candidate) or candidate.startswith("## "):
                break
            field = parse_aligned_field_line(candidate)
            field_md = _FIELD_LINE_RE.match(candidate)
            if field is not None:
                _, label, value = field
                fields[label] = value
                cursor += 1
                continue
            if field_md is not None:
                fields[field_md.group(2).strip()] = field_md.group(3).strip()
                cursor += 1
                continue
            break
        if len(fields) >= 2:
            changed = True
            out.extend(_field_table(title, fields))
            index = cursor
            continue
        out.append(line)
        index += 1
    if not changed:
        return markup
    result = "\n".join(out)
    if markup.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def normalize_divergence_cards(markup: str) -> str:
    """Turn Reference/Pythinker/Rationale/Spec/Verdict stacks into one table card."""
    lines = markup.splitlines()
    out: list[str] = []
    index = 0
    changed = False
    while index < len(lines):
        line = lines[index]
        heading = re.match(r"^#{1,4}\s+(\d+(?:\.\d+)?)\s+(.+)$", line.strip())
        if heading is None:
            out.append(line)
            index += 1
            continue
        title = f"{heading.group(1)} {heading.group(2).strip()}"
        cursor = index + 1
        fields: dict[str, str] = {}
        while cursor < len(lines):
            candidate = lines[cursor].strip()
            if not candidate:
                cursor += 1
                if fields:
                    break
                continue
            if candidate.startswith("#"):
                break
            label_match = _UNDERLINE_HEADING_RE.match(candidate)
            if (
                label_match is not None
                and cursor + 1 < len(lines)
                and _UNICODE_RULE_LINE_RE.match(lines[cursor + 1].strip())
            ):
                body_lines: list[str] = []
                cursor += 2
                while cursor < len(lines):
                    body = lines[cursor].strip()
                    if not body:
                        cursor += 1
                        break
                    if _UNDERLINE_HEADING_RE.match(body) or body.startswith("#"):
                        break
                    body_lines.append(body)
                    cursor += 1
                fields[label_match.group(1).title()] = " ".join(body_lines).strip()
                continue
            if candidate.startswith("### "):
                label = candidate.removeprefix("### ").strip()
                body_lines = []
                cursor += 1
                while cursor < len(lines):
                    body = lines[cursor].strip()
                    if not body:
                        cursor += 1
                        break
                    if body.startswith("### ") or body.startswith("#"):
                        break
                    body_lines.append(body)
                    cursor += 1
                fields[label] = " ".join(body_lines).strip()
                continue
            break
        if len(fields) >= 3 and {"Reference", "Pythinker", "Verdict"} & set(fields):
            changed = True
            out.append(f"#### {title}")
            out.append("")
            out.append("| | |")
            out.append("| --- | --- |")
            for label in ("Reference", "Pythinker", "Rationale", "Spec", "Verdict"):
                if label in fields:
                    out.append(f"| {label} | {compact_known_paths(fields[label])} |")
            out.append("")
            index = cursor
            continue
        out.append(line)
        index += 1
    if not changed:
        return markup
    result = "\n".join(out)
    if markup.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def normalize_command_result_blocks(markup: str) -> str:
    """Convert Command/Expected/Result triplets into result-first checklist rows."""
    lines = markup.splitlines()
    out: list[str] = []
    index = 0
    changed = False
    while index < len(lines):
        line = lines[index]
        bullet = _BULLET_RE.match(line)
        if bullet is None:
            out.append(line)
            index += 1
            continue
        title = bullet.group(2).strip()
        fields: dict[str, str] = {}
        commands: list[str] = []
        cursor = index + 1
        while cursor < len(lines):
            candidate = lines[cursor]
            field = parse_aligned_field_line(candidate)
            field_md = _FIELD_LINE_RE.match(candidate)
            if field is not None:
                _, label, value = field
                fields[label] = value
                if label.lower() == "command" and value:
                    commands.append(value)
                cursor += 1
                continue
            if field_md is not None:
                label = field_md.group(2).strip()
                value = field_md.group(3).strip()
                fields[label] = value
                if label.lower() == "command" and value:
                    commands.append(value)
                cursor += 1
                continue
            break
        if not {"Command", "Expected", "Result"} & {k.title() for k in fields}:
            out.append(line)
            index += 1
            continue
        changed = True
        result = fields.get("Result") or fields.get("result") or fields.get("Expected") or ""
        label = title or "Check"
        out.append(f"- {result.strip()} **{label}**")
        if result and fields.get("Expected"):
            out.append(f"  Expected: {fields['Expected']}")
        for command in commands:
            out.append(f"  ```bash\n  {command}\n  ```")
        out.append("")
        index = cursor
    if not changed:
        return markup
    result = "\n".join(out)
    if markup.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def normalize_verification_prose(markup: str) -> str:
    """Turn inline ``Verification: cmd → result`` prose into compact check rows."""
    pattern = re.compile(
        r"^(?P<prefix>.*?Verification:\s*)"
        r"(?P<body>.+)$",
        re.I,
    )
    out_lines: list[str] = []
    changed = False
    for line in markup.splitlines():
        match = pattern.match(line.strip())
        if match is None:
            out_lines.append(line)
            continue
        changed = True
        prefix = match.group("prefix").strip()
        if prefix and prefix != "Verification:":
            out_lines.append(prefix)
        body = match.group("body")
        chunks = re.split(r"\.\s+(?=uv run|make |pytest|ruff )", body)
        out_lines.append("**Checks**")
        out_lines.append("")
        for chunk in chunks:
            chunk = chunk.strip().rstrip(".")
            if not chunk:
                continue
            if "→" in chunk:
                cmd, result = chunk.split("→", 1)
                out_lines.append(f"- {result.strip()} `{cmd.strip()}`")
            else:
                out_lines.append(f"- {chunk}")
        out_lines.append("")
    if not changed:
        return markup
    return "\n".join(out_lines) + ("\n" if markup.endswith("\n") else "")


def normalize_audit_header(markup: str) -> str:
    """Promote the report title into a summary blockquote with optional verdict line."""
    lines = markup.splitlines()
    if not lines:
        return markup
    title = ""
    cursor = 0
    first = lines[0].strip()
    if first.startswith("#"):
        title = first.lstrip("#").strip()
        cursor = 1
    elif first and not _UNICODE_RULE_LINE_RE.match(first):
        title = first
        cursor = 1
        if cursor < len(lines) and _UNICODE_RULE_LINE_RE.match(lines[cursor].strip()):
            cursor += 1

    verdict = ""
    for line in lines:
        lowered = line.lower()
        if (
            "30/30" in line
            or "parity verdict" in lowered
            or "no code changes are required" in lowered
        ):
            verdict = line.strip().lstrip("-•> ")
            break

    if not title:
        return markup

    summary: list[str] = [f"# {title}", ""]
    if verdict:
        summary.extend([f"> **Verdict:** {verdict}", ""])
    summary.extend(lines[cursor:])
    result = "\n".join(summary)
    if markup.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def dedupe_final_verdict_section(markup: str) -> str:
    """Drop a trailing ``Final answer`` section when a verdict section already exists."""
    if "final answer" not in markup.lower():
        return markup
    if not re.search(r"verdict and recommendation|##\s+\d+\.\s+verdict", markup, re.I):
        return markup
    parts = re.split(r"\n(?:Final answer|## Final answer)\s*\n", markup, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return parts[0].rstrip() + "\n"
    return markup


def _convert_space_aligned_basics(markup: str) -> str:
    """Convert bullets and section headings before audit-specific passes."""
    from pythinker_code.ui.shell.markdown.normalizers import normalize_space_aligned_report_blocks

    converted = normalize_space_aligned_report_blocks(markup)
    if converted == markup:
        return markup
    lines: list[str] = []
    for line in converted.splitlines():
        field_md = _FIELD_LINE_RE.match(line)
        if field_md is not None:
            label = field_md.group(2).strip()
            value = compact_known_paths(field_md.group(3).strip())
            lines.append(f"{field_md.group(1)}- {label}: {value}")
            continue
        lines.append(line)
    result = "\n".join(lines)
    if markup.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def normalize_audit_report(markup: str) -> str:
    """Audit-specific normalization for dense parity/inventory agent reports."""
    if not detect_audit_report(markup):
        return markup

    text = markup
    text = normalize_unicode_underline_headings(text)
    text = _convert_space_aligned_basics(text)
    text = normalize_quote_gutters(text)
    text = normalize_verification_prose(text)
    text = collapse_parity_matrix(text)
    text = normalize_field_blocks(text)
    text = normalize_divergence_cards(text)
    text = normalize_command_result_blocks(text)
    text = normalize_audit_header(text)
    text = dedupe_final_verdict_section(text)
    return text
