"""Tests for audit-report normalization and rendering."""

from __future__ import annotations

from pythinker_code.ui.shell.components.render_utils import render_plain
from pythinker_code.ui.shell.components.report import render_agent_body
from pythinker_code.ui.shell.markdown.audit import (
    compact_known_paths,
    detect_audit_report,
    normalize_audit_report,
    normalize_quote_gutters,
)
from pythinker_code.ui.shell.markdown.renderer import pythinker_report_markdown


def _plain(renderable: object, *, width: int = 100) -> str:
    return render_plain(renderable, width=width)


def test_detect_audit_report_requires_parity_shape() -> None:
    small = (
        "1. Parity Matrix\n\n"
        "• Spawn race guard\n"
        "    Reference line      LSPClient.ts:111-131\n"
        "    Status              ✓ exact\n"
    )
    assert detect_audit_report(small) is False


def test_quote_gutter_becomes_blockquote() -> None:
    source = '▌ "Deferred until a safe global-write path exists."\n'
    out = normalize_quote_gutters(source)
    assert out.startswith("> ")


def test_compact_known_paths_strips_repo_prefix() -> None:
    path = "src/pythinker_code/lsp/client.py:65-73"
    assert compact_known_paths(path) == "lsp/client.py:65-73"


def test_small_parity_report_renders_field_tables() -> None:
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
    assert "LSPClient.ts:111-131" in out
    assert "lsp/client.py:65-73" in out
    assert "Reference line      LSPClient" not in out


def test_large_parity_matrix_collapses_to_summary() -> None:
    rows = []
    for index in range(6):
        rows.append(f"• Contract {index}")
        rows.append(f"    Reference line      ref{index}.ts:{index}")
        rows.append(f"    Pythinker location  src/pythinker_code/a/b{index}.py:{index}")
        rows.append("    Status              ✓ exact")
        rows.append("")
    sample = "1. Parity Matrix\n\n" + "\n".join(rows)
    normalized = normalize_audit_report(sample)
    assert "| ✓ Exact parity | 6 |" in normalized
    assert "Reference line: ref0" not in normalized


def test_audit_report_kind_uses_report_styling() -> None:
    md = pythinker_report_markdown("# Title\n\nBody.", report_kind="audit")
    assert md._report_mode is True


def test_verification_prose_becomes_checklist() -> None:
    sample = (
        "Verification: uv run --directory . pytest tests/tools/test_lsp_*.py -q → 64 passed. "
        "uv run --directory . ruff check src/pythinker_code/lsp/ → All checks passed.\n"
    )
    normalized = normalize_audit_report(
        "Deep Code Scan\n"
        "═══════════════════════\n\n"
        "1. Parity Matrix\n\n"
        + "\n".join(
            [
                "• Item",
                "    Reference line      a.ts:1",
                "    Pythinker location  src/pythinker_code/a.py:1",
                "    Status              ✓ exact",
            ]
            * 3
        )
        + "\n\n"
        + sample
    )
    assert "64 passed" in normalized
    assert "All checks passed" in normalized or "ruff check" in normalized
