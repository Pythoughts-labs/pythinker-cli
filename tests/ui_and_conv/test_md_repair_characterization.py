# tests/ui_and_conv/test_md_repair_characterization.py
"""Characterization tests that PIN the existing regex Markdown-repair pipeline.

These lock in current correct behavior of _repair_crammed_markdown_tables,
_normalize_markdown_tables, and the priority-matrix detector so any future
change that alters them is caught. Per the spec (§2), this pipeline is pinned,
NOT refactored. If a characterized output looks imperfect, mark it with a
`# pinned: imperfect` note and a follow-up — do not change source here.
"""

from __future__ import annotations

from pythinker_code.ui.shell.components.markdown import (
    _loosen_tight_ordered_lists,
    _normalize_markdown_tables,
    _repair_crammed_markdown_tables,
    pythinker_markdown,
)
from tests.ui_and_conv._md_contract_helpers import render_plain


def test_glued_heading_and_table_header_is_split():
    """Model output that glues a section title to a table header gets split so
    the table renders as a table, not crammed prose."""
    glued = "Medium| # | File |\n| --- | --- |\n| 1 | a.py |\n"
    repaired = _repair_crammed_markdown_tables(glued)
    # The heading is separated onto its own line before the table header.
    assert repaired.splitlines()[0].strip() == "Medium"
    out = render_plain(pythinker_markdown(glued), width=60)
    assert "Medium" in out
    assert "a.py" in out
    assert "File" in out


def test_crammed_data_rows_on_delimiter_line_are_rechunked():
    """Data cells crammed onto the delimiter line are split into rows."""
    crammed = "| # | File |\n| --- | --- || 1 | a.py || 2 | b.py |\n"
    normalized = _normalize_markdown_tables(crammed)
    out = render_plain(pythinker_markdown(normalized), width=60)
    assert "a.py" in out
    assert "b.py" in out


def test_wellformed_table_is_passed_through_unchanged_in_render():
    """A clean table renders with both rows and the header intact."""
    clean = "| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n"
    out = render_plain(pythinker_markdown(clean), width=40)
    for token in ("A", "B", "1", "2", "3", "4"):
        assert token in out

# ---------------------------------------------------------------------------
# _loosen_tight_ordered_lists — behavior specs (not pinned characterization)
# ---------------------------------------------------------------------------


def test_loosen_tight_ol_inserts_blank_between_items():
    inp = "1. First\n2. Second\n3. Third\n"
    out = _loosen_tight_ordered_lists(inp)
    assert out == "1. First\n\n2. Second\n\n3. Third\n"


def test_loosen_tight_ol_skips_already_spaced():
    inp = "1. First\n\n2. Second\n"
    out = _loosen_tight_ordered_lists(inp)
    assert out == "1. First\n\n2. Second\n"


def test_loosen_tight_ol_ignores_inside_fence():
    inp = "```\n1. inside\n2. fence\n```\n"
    out = _loosen_tight_ordered_lists(inp)
    assert out == "```\n1. inside\n2. fence\n```\n"


def test_loosen_tight_ol_preserves_surrounding_text():
    inp = "intro\n1. First\n2. Second\noutro\n"
    out = _loosen_tight_ordered_lists(inp)
    assert out == "intro\n1. First\n\n2. Second\noutro\n"


def test_loosen_tight_ol_unordered_list_unchanged():
    """Unordered list items are not loosened — function is OL-only."""
    inp = "- a\n- b\n- c\n"
    out = _loosen_tight_ordered_lists(inp)
    assert out == "- a\n- b\n- c\n"
