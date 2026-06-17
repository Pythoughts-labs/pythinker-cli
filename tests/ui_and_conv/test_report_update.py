"""Tests for structured report-update TUI rendering."""

from __future__ import annotations

from pythinker_code.ui.shell.components.render_utils import sanitize_ansi
from pythinker_code.ui.shell.components.report import render_agent_body
from pythinker_code.ui.shell.components.report_update import (
    parse_report_update,
    render_report_update,
)


def _plain(renderable, *, width: int = 100) -> str:
    from rich.console import Console

    console = Console(width=width, no_color=True, legacy_windows=False)
    with console.capture() as cap:
        console.print(renderable)
    return sanitize_ansi(cap.get())


_SAMPLE = """\
Report update complete. Summary of what changed:

Files modified:

• .pythinker/reports/multi-agent-orchestration-scan.md (598 → 662 lines)
• .pythinker/reports/multi-agent-orchestration-scan-verification.md (new, 151 lines)

Corrections applied:

• 1
  Severity  High
  Item      M4 (isConcurrencySafe)
  Fix       TS-only concept; reframed as port-or-document

• 2
  Severity  Med
  Item      Subagent count
  Fix       12 → 13; §1, §5, §8 updated

• 3
  Severity  Med
  Item      H1 test path
  Fix       tests/subagents/ → tests/core/

Out-of-scope flags (recorded in verification memo, not fixed here):

• AGENTS.md still has stale "12 built-ins" count — needs separate PR
• tools/agent/__init__.py at 1035 lines is approaching a soft ceiling

Method: Read-only verification. No source files modified; only the report and a new companion memo
were written. All claims in the corrections were verified against live source on
feat/tui-streaming-pr @ dfcc6b7.
"""


def test_parse_report_update_extracts_sections() -> None:
    update = parse_report_update(_SAMPLE)
    assert update is not None
    assert len(update.files) == 2
    assert update.files[0].kind == "modified"
    assert update.files[1].kind == "created"
    assert len(update.corrections) == 3
    assert update.corrections[0].severity == "high"
    assert update.corrections[0].item.startswith("M4")
    assert len(update.followups) == 2
    assert update.branch == "feat/tui-streaming-pr"
    assert update.commit == "dfcc6b7"
    assert update.scope == "reports only; no source files changed"


def test_render_report_update_collapsed_is_compact() -> None:
    update = parse_report_update(_SAMPLE)
    assert update is not None
    out = _plain(render_report_update(update, expanded=False), width=100)
    assert "✓ Report update complete" in out
    assert "Summary" in out
    assert "2 files updated" in out
    assert "3 corrections applied" in out
    assert "multi-agent-orchestration-scan.md" in out
    assert "598 → 662" in out
    assert "High" in out
    assert "M4" in out
    assert "expand all 3 corrections" in out.lower()
    assert "Severity  High" not in out


def test_render_report_update_expanded_shows_table() -> None:
    update = parse_report_update(_SAMPLE)
    assert update is not None
    out = _plain(render_report_update(update, expanded=True), width=120)
    assert "Corrections applied" in out
    assert "Subagent count" in out
    assert "tests/subagents/" in out


def test_render_agent_body_promotes_report_update() -> None:
    out = _plain(render_agent_body(_SAMPLE), width=100)
    assert "✓ Report update complete" in out
    assert "Follow-ups" in out
    assert "AGENTS.md" in out
    assert "Files modified:" not in out


def test_non_report_update_stays_markdown() -> None:
    out = _plain(render_agent_body("Here is a normal assistant answer."), width=80)
    assert "Report update complete" not in out
    assert "Here is a normal assistant answer." in out
