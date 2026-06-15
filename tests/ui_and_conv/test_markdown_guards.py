"""Tests for md-fence table unwrapping and the syntax-highlight size guard."""

from __future__ import annotations

from pythinker_code.ui.shell.components.markdown import (
    PythinkerMarkdown,
    _loosen_tight_ordered_lists,
    _unwrap_fenced_markdown_tables,
    pythinker_markdown,
)
from pythinker_code.ui.shell.components.render_utils import render_plain

_TABLE_BODY = "| Name | Value |\n|------|-------|\n| a    | 1     |\n"


# ---------------------------------------------------------------------------
# Fence unwrapping for tables
# ---------------------------------------------------------------------------


def test_md_fence_with_table_unwraps() -> None:
    markup = f"Before\n\n```markdown\n{_TABLE_BODY}```\n\nAfter\n"
    out = _unwrap_fenced_markdown_tables(markup)
    assert "```" not in out
    assert "| Name | Value |" in out
    assert "Before" in out and "After" in out


def test_md_alias_fence_with_table_unwraps() -> None:
    out = _unwrap_fenced_markdown_tables(f"```md\n{_TABLE_BODY}```\n")
    assert "```" not in out
    assert "| Name | Value |" in out


def test_unwrap_pads_blank_lines_against_adjacent_prose() -> None:
    out = _unwrap_fenced_markdown_tables(f"Intro text\n```md\n{_TABLE_BODY}```\nOutro text\n")
    assert "Intro text\n\n| Name" in out
    assert "| a    | 1     |\n\nOutro text" in out


def test_code_fence_with_pipes_is_untouched() -> None:
    markup = f"```python\n{_TABLE_BODY}```\n"
    assert _unwrap_fenced_markdown_tables(markup) == markup


def test_untagged_fence_with_table_is_untouched() -> None:
    markup = f"```\n{_TABLE_BODY}```\n"
    assert _unwrap_fenced_markdown_tables(markup) == markup


def test_md_fence_without_table_is_untouched() -> None:
    markup = "```md\n# Just a heading\n\nProse only.\n```\n"
    assert _unwrap_fenced_markdown_tables(markup) == markup


def test_md_fence_with_separated_header_and_delimiter_is_untouched() -> None:
    # Header and delimiter must be adjacent — a blank line between them means
    # this is not a confident table.
    markup = "```md\n| Name | Value |\n\n|------|-------|\n```\n"
    assert _unwrap_fenced_markdown_tables(markup) == markup


def test_unclosed_md_fence_is_untouched() -> None:
    markup = f"```md\n{_TABLE_BODY}"
    assert _unwrap_fenced_markdown_tables(markup) == markup


def test_markup_without_fences_fast_path() -> None:
    markup = "Just prose with | pipes | here.\n"
    assert _unwrap_fenced_markdown_tables(markup) is markup


def test_tilde_md_fence_with_table_unwraps() -> None:
    out = _unwrap_fenced_markdown_tables(f"~~~markdown\n{_TABLE_BODY}~~~\n")
    assert "~~~" not in out
    assert "| Name | Value |" in out


def test_md_fence_inside_other_fence_is_untouched() -> None:
    # A ```md line inside a ~~~ fence is content, not a fence opener.
    markup = f"~~~\n```md\n{_TABLE_BODY}```\n~~~\n"
    assert _unwrap_fenced_markdown_tables(markup) == markup


def test_info_string_fence_inside_other_fence_does_not_close_it() -> None:
    # CommonMark closing fences carry no info string, so a ```python line
    # inside an open ``` fence is content — the md fence after it must still
    # be treated as fence interior, not unwrapped.
    markup = f"```\n```python\n```md\n{_TABLE_BODY}```\n```\n"
    assert _unwrap_fenced_markdown_tables(markup) == markup


def test_pythinker_markdown_applies_unwrap_pass() -> None:
    md = PythinkerMarkdown(f"```markdown\n{_TABLE_BODY}```\n")
    assert "```" not in md.markup
    out = render_plain(md, width=60)
    # Renders as a table (grid borders), not as a fenced code block.
    assert "Name" in out and "Value" in out
    assert "markdown" not in out  # no code-block language label


# ---------------------------------------------------------------------------
# Syntax-highlight size guard
# ---------------------------------------------------------------------------


def test_huge_code_block_skips_highlighting_with_notice() -> None:
    code = "\n".join(f"x = {i}" for i in range(10_001))
    out = render_plain(pythinker_markdown(f"```python\n{code}\n```"), width=100)
    assert "highlighting skipped" in out
    assert "10,001 lines" in out


def test_small_code_block_still_highlights_without_notice() -> None:
    out = render_plain(pythinker_markdown("```python\nx = 1\n```"), width=80)
    assert "highlighting skipped" not in out
    assert "x = 1" in out


# ---------------------------------------------------------------------------
# _loosen_tight_ordered_lists
# ---------------------------------------------------------------------------


def test_tight_ol_gets_blank_lines_between_items() -> None:
    text = "1. first\n2. second\n3. third\n"
    result = _loosen_tight_ordered_lists(text)
    assert result == "1. first\n\n2. second\n\n3. third\n"


def test_already_loose_ol_unchanged() -> None:
    text = "1. first\n\n2. second\n"
    assert _loosen_tight_ordered_lists(text) == text


def test_single_ol_item_unchanged() -> None:
    text = "1. only item\n"
    assert _loosen_tight_ordered_lists(text) == text


def test_ol_inside_fence_not_loosened() -> None:
    text = "```\n1. inside fence\n2. still inside\n```\n1. outside\n2. outside too\n"
    result = _loosen_tight_ordered_lists(text)
    # Items inside the fence must stay tight; items outside get the blank line
    assert "1. inside fence\n2. still inside" in result
    assert "1. outside\n\n2. outside too" in result


def test_unordered_list_not_affected() -> None:
    text = "- bullet one\n- bullet two\n"
    assert _loosen_tight_ordered_lists(text) == text


def test_ol_mixed_with_prose_inserts_only_between_items() -> None:
    text = "Intro.\n1. first\n2. second\nOutro.\n"
    result = _loosen_tight_ordered_lists(text)
    assert result == "Intro.\n1. first\n\n2. second\nOutro.\n"
