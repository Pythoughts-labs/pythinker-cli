"""Tests for the standardized report renderer."""

from __future__ import annotations

import pytest
from rich.console import Console

from pythinker_code.ui.shell.components.render_utils import sanitize_ansi
from pythinker_code.ui.shell.components.report import (
    Report,
    ReportFinding,
    parse_report_block,
    render_agent_body,
    render_report,
)


def _plain(renderable, *, width: int = 80) -> str:
    console = Console(width=width, no_color=True, legacy_windows=False)
    with console.capture() as cap:
        console.print(renderable)
    return sanitize_ansi(cap.get())


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _sample_report() -> Report:
    return Report(
        title="Code Review Results",
        scope="Reviewed 17 files across 3 clusters",
        findings=(
            ReportFinding(
                "Inconsistent scroll-indicator fix",
                "medium",
                location="settings_list.py:177-180",
                body="The off-by-one scroll bug is unfixed in the sibling.",
            ),
            ReportFinding("Slow optional fetch blocks login", "medium"),
            ReportFinding("Missing test for erase_when_done", "low", location="selector.py:447"),
            ReportFinding("Fallback chain is well-tested", "info"),
        ),
        note="Most actionable — fix the settings_list scroll bug.",
    )


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_render_report_includes_title_sections_and_locations(theme):
    out = _plain(render_report(_sample_report(), theme=theme))
    assert "Code Review Results" in out
    assert "Reviewed 17 files across 3 clusters" in out
    # section headers for each present severity, omitted for absent ones
    assert "Medium" in out
    assert "Low" in out
    assert "Info" in out
    assert "Critical" not in out
    assert "High" not in out
    # findings + locations
    assert "Inconsistent scroll-indicator fix" in out
    assert "settings_list.py:177-180" in out
    assert "Most actionable" in out


def test_render_report_summary_tally_and_no_critical_high():
    out = _plain(render_report(_sample_report()))
    assert "2 medium" in out
    assert "1 low" in out
    assert "1 info" in out
    assert "no critical or high" in out


def test_render_report_uses_padded_reading_surface():
    out = _plain(render_report(_sample_report()), width=100)

    assert "╭" in out
    assert "Code Review Results" in out
    assert "│  Reviewed 17 files across 3 clusters" in out


def test_render_report_hanging_indents_wrapped_locations():
    report = Report(
        title="Deep Code Scan Results",
        findings=(
            ReportFinding(
                "Pythinker provider loses minimal round-trip",
                "medium",
                location=(
                    "packages/pythinker-core/src/pythinker_core/chat_provider/pythinker.py:138-148; "
                    "packages/pythinker-core/src/pythinker_core/chat_provider/pythinker.py:198-209"
                ),
            ),
        ),
    )

    out = _plain(render_report(report), width=120)
    location_lines = [line for line in out.splitlines() if "pythinker.py" in line]

    assert len(location_lines) >= 2
    assert location_lines[0].index("packages") == location_lines[1].index("packages")


def test_render_report_hang_indents_wrapped_finding_title():
    """A wrapped finding title aligns with the body/location column, not back
    under the ● marker — so a long title never reads as a separate finding."""
    report = Report(
        title="Deep Code Scan Results",
        findings=(
            ReportFinding(
                "disconnect_mcp_server swallows close errors and overwrites info.error on timeout",
                "high",
                location="src/pythinker_code/soul/toolset.py:1378-1385",
                body="The bare except swallows the timeout.",
            ),
        ),
    )

    out = _plain(render_report(report), width=60)
    lines = out.splitlines()
    marker_line = next(line for line in lines if "● disconnect_mcp_server" in line)
    wrap_line = next(line for line in lines if "overwrites" in line and "●" not in line)
    location_line = next(line for line in lines if "toolset.py" in line)

    marker_col = marker_line.index("●")
    title_col = marker_line.index("disconnect_mcp_server")
    # The wrapped title and the location start at the title column, both deeper
    # than the marker — an unambiguous, cohesive finding block.
    assert wrap_line.index("overwrites") == title_col
    assert location_line.index("src/pythinker") == title_col
    assert title_col > marker_col


def test_render_report_groups_in_severity_order_regardless_of_input():
    report = Report(
        title="t",
        findings=(
            ReportFinding("i", "info"),
            ReportFinding("c", "critical"),
            ReportFinding("m", "medium"),
        ),
    )
    out = _plain(render_report(report))
    # Critical section must appear before Medium, which appears before Info.
    assert out.index("Critical") < out.index("Medium") < out.index("Info")
    assert "no critical or high" not in out  # critical present


def test_render_report_empty_findings_is_safe():
    out = _plain(render_report(Report(title="Empty report")))
    assert "Empty report" in out
    assert "no critical or high" in out


# ---------------------------------------------------------------------------
# parse_report_block
# ---------------------------------------------------------------------------


def test_parse_report_block_valid():
    payload = """
    {
      "title": "R",
      "scope": "s",
      "note": "n",
      "findings": [
        {"title": "f1", "severity": "high", "location": "a.py:1", "body": "b"},
        {"title": "f2", "severity": "low"}
      ]
    }
    """
    report = parse_report_block(payload)
    assert report is not None
    assert report.title == "R"
    assert report.scope == "s"
    assert report.note == "n"
    assert len(report.findings) == 2
    assert report.findings[0].severity == "high"
    assert report.findings[0].location == "a.py:1"
    assert report.findings[1].location is None


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",  # not an object
        '{"scope": "x"}',  # missing title
        '{"title": ""}',  # empty title
        '{"title": "t", "findings": "nope"}',  # findings not a list
        '{"title": "t", "findings": [{"title": "f"}]}',  # finding missing severity
        '{"title": "t", "findings": [{"title": "f", "severity": "bogus"}]}',  # bad severity
        '{"title": "t", "findings": ["nope"]}',  # finding not an object
    ],
)
def test_parse_report_block_malformed_returns_none(payload):
    assert parse_report_block(payload) is None


# ---------------------------------------------------------------------------
# render_agent_body — the fenced-block bridge
# ---------------------------------------------------------------------------


def test_render_agent_body_promotes_report_fence():
    text = (
        "Here is the review.\n\n"
        "```report\n"
        '{"title": "My Report", "findings": [{"title": "bug", "severity": "medium"}]}\n'
        "```\n\n"
        "Done."
    )
    out = _plain(render_agent_body(text))
    assert "Here is the review." in out
    assert "My Report" in out
    assert "1 medium" in out  # rendered as a report, not raw JSON
    assert "Done." in out
    assert '"severity"' not in out  # JSON payload not shown verbatim


def test_render_agent_body_invalid_fence_falls_back_to_markdown():
    text = "```report\nthis is not json\n```"
    out = _plain(render_agent_body(text))
    # Left as an ordinary fenced code block — content preserved, not swallowed.
    assert "this is not json" in out


def test_render_agent_body_plain_markdown_unchanged():
    out = _plain(render_agent_body("# Heading\n\nSome **text**."))
    assert "Heading" in out
    assert "text" in out


def test_space_aligned_agent_report_renders_nested_fields():
    sample = (
        "Deep Code Scan Analysis\n"
        "═══════════════════════\n\n"
        "1. Parity Matrix\n\n"
        "• Spawn race guard (ENOENT → LspStartError)\n"
        "    Reference line      LSPClient.ts:111-131\n"
        "    Pythinker location  src/pythinker_code/lsp/client.py:65-73\n"
        "    Status              ✓ exact\n\n"
        "• Initialize handshake\n"
        "    Reference line      LSPServerInstance.ts:167-272\n"
        "    Pythinker location  src/pythinker_code/lsp/instance.py:200-252\n"
        "    Status              ✓ exact\n"
    )
    out = _plain(render_agent_body(sample), width=100)
    assert "Spawn race guard" in out
    assert "Reference line" in out
    assert "LSPClient.ts:111-131" in out
    assert "Pythinker location" in out
    assert "Reference line      LSPClient" not in out


def test_report_markdown_only_h1_is_bold():
    from pythinker_code.ui.theme.adapters.markdown import report_markdown_style_overrides

    overrides = report_markdown_style_overrides()
    assert overrides["markdown.h1"].bold is True
    assert overrides["markdown.h2"].bold is False
    assert overrides["markdown.h3"].bold is False
    assert overrides["markdown.strong"].bold is False
    assert overrides["markdown.item.bullet"].bold is False


def test_render_agent_body_report_prose_gets_section_rhythm():
    text = (
        "Exit codes: both `0`. Only a vendored warning remains.\n"
        "**What this means for the recalled todos:** The named scope is green on `main`.\n"
        "**Cross-check against PR #61 (`15e3342`):** It updated `tests/ui_and_conv/`.\n\n"
        "**Residual unknowns (transparency):**\n"
        "- The old scratch inventory was not persisted.\n"
        "- Nothing in the current scope is red.\n"
        "Next step suggestion: keep the loop closed unless you want a proactive pass.\n"
    )

    out = _plain(render_agent_body(text), width=100)
    lines = out.splitlines()

    assert any(line.strip() == "Exit codes" for line in lines)
    assert any(line.startswith("  both 0.") for line in lines)
    assert "Exit codes: both" not in out
    assert "Cross-check against PR #61 (15e3342)" in out
    assert "`15e3342`" not in out
    assert any(line.startswith("  • The old scratch") for line in lines)
    assert "\n\nWhat this means" in out
    assert "\n\nNext step suggestion" in out


def test_render_agent_body_single_label_stays_plain_markdown():
    out = _plain(render_agent_body("Note: keep this as ordinary prose."), width=100)
    assert "Note: keep this as ordinary prose." in out


def test_streaming_commit_keeps_report_fence_atomic_and_renders():
    """Integration contract for the live shell: the incremental renderer
    (_blocks._flush_committed) commits at markdown_commit_boundary and renders
    the committed slice via render_agent_body. A complete report fence must
    commit whole (not split mid-JSON) so it renders as a report, not raw text.
    """
    from pythinker_code.ui.shell.components.markdown import markdown_commit_boundary

    text = (
        "Here is the review.\n\n"
        "```report\n"
        '{"title": "Code Review Results", "findings": '
        '[{"title": "Slow fetch", "severity": "medium", "location": "x.py:1"}]}\n'
        "```\n\n"
        "Trailing paragraph.\n"
    )
    boundary = markdown_commit_boundary(text)
    assert boundary is not None
    committed = text[:boundary]
    # The closed fence (open + close) is fully inside the committed slice.
    assert committed.count("```") == 2
    out = _plain(render_agent_body(committed))
    assert "Code Review Results" in out
    assert "1 medium" in out
    assert '"severity"' not in out  # rendered as a report, not raw JSON
