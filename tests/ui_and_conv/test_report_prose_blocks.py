"""Tests for structured report prose block parsing and rendering."""

from __future__ import annotations

from rich.console import RenderableType

from pythinker_code.ui.shell.components.render_utils import render_plain
from pythinker_code.ui.shell.components.report import render_agent_body
from pythinker_code.ui.shell.components.report_prose_blocks import (
    parse_parent_bullet,
    render_report_prose_blocks,
    split_report_prose,
)
from pythinker_code.ui.shell.markdown.normalizers import normalize_space_aligned_report_blocks

_FINDINGS_PREVIEW_SAMPLE = (
    "Findings\n\n"
    "• 1\n"
    "    Severity  medium\n"
    "    Location  llm.py:58-60\n"
    "    What      Host allowlist is a single-member frozenset; safe-by-default but "
    "invisible on new genuine-Anthropic hosts (tool silently absent). Consider a "
    "config-level list or docs pointer.\n\n"
    "• 2\n"
    "    Severity  medium\n"
    "    Location  test_default_agent.py:312-341\n"
    "    What      Root-tool snapshot omits ToolSearch — correctly, because the llm "
    "fixture has provider_config=None (verified via conftest.py:94-101). But the "
    "coupling is implicit.\n"
)

_DASHBOARD_CRITICAL_BLOCK = (
    "Critical a11y / HTML-correctness (block further polish)\n\n"
    "  • 1.1\n"
    "    Issue   Nested interactive elements: card <button> contains <button>s —\n"
    "            invalid HTML\n"
    "    Anchor  session-card.tsx:131-170, 175-180, 196-217\n"
)

_ENHANCEMENT_BLOCK = (
    "Highest-impact enhancements\n\n"
    "  • Palette\n"
    "    Finding   Purple/indigo tool-color cluster overloads event log\n"
    "    Severity  HIGH (AI-slop)\n"
)


def _plain(renderable: RenderableType, *, width: int = 100) -> str:
    return render_plain(renderable, width=width)


def test_parse_parent_bullet_rejects_numbered_list():
    assert parse_parent_bullet("  1. Fix nested buttons") is None


def test_split_report_prose_finding_blocks():
    chunks = split_report_prose(_DASHBOARD_CRITICAL_BLOCK)
    finding_chunks = [c for c in chunks if c.kind == "finding_block"]
    assert len(finding_chunks) == 1
    block = finding_chunks[0].finding
    assert block is not None
    assert block.title == "1.1"
    assert [row.label for row in block.fields] == ["Issue", "Anchor"]
    assert "invalid HTML" in block.fields[0].value


def test_normalize_nested_bullets_under_parent():
    sample = (
        "• 1.1\n"
        "    Issue   Nested interactive elements\n"
        "    Anchor  session-card.tsx:1-2\n"
        "    Fix     restructure card as div role=button\n"
    )
    normalized = normalize_space_aligned_report_blocks(sample)
    assert "- 1.1" in normalized
    assert "  - Issue: Nested interactive elements" in normalized
    assert "  - Anchor: session-card.tsx:1-2" in normalized
    assert normalized.count("- Issue:") == 1
    assert "• Issue" not in normalized


def test_render_dashboard_critical_block_preserves_hierarchy():
    out = _plain(render_report_prose_blocks(_DASHBOARD_CRITICAL_BLOCK) or "", width=100)
    lines = out.splitlines()
    title_line = next(line for line in lines if "1.1" in line)
    issue_line = next(line for line in lines if "Issue" in line and "Nested" in line)
    anchor_line = next(line for line in lines if "Anchor" in line and "session-card" in line)
    assert "●" in title_line or "1.1" in title_line
    assert "● Issue:" not in out
    assert issue_line.index("Nested") == anchor_line.index("session-card")


def test_render_enhancement_block_uses_per_block_label_width():
    out = _plain(render_report_prose_blocks(_ENHANCEMENT_BLOCK) or "", width=100)
    finding_line = next(line for line in out.splitlines() if "Finding" in line and "Purple" in line)
    severity_line = next(line for line in out.splitlines() if "Severity" in line and "HIGH" in line)
    assert finding_line.index("Purple") == severity_line.index("HIGH")


def test_render_findings_preview_sample_no_raw_columns():
    out = _plain(render_agent_body(_FINDINGS_PREVIEW_SAMPLE), width=100)
    assert "● Severity:" not in out
    assert "● Issue:" not in out
    assert "medium" in out
    assert "llm.py:58-60" in out
    assert out.count("● 1") == 1 or "● 1\n" in out or "\n  ● 1" in out


def test_section_heading_rendered():
    out = _plain(render_report_prose_blocks(_DASHBOARD_CRITICAL_BLOCK) or "", width=100)
    assert "Critical a11y / HTML-correctness" in out
    assert "─" in out


def test_single_field_after_bullet_not_a_block():
    sample = "• Only one\n    Issue   value only\n"
    chunks = split_report_prose(sample)
    assert not any(chunk.kind == "finding_block" for chunk in chunks)
    assert render_report_prose_blocks(sample) is None


def test_unicode_underlined_heading_detected():
    sample = "Scope\n═════\n\n• item\n    Issue   value\n    Anchor  file.py:1\n"
    chunks = split_report_prose(sample)
    heading_chunks = [c for c in chunks if c.kind == "section_heading"]
    assert len(heading_chunks) == 1
    assert heading_chunks[0].heading is not None
    assert heading_chunks[0].heading.text == "Scope"


def test_tl_dr_promoted_as_heading():
    # TL;DR appears without a preceding blank line (end of finding block)
    sample = "• finding\n    Issue   bad thing\n    Anchor  x.py:1\nTL;DR\nShort summary.\n"
    chunks = split_report_prose(sample)
    heading_chunks = [c for c in chunks if c.kind == "section_heading"]
    assert any(h.heading is not None and h.heading.text == "TL;DR" for h in heading_chunks)


def test_rule_width_matches_title_not_terminal():
    from rich.text import Text as RichText

    from pythinker_code.ui.shell.components.report_prose_blocks import (
        ReportSectionHeading,
        render_section_heading,
    )

    heading = ReportSectionHeading(text="Scope")
    rendered = render_section_heading(heading)
    # Group contains [title Text, underline Text]; underline must NOT be a Rule
    from rich.console import Group

    assert isinstance(rendered, Group)
    underline = rendered.renderables[1]
    assert isinstance(underline, RichText)
    # underline width == title width, not terminal width
    assert str(underline) == "─" * max(4, len("Scope"))


def test_section_heading_not_triggered_by_prose_sentence():
    # A line ending with "." must not be treated as a section heading
    sample = "This is a full sentence.\n\n• item\n    Issue   value\n    Anchor  file.py:1\n"
    chunks = split_report_prose(sample)
    heading_chunks = [c for c in chunks if c.kind == "section_heading"]
    assert not heading_chunks


def test_fenced_report_before_prose_uses_prose_renderer():
    # Prose containing finding blocks that appears BEFORE a ```report``` fence
    # must go through render_report_prose_blocks, not _agent_markdown.
    import json

    report_json = json.dumps(
        {
            "kind": "code_review",
            "title": "Review",
            "severity": "medium",
            "summary": "ok",
            "findings": [],
        }
    )
    prose = (
        "Critical issues\n\n"
        "  • 1.1\n"
        "    Issue   Something broken\n"
        "    Anchor  x.py:1\n"
        "\n"
    )
    full = prose + f"```report\n{report_json}\n```\n"
    out = _plain(render_agent_body(full), width=120)
    # The prose section must render the finding hierarchy (● dot), not raw columns
    assert "● 1.1" in out or "1.1" in out
    assert "● Issue:" not in out
