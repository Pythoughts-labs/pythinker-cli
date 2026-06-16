"""Regression matrix for the markdown normalization and rendering pipeline."""

from __future__ import annotations

from rich.console import RenderableType

from pythinker_code.ui.shell.components.markdown import (
    PythinkerMarkdown,
    PythinkerMarkdownStream,
    markdown_commit_boundary,
    normalize_model_markdown,
)
from pythinker_code.ui.shell.components.render_utils import render_plain
from pythinker_code.ui.shell.markdown.normalizers import (
    MAX_MARKDOWN_NORMALIZE_BYTES,
    MarkdownNormalizationResult,
    simplify_markdown_report_icons,
    unwrap_fenced_markdown_tables,
)
from pythinker_code.ui.shell.render_constants import MAX_HIGHLIGHT_LINES

_TABLE_BODY = "| Name | Value |\n|------|-------|\n| a    | 1     |\n"


def _plain(renderable: RenderableType, *, width: int = 80) -> str:
    return render_plain(renderable, width=width)


def test_ansi_escape_input_is_sanitized() -> None:
    malicious = "Hello \x1b[31mred\x1b[0m \x1b[2J world"
    out = _plain(PythinkerMarkdown(malicious))
    assert "Hello" in out and "world" in out
    assert "\x1b" not in out


def test_normal_fenced_python_code_stays_fenced() -> None:
    out = _plain(PythinkerMarkdown("```python\nx = 1\n```"))
    assert "x = 1" in out
    assert "╭" in out


def test_md_fence_with_table_gets_unwrapped() -> None:
    markup = f"```markdown\n{_TABLE_BODY}```\n"
    out = unwrap_fenced_markdown_tables(markup)
    assert "```" not in out
    assert "| Name | Value |" in out


def test_md_fence_without_table_stays_code() -> None:
    markup = "```md\n# Just a heading\n\nProse only.\n```\n"
    assert unwrap_fenced_markdown_tables(markup) == markup
    rendered = _plain(PythinkerMarkdown(markup))
    assert "Just a heading" in rendered


def test_table_glued_to_heading_gets_repaired() -> None:
    glued = "Medium| # | File |\n| --- | --- |\n| 1 | a.py |\n"
    out = _plain(PythinkerMarkdown(glued), width=60)
    assert "Medium" in out
    assert "a.py" in out


def test_pipe_inside_inline_code_in_table_is_escaped() -> None:
    md = "| A | B |\n| --- | --- |\n| 1 | `x|y` |\n"
    out = _plain(PythinkerMarkdown(md), width=60)
    assert "x|y" in out or "x" in out


def test_wide_table_stacks() -> None:
    md = (
        "| Item | Reference line | Pythinker location | Status |\n"
        "| --- | --- | --- | --- |\n"
        "| Spawn race guard | LSPClient.ts:111-131 | src/pythinker_code/lsp/client.py:65-73 | exact |\n"
    )
    out = _plain(PythinkerMarkdown(md), width=60)
    assert "Spawn race guard" in out
    assert "Reference line" in out


def test_compact_table_stays_grid() -> None:
    md = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    out = _plain(PythinkerMarkdown(md), width=40)
    for token in ("A", "B", "1", "2"):
        assert token in out


def test_huge_code_block_skips_highlighting() -> None:
    code = "\n".join(f"x = {i}" for i in range(MAX_HIGHLIGHT_LINES + 1))
    out = _plain(PythinkerMarkdown(f"```python\n{code}\n```"), width=100)
    assert "highlighting skipped" in out


def test_streaming_does_not_commit_incomplete_table() -> None:
    full = "Intro.\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\nAfter.\n"
    stream = PythinkerMarkdownStream()
    committed: list[str] = []
    for char in full:
        ready = stream.push(char)
        if ready:
            committed.append(ready)
    tail = stream.flush()
    if tail:
        committed.append(tail)
    for slice_ in committed[:-1]:
        if "---" in slice_:
            assert "| 1 | 2 |" in slice_
    assert "".join(committed) == full


def test_streaming_does_not_commit_incomplete_fenced_code() -> None:
    partial = "Before.\n\n```python\ndef foo():\n    pass"
    boundary = markdown_commit_boundary(partial)
    if boundary is not None:
        assert "```python" not in partial[:boundary]


def test_emoji_outside_code_changes_to_monochrome() -> None:
    out = _plain(PythinkerMarkdown("Status ✅ failed"))
    assert "✓" in out
    assert "✅" not in out


def test_emoji_inside_inline_code_is_preserved() -> None:
    source = "Use `✅` marker"
    result = simplify_markdown_report_icons(source)
    assert "`✅`" in result


def test_emoji_inside_fenced_code_is_preserved() -> None:
    source = "```\n✅ still emoji\n```\n"
    result = simplify_markdown_report_icons(source)
    assert "✅ still emoji" in result


def test_ordered_lists_not_over_spaced_in_normal_mode() -> None:
    text = "1. first\n2. second\n3. third\n"
    normalized = normalize_model_markdown(text, report=False)
    assert isinstance(normalized, str)
    assert normalized == text


def test_normalize_model_markdown_trace_records_passes() -> None:
    sample = "Status ✅\n\n```markdown\n| A | B |\n| --- | --- |\n| 1 | 2 |\n```\n"
    result = normalize_model_markdown(sample, trace=True)
    assert isinstance(result, MarkdownNormalizationResult)
    assert result.text
    assert "simplified_icons" in result.applied


def test_oversize_input_skips_heavy_normalizers() -> None:
    huge = "x" * (MAX_MARKDOWN_NORMALIZE_BYTES + 1) + " ✅"
    result = normalize_model_markdown(huge, trace=True)
    assert isinstance(result, MarkdownNormalizationResult)
    assert "size_guard_icons_only" in result.applied
    assert "✓" in result.text
