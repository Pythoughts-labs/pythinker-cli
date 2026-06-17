"""Markdown normalization and LLM-output repair passes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pythinker_code.ui.shell.components.render_utils import sanitize_ansi
from pythinker_code.ui.shell.glyphs import TRANSCRIPT_ASSISTANT_MARKER
from pythinker_code.ui.shell.markdown.fences import (
    FENCE_RE,
    MARKDOWN_FENCE_INFOS,
    FenceState,
)

if TYPE_CHECKING:
    pass

MAX_MARKDOWN_NORMALIZE_BYTES = 250_000

_MARKDOWN_ICON_REPLACEMENTS: dict[str, str] = {
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
_OL_ITEM_RE = re.compile(r"^\d+\.\s")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_DELIM_RUN_RE = re.compile(r"\|(?:\s*:?-{2,}:?\s*\|)+")
_HEADER_RE = re.compile(r"^(?P<prefix>.*?)(?P<cells>(?:\|[^\n|]*)+\|)\s*$")
UNICODE_RULE_LINE_RE = re.compile(r"^[─═━\-]{3,}$")
_UNICODE_RULE_LINE_RE = UNICODE_RULE_LINE_RE  # ponytail: compat alias for compat shim
_CODE_SPAN_RE = re.compile(r"(?P<ticks>`+)(?P<body>.*?)(?P=ticks)")


@dataclass(frozen=True, slots=True)
class MarkdownNormalizationResult:
    """Normalized markdown plus the repair passes that changed the input."""

    text: str
    applied: tuple[str, ...]


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


def repair_crammed_markdown_tables(markup: str) -> str:
    """Split report headings accidentally glued to a following Markdown table."""
    if "|" not in markup:
        return markup

    lines = markup.splitlines(keepends=True)
    if len(lines) < 2:
        return markup

    repaired: list[str] = []
    state = FenceState()
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        eol = line[len(body) :]
        if state.active:
            repaired.append(line)
            state.feed(body)
            continue
        match = FENCE_RE.match(body)
        if match is not None:
            state.feed(body)
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


def _contains_markdown_table(lines: list[str]) -> bool:
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


def unwrap_fenced_markdown_tables(markup: str) -> str:
    """Unwrap ```md fences whose body contains a markdown table."""
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
        match = FENCE_RE.match(body)
        if in_other_fence:
            out.append(line)
            if match is not None:
                fence = match.group("fence")
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
        if info not in MARKDOWN_FENCE_INFOS:
            in_other_fence = True
            other_char = fence[0]
            other_len = len(fence)
            out.append(line)
            i += 1
            continue

        close_index: int | None = None
        for j in range(i + 1, len(lines)):
            inner_body = lines[j].rstrip("\r\n")
            inner_match = FENCE_RE.match(inner_body)
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

        if out and out[-1].strip():
            out.append("\n")
        out.extend(fenced_body)
        next_line = lines[close_index + 1] if close_index + 1 < len(lines) else None
        ends_blank = bool(fenced_body) and not fenced_body[-1].strip()
        if next_line is not None and next_line.strip() and not ends_blank:
            out.append("\n")
        i = close_index + 1
    return "".join(out)


def _replace_report_icons(text: str) -> str:
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


def simplify_markdown_report_icons(markup: str) -> str:
    """Simplify report/status emoji outside fenced code blocks."""
    if not any(icon in markup for icon in _MARKDOWN_ICON_KEYS):
        return markup

    lines: list[str] = []
    state = FenceState()
    for line in markup.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if state.active:
            lines.append(line)
            state.feed(body)
            continue
        match = FENCE_RE.match(body)
        if match is not None:
            state.feed(body)
            lines.append(line)
            continue
        lines.append(_replace_report_icons(line))
    return "".join(lines)


def _escape_code_span_pipes(text: str) -> str:
    def _repl(match: re.Match[str]) -> str:
        ticks = match.group("ticks")
        body = re.sub(r"(?<!\\)\|", r"\\|", match.group("body"))
        return f"{ticks}{body}{ticks}"

    return _CODE_SPAN_RE.sub(_repl, text)


def _split_pipe_cells(segment: str) -> list[str]:
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


def normalize_table_block(text: str) -> str:
    """Repair malformed GFM tables in a fence-free block of markdown."""
    out: list[str] = []
    while True:
        match = _DELIM_RUN_RE.search(text)
        if match is None:
            return "".join(out) + text
        markers = _delimiter_markers(match.group(0))
        n_cols = len(markers)
        head = text[: match.start()]
        tail = text[match.end() :]

        indent = head[head.rfind("\n") + 1 :]
        if n_cols < 2 or indent.strip() != "":
            out.append(text[: match.end()])
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
            out.append(text[: match.end()])
            text = tail
            continue

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
                bail = True
                break
            for i in range(0, len(cells), n_cols):
                data_rows.append(cells[i : i + n_cols])
        if bail:
            out.append(text[: match.end()])
            text = tail
            continue

        preamble = head_lines[:-1]
        prose = header_match.group("prefix").rstrip()
        if preamble:
            out.append("\n".join(preamble) + "\n")
        if prose:
            out.append(prose + "\n")
        block_so_far = "".join(out)
        if block_so_far and not block_so_far.endswith("\n\n"):
            out.append("\n" if block_so_far.endswith("\n") else "\n\n")
        out.append(f"{indent}| " + " | ".join(header_cells) + " |\n")
        out.append(f"{indent}| " + " | ".join(markers) + " |\n")
        for row in data_rows:
            out.append(f"{indent}| " + " | ".join(row) + " |\n")

        remainder = "\n".join(tail_lines[consumed:])
        if not remainder.strip():
            return "".join(out)
        text = remainder if remainder.startswith("\n") else "\n" + remainder


def parse_aligned_field_line(line: str) -> tuple[str, str, str] | None:
    """Parse ``    Label      value`` report rows; return indent, label, value."""
    stripped = line.rstrip()
    if not stripped:
        return None
    first = stripped.lstrip()
    if first.startswith(("•", "-", "|", "#", ">", "`")):
        return None
    match = re.match(r"^(\s*)(.+?)\s{2,}(.+)$", stripped)
    if match is None:
        return None
    indent, label, value = match.group(1), match.group(2).strip(), match.group(3).strip()
    if not label or not value or len(label) > 48 or len(label.split()) > 6:
        return None
    if not (label[0].isupper() or label == "LoC"):
        return None
    return indent, label, value


def normalize_space_aligned_report_blocks(markup: str) -> str:
    """Convert LLM space-column report rows into nested Markdown lists."""
    if "•" not in markup:
        return markup

    lines = markup.splitlines()
    if sum(1 for line in lines if parse_aligned_field_line(line) is not None) < 3:
        return markup

    out: list[str] = []
    state = FenceState()
    last_field_idx: int | None = None

    index = 0
    while index < len(lines):
        line = lines[index]
        body = line.rstrip("\r\n")
        if state.active:
            out.append(line)
            state.feed(body)
            last_field_idx = None
            index += 1
            continue
        fence_match = FENCE_RE.match(body)
        if fence_match is not None:
            state.feed(body)
            out.append(line)
            last_field_idx = None
            index += 1
            continue

        bullet_match = re.match(r"^(\s*)•\s+(.+)$", body)
        if bullet_match is not None:
            indent, text = bullet_match.groups()
            out.append(f"{indent}- {text}")
            last_field_idx = None
            index += 1
            continue

        if (
            index + 1 < len(lines)
            and body.strip()
            and not body.lstrip().startswith("•")
            and _UNICODE_RULE_LINE_RE.match(lines[index + 1].strip())
        ):
            out.append(f"# {body.strip()}")
            index += 2
            last_field_idx = None
            continue

        section_match = re.match(r"^(\s*)(\d+)\.\s+(.+)$", body)
        if section_match is not None:
            _, number, title = section_match.groups()
            out.append(f"## {number}. {title}")
            last_field_idx = None
            index += 1
            continue

        if _UNICODE_RULE_LINE_RE.match(body.strip()):
            out.append("---")
            last_field_idx = None
            index += 1
            continue

        field = parse_aligned_field_line(body)
        if field is not None:
            indent, label, value = field
            nest = "  " if len(indent) >= 2 else ""
            out.append(f"{nest}- {label}: {value}")
            last_field_idx = len(out) - 1
            index += 1
            continue

        if last_field_idx is not None and re.match(r"^\s{6,}\S", body):
            out[last_field_idx] = f"{out[last_field_idx]} {body.strip()}"
            index += 1
            continue

        out.append(line)
        last_field_idx = None
        index += 1

    result = "\n".join(out)
    if markup.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def loosen_tight_ordered_lists(markup: str, *, min_item_length: int = 0) -> str:
    """Insert blank lines between consecutive ordered-list items.

    When *min_item_length* is greater than zero, only long items are loosened.
    """
    lines = markup.splitlines(keepends=True)
    out: list[str] = []
    state = FenceState()
    prev_was_ol = False

    for line in lines:
        body = line.rstrip("\r\n")
        if state.active:
            out.append(line)
            state.feed(body)
            prev_was_ol = False
            continue
        match = FENCE_RE.match(body)
        if match is not None:
            state.feed(body)
            out.append(line)
            prev_was_ol = False
            continue

        is_ol = bool(_OL_ITEM_RE.match(line))
        if is_ol and prev_was_ol and len(line.strip()) > min_item_length:
            out.append("\n")
        out.append(line)
        prev_was_ol = is_ol

    return "".join(out)


def normalize_markdown_tables(markup: str) -> str:
    """Apply :func:`normalize_table_block` to every fence-free span of markup."""
    if "|" not in markup or "-" not in markup:
        return markup

    out: list[str] = []
    buffer: list[str] = []
    state = FenceState()

    def flush() -> None:
        if buffer:
            out.append(normalize_table_block("\n".join(buffer)))
            buffer.clear()

    for line in markup.splitlines():
        body = line
        if state.active:
            state.feed(body)
            out.append(line)
            continue
        match = FENCE_RE.match(body)
        if match is not None:
            flush()
            state.feed(body)
            out.append(line)
            continue
        buffer.append(line)
    flush()

    result = "\n".join(out)
    if markup.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _record(applied: list[str], name: str, before: str, after: str) -> str:
    if after != before:
        applied.append(name)
    return after


def normalize_model_markdown(
    markup: str,
    *,
    report: bool = False,
    audit: bool = False,
    trace: bool = False,
) -> str | MarkdownNormalizationResult:
    """Run the full model-markdown repair pipeline in a fixed, testable order."""
    applied: list[str] = []
    current = markup

    next_text = sanitize_ansi(current)
    current = _record(applied, "ansi_sanitized", current, next_text)

    if len(current) > MAX_MARKDOWN_NORMALIZE_BYTES:
        next_text = simplify_markdown_report_icons(current)
        current = _record(applied, "size_guard_icons_only", current, next_text)
        if trace:
            return MarkdownNormalizationResult(current, tuple(applied))
        return current

    from pythinker_code.ui.shell.markdown.audit import detect_audit_report, normalize_audit_report

    use_audit = audit or detect_audit_report(current)
    if use_audit:
        current = _record(applied, "audit_report", current, normalize_audit_report(current))
    else:
        current = _record(
            applied,
            "space_aligned_report_blocks",
            current,
            normalize_space_aligned_report_blocks(current),
        )
    current = _record(
        applied,
        "unwrapped_markdown_table_fence",
        current,
        unwrap_fenced_markdown_tables(current),
    )
    current = _record(
        applied,
        "repaired_crammed_table",
        current,
        repair_crammed_markdown_tables(current),
    )
    current = _record(
        applied,
        "normalized_table",
        current,
        normalize_markdown_tables(current),
    )
    if report:
        current = _record(
            applied,
            "loosened_ordered_list",
            current,
            loosen_tight_ordered_lists(current, min_item_length=80),
        )
    current = _record(
        applied,
        "simplified_icons",
        current,
        simplify_markdown_report_icons(current),
    )

    if trace:
        return MarkdownNormalizationResult(current, tuple(applied))
    return current


# Backward-compatible private aliases used by tests and characterization pins.
_repair_crammed_markdown_tables = repair_crammed_markdown_tables
_unwrap_fenced_markdown_tables = unwrap_fenced_markdown_tables
_normalize_markdown_tables = normalize_markdown_tables
_normalize_table_block = normalize_table_block
_loosen_tight_ordered_lists = loosen_tight_ordered_lists
_simplify_markdown_report_icons = simplify_markdown_report_icons
_normalize_space_aligned_report_blocks = normalize_space_aligned_report_blocks
_parse_aligned_field_line = parse_aligned_field_line

__all__ = [
    "loosen_tight_ordered_lists",
    "normalize_markdown_tables",
    "normalize_model_markdown",
    "normalize_space_aligned_report_blocks",
    "normalize_table_block",
    "parse_aligned_field_line",
    "repair_crammed_markdown_tables",
    "simplify_markdown_report_icons",
    "unwrap_fenced_markdown_tables",
    "_escape_code_span_pipes",
    "_loosen_tight_ordered_lists",
    "_normalize_markdown_tables",
    "_normalize_space_aligned_report_blocks",
    "_normalize_table_block",
    "_parse_aligned_field_line",
    "_repair_crammed_markdown_tables",
    "_simplify_markdown_report_icons",
    "_unwrap_fenced_markdown_tables",
]
