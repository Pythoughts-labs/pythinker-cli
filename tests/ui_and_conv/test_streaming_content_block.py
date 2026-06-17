"""Tests for streaming content block: incremental markdown commitment,
token estimation, and related utilities."""

from __future__ import annotations

import pytest
from rich.console import Console
from rich.style import Style
from rich.text import Text

from pythinker_code.ui.shell.visualize import (
    _ContentBlock,
    _estimate_tokens,
    _find_committed_boundary,
    _normalize_streaming_preview_text,
    _tail_lines,
    _truncate_to_display_width,
    _wrap_preview_line,
)
from pythinker_code.ui.shell.visualize._blocks import FlushReason
from pythinker_code.ui.theme import tui_rich_style

# ---------------------------------------------------------------------------
# _estimate_tokens
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_english_text(self):
        # ~1 token per 4 chars
        assert _estimate_tokens("Hello world!") == pytest.approx(3.0)

    def test_empty_string(self):
        assert _estimate_tokens("") == 0.0

    def test_returns_float(self):
        """Ensure float is returned so callers can accumulate without truncation."""
        result = _estimate_tokens("abc")
        assert isinstance(result, float)
        assert result == pytest.approx(0.75)

    def test_small_chunk_accumulation(self):
        """100 x 3-char chunks should accumulate to ~75 tokens, not 0."""
        total = sum(_estimate_tokens("abc") for _ in range(100))
        assert int(total) == 75

    def test_single_char_accumulation(self):
        total = sum(_estimate_tokens("a") for _ in range(100))
        assert int(total) == 25


# ---------------------------------------------------------------------------
# _find_committed_boundary
# ---------------------------------------------------------------------------


class TestFindCommittedBoundary:
    def test_two_paragraphs(self):
        text = "First paragraph.\n\nSecond paragraph."
        boundary = _find_committed_boundary(text)
        assert boundary is not None
        assert text[:boundary].strip() == "First paragraph."

    def test_single_block_returns_none(self):
        assert _find_committed_boundary("Just one paragraph.") is None

    def test_empty_string(self):
        assert _find_committed_boundary("") is None

    def test_list_as_single_block(self):
        """An entire list must be treated as one block, not split per item."""
        text = "Intro.\n\n- item 1\n- item 2\n- item 3\n\nAfter."
        boundary = _find_committed_boundary(text)
        assert boundary is not None
        confirmed = text[:boundary]
        # Both intro and entire list should be confirmed.
        assert "Intro." in confirmed
        assert "item 1" in confirmed
        assert "item 3" in confirmed
        # "After." should remain as pending.
        assert "After." in text[boundary:]

    def test_incomplete_fence(self):
        """An unclosed code fence should not be committed."""
        text = "Before.\n\n```python\ndef foo():"
        boundary = _find_committed_boundary(text)
        assert boundary is not None
        confirmed = text[:boundary]
        assert "Before." in confirmed
        # The unclosed fence stays as pending.
        assert "```python" in text[boundary:]

    def test_complete_fence(self):
        text = "Before.\n\n```python\nprint(1)\n```\n\nAfter."
        boundary = _find_committed_boundary(text)
        assert boundary is not None
        confirmed = text[:boundary]
        assert "print(1)" in confirmed
        assert "After." in text[boundary:]

    def test_blockquote_as_single_block(self):
        text = "Intro.\n\n> line 1\n> line 2\n\nAfter."
        boundary = _find_committed_boundary(text)
        assert boundary is not None
        confirmed = text[:boundary]
        assert "line 1" in confirmed
        assert "line 2" in confirmed
        assert "After." in text[boundary:]

    def test_table_as_single_block(self):
        text = "Before.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nAfter."
        boundary = _find_committed_boundary(text)
        assert boundary is not None
        confirmed = text[:boundary]
        assert "| a | b |" in confirmed
        assert "After." in text[boundary:]

    def test_heading_then_paragraph(self):
        text = "# Title\n\nParagraph."
        boundary = _find_committed_boundary(text)
        assert boundary is not None
        assert "# Title" in text[:boundary]

    def test_hr(self):
        text = "Before.\n\n---\n\nAfter."
        boundary = _find_committed_boundary(text)
        assert boundary is not None
        assert "Before." in text[:boundary]

    def test_only_newlines(self):
        assert _find_committed_boundary("\n\n\n") is None

    def test_boundary_is_exact_char_offset(self):
        """Returned offset must allow text[:offset] + text[offset:] == text."""
        text = "AAA.\n\nBBB.\n\nCCC."
        boundary = _find_committed_boundary(text)
        assert boundary is not None
        assert text[:boundary] + text[boundary:] == text


# ---------------------------------------------------------------------------
# _tail_lines
# ---------------------------------------------------------------------------
# _truncate_to_display_width
# ---------------------------------------------------------------------------


class TestTruncateToDisplayWidth:
    def test_short_ascii_unchanged(self):
        assert _truncate_to_display_width("Hello", 30) == "Hello"

    def test_long_ascii_truncated(self):
        result = _truncate_to_display_width("A" * 50, 20)
        from rich.cells import cell_len

        assert cell_len(result) <= 20
        assert result.endswith("...")

    def test_empty_string(self):
        assert _truncate_to_display_width("", 10) == ""

    def test_exact_fit(self):
        """Line that exactly fills max_width should not be truncated."""
        assert _truncate_to_display_width("abcde", 5) == "abcde"


# ---------------------------------------------------------------------------
# _tail_lines
# ---------------------------------------------------------------------------


class TestTailLines:
    def test_basic(self):
        text = "a\nb\nc\nd\ne"
        assert _tail_lines(text, 2) == "d\ne"

    def test_fewer_lines_than_requested(self):
        assert _tail_lines("a\nb", 10) == "a\nb"

    def test_no_newlines(self):
        assert _tail_lines("hello", 5) == "hello"

    def test_exact_count(self):
        text = "1\n2\n3"
        assert _tail_lines(text, 3) == "1\n2\n3"

    def test_empty_string(self):
        assert _tail_lines("", 3) == ""

    def test_large_text(self):
        text = "\n".join(f"line {i}" for i in range(1000))
        tail = _tail_lines(text, 3)
        assert tail == "line 997\nline 998\nline 999"


# ---------------------------------------------------------------------------
# _ContentBlock integration
# ---------------------------------------------------------------------------


class TestContentBlockTokenCount:
    """Verify token accumulation works correctly with small chunks."""

    def test_small_english_chunks(self):
        block = _ContentBlock(is_think=True)
        for _ in range(100):
            block.append("abc")
        # 300 chars / 4 = 75 tokens
        assert int(block._token_count) == 75

    def test_mixed_accumulation(self):
        block = _ContentBlock(is_think=True)
        block.append("Hi")  # 0.5
        block.append("world")  # 1.25
        assert block._token_count == pytest.approx(1.75)

    def test_activity_snapshot_uses_recent_token_rate_window(self, monkeypatch):
        import importlib

        blocks_mod = importlib.import_module("pythinker_code.ui.shell.visualize._blocks")

        class Clock:
            now = 0.0

            def monotonic(self) -> float:
                return self.now

        clock = Clock()
        monkeypatch.setattr(blocks_mod.time, "monotonic", clock.monotonic)

        block = _ContentBlock(is_think=False)
        block.append("a" * 40)  # 10 tokens
        assert block._activity_snapshot("Composing").token_rate is None

        clock.now = 0.5
        block.append("b" * 40)  # 20 cumulative tokens, 2 samples
        assert block._activity_snapshot("Composing").token_rate is None

        clock.now = 1.0
        block.append("c" * 40)  # 30 cumulative tokens over the 0.0-1.0s window
        assert block._activity_snapshot("Composing").token_rate == 20

        clock.now = 2.0
        block.append("d" * 40)  # Oldest sample is trimmed; use the recent 1.5s window.
        assert block._activity_snapshot("Composing").token_rate == 13


def test_composing_live_label_uses_professional_activity_wording():
    block = _ContentBlock(is_think=False)
    block.append("hello")
    renderable = block.compose()
    console = Console(record=True, width=120, color_system=None)
    console.print(renderable)
    output = console.export_text()

    assert "Composing" in output
    assert "tokens" in output
    assert "hello" in output


def test_thinking_status_line_uses_compact_activity_metadata():
    block = _ContentBlock(is_think=True)
    block.append("reasoning")
    console = Console(record=True, width=120, color_system=None)
    console.print(block.compose())
    output = console.export_text()
    assert "Thinking… (" in output
    assert "tokens" in output
    assert "esc to interrupt" not in output


def _assert_blank_line_after_activity(output: str, label: str) -> None:
    lines = output.splitlines()
    try:
        activity_index = next(index for index, line in enumerate(lines) if label in line)
    except StopIteration:
        raise AssertionError(f"Label '{label}' not found in output") from None
    assert activity_index + 1 < len(lines), f"Label '{label}' is the last line in output"
    assert lines[activity_index + 1].strip() == ""


def test_assert_blank_line_after_activity_reports_missing_label() -> None:
    with pytest.raises(AssertionError, match="Label 'Missing' not found in output"):
        _assert_blank_line_after_activity("Composing\n", "Missing")


def test_assert_blank_line_after_activity_reports_missing_following_line() -> None:
    with pytest.raises(AssertionError, match="Label 'Composing' is the last line in output"):
        _assert_blank_line_after_activity("Composing\n", "Composing")


def test_composing_committed_prose_has_gap_before_spinner() -> None:
    """Staged paragraphs must not run flush into the Composing activity line."""
    block = _ContentBlock(is_think=False)
    block.append("First paragraph here.\n\nSecond paragraph here.\n\n")
    block.append("Third still streaming")
    assert block._committed_renderables
    console = Console(record=True, width=120, color_system=None)
    console.print(block.compose())
    lines = [line.rstrip() for line in console.export_text().splitlines()]
    first_idx = next(i for i, line in enumerate(lines) if "First paragraph" in line)
    composing_idx = next(i for i, line in enumerate(lines) if "Composing" in line)
    assert composing_idx > first_idx
    assert composing_idx - first_idx >= 2
    assert any(lines[j] == "" for j in range(first_idx + 1, composing_idx))


def test_composing_preview_does_not_double_blank_after_commit_boundary() -> None:
    """Pending text that starts with "\\n" after a commit boundary must not
    produce a second blank row in the transient Live region.
    """
    block = _ContentBlock(is_think=False)
    block.append("First paragraph here.\n\nSecond paragraph here.\n\n")
    block.append("\nThird still streaming")
    assert block._committed_renderables

    console = Console(record=True, width=120, color_system=None)
    console.print(block.compose())
    output = console.export_text()

    assert "Composing" in output
    assert "Third still streaming" in output
    lines = output.splitlines()
    activity_index = next(i for i, line in enumerate(lines) if "Composing" in line)
    assert activity_index + 1 < len(lines)
    assert lines[activity_index + 1].strip() == ""
    if activity_index + 2 < len(lines):
        assert lines[activity_index + 2].strip() != "", (
            "Second blank row after 'Composing' — leading '\\n' in pending "
            "text is leaking through the preview path."
        )


def test_composing_preview_has_standard_gap_after_activity_line(monkeypatch):
    from pythinker_code.ui.shell.visualize import _blocks as blocks_module

    block = _ContentBlock(is_think=False)
    block.append("live preview without newline")
    # Pin the blink to its visible phase: the streaming marker blinks while
    # the block is live and turns solid green only on commit.
    monkeypatch.setattr(blocks_module.time, "monotonic", lambda: 0.0)
    console = Console(record=True, width=120, color_system=None)
    console.print(block.compose())
    output = console.export_text()

    _assert_blank_line_after_activity(output, "Composing")
    assert "\n\n⏺ live preview without newline" in output


def test_paced_composing_preview_renders_complete_inline_markdown(monkeypatch):
    from pythinker_code.ui.shell.visualize import _blocks as blocks_module

    block = _ContentBlock(is_think=False, paced=True)
    block.append("**Planning agent tasks**")
    block.reveal_all()
    monkeypatch.setattr(blocks_module.time, "monotonic", lambda: 0.0)
    console = Console(record=True, width=120, color_system=None)
    console.print(block.compose())
    output = console.export_text()

    assert "Planning agent tasks" in output
    # Live preview uses plain Text; delimiters stay visible until finalize.
    assert "**Planning agent tasks**" in output


def test_paced_composing_preview_keeps_incomplete_inline_markdown_plain(monkeypatch):
    from pythinker_code.ui.shell.visualize import _blocks as blocks_module

    block = _ContentBlock(is_think=False, paced=True)
    block.append("**Planning agent")
    block.reveal_all()
    monkeypatch.setattr(blocks_module.time, "monotonic", lambda: 0.0)
    console = Console(record=True, width=120, color_system=None)
    console.print(block.compose())
    output = console.export_text()

    assert "**Planning agent" in output


def test_thinking_stream_preview_has_standard_gap_after_activity_line():
    block = _ContentBlock(is_think=True, show_thinking_stream=True)
    block.append("reasoning preview")
    console = Console(record=True, width=120, color_system=None)
    console.print(block.compose())

    _assert_blank_line_after_activity(console.export_text(), "Thinking")


def test_thinking_stream_preview_uses_transcript_bullet_after_activity_line():
    block = _ContentBlock(is_think=True, show_thinking_stream=True)
    block.append("**Preparing report generation**")
    console = Console(record=True, width=120, color_system=None)
    console.print(block.compose())
    output = console.export_text()

    assert "Thinking" in output
    assert "\n\n⏺ **Preparing report generation**" in output


def _style_for(renderable: Text, text: str) -> Style:
    start = renderable.plain.index(text)
    end = start + len(text)
    span = next(span for span in renderable.spans if span.start <= start and span.end >= end)
    return Style.parse(span.style) if isinstance(span.style, str) else span.style


def test_composing_and_thinking_labels_are_neutral_grey():
    # Both Composing and Thinking read as neutral thinking grey, never the
    # bright activity-label white or purple-tinted muted color.
    thinking_grey = tui_rich_style("thinking_text").color
    muted = tui_rich_style("muted").color
    bright = tui_rich_style("activity_label").color

    composing = _ContentBlock(is_think=False)
    composing.append("hello")
    composing_renderable = composing._compose_spinner()
    assert isinstance(composing_renderable, Text)
    assert _style_for(composing_renderable, "Composing").color == thinking_grey
    assert _style_for(composing_renderable, "Composing").color != muted
    assert _style_for(composing_renderable, "Composing").color != bright

    thinking = _ContentBlock(is_think=True)
    thinking.append("reasoning")
    thinking_renderable = thinking.compose()
    assert isinstance(thinking_renderable, Text)
    thinking_style = _style_for(thinking_renderable, "Thinking")
    assert thinking_style.color == thinking_grey
    assert thinking_style.color != muted
    assert thinking_style.color != bright
    assert thinking_style.italic


class TestContentBlockCommitment:
    """Verify incremental commitment for composing blocks."""

    def test_thinking_never_commits(self):
        block = _ContentBlock(is_think=True)
        block.append("First.\n\nSecond.\n\nThird.")
        assert block._committed_len == 0

    def test_composing_commits_on_newline(self):
        block = _ContentBlock(is_think=False)
        block.append("First paragraph.\n\nSecond paragraph.\n\nThird.")
        assert block._committed_len > 0
        pending = block.raw_text[block._committed_len :]
        assert "Third." in pending

    def test_report_fence_continuation_keeps_gap_after_streamed_prose(self):
        output_console = Console(record=True, width=100, color_system=None)

        block = _ContentBlock(is_think=False)
        block.append("Deep scan completed. Full report saved here:\n")
        block.append("  .pythinker/reports/deep-code-scan.md\n")
        block.append('```report\n{"title": "Deep Code Scan Results", "findings": []}\n```\n\n')
        output_console.print(block.compose_final())
        output = output_console.export_text()

        assert "Deep Code Scan Results" in output

    def test_streamed_prose_blocks_match_single_pass_spacing(self):
        """Regression: streamed multi-paragraph bodies used to render every
        paragraph crammed onto consecutive lines. Each committed block and the
        final tail must keep the one-row gap a single markdown pass puts
        between blocks."""
        rec = Console(record=True, width=80, color_system=None)

        block = _ContentBlock(is_think=False)
        body = (
            "First paragraph here.\n\nSecond paragraph here.\n\nThird and final paragraph here.\n"
        )
        for ch in body:  # char-by-char: the worst case for commit seams
            block.append(ch)
        rec.print(block.compose_final())

        lines = [line.rstrip() for line in rec.export_text().splitlines()]
        markers = ("First paragraph", "Second paragraph", "Third and final")
        idxs = [next(i for i, line in enumerate(lines) if marker in line) for marker in markers]
        for first, second in zip(idxs, idxs[1:], strict=False):
            assert second - first >= 2, f"paragraphs at lines {first},{second} are crammed"
            assert any(lines[j] == "" for j in range(first + 1, second))

    def test_composing_no_commit_without_newline(self):
        block = _ContentBlock(is_think=False)
        block.append("just some text without newlines")
        assert block._committed_len == 0

    def test_composing_previews_pending_text_without_committing(self):
        block = _ContentBlock(is_think=False)
        block.append("live preview without newline")
        console = Console(record=True, width=120, color_system=None)
        console.print(block.compose())

        assert "live preview without newline" in console.export_text()
        assert block._committed_len == 0
        assert not block.has_emitted_to_scrollback

    def test_composing_preview_is_tail_limited(self):
        block = _ContentBlock(is_think=False)
        block.append("\n".join(f"line {i:02d}" for i in range(1, 21)))
        console = Console(record=True, width=120, color_system=None)
        console.print(block.compose())
        output = console.export_text()

        assert "line 20" in output
        assert "line 01" not in output

    def test_newline_split_across_chunks(self):
        """Block boundary \\n\\n split across two chunks should still commit."""
        block = _ContentBlock(is_think=False)
        block.append("First paragraph.\n")
        assert block._committed_len == 0  # Only one \n so far, can't form boundary
        block.append("\nSecond paragraph.\n")
        # Now pending has "First paragraph.\n\nSecond paragraph.\n"
        # But _flush_committed needs 2 blocks — "First paragraph.\n\n" + "Second paragraph.\n"
        # The second \n chunk triggers the check.
        block.append("\nThird.")
        # Now should have committed first two paragraphs
        assert block._committed_len > 0

    def test_has_pending(self):
        block = _ContentBlock(is_think=False)
        block.append("Para 1.\n\nPara 2.\n\nPara 3.")
        assert block.has_pending()

    def test_bullet_printed_once(self):
        block = _ContentBlock(is_think=False)
        assert not block._has_printed_bullet
        block.append("First.\n\nSecond.\n\nThird.")
        # After first commit, bullet should be marked as printed
        if block._committed_len > 0:
            assert block._has_printed_bullet


# ---------------------------------------------------------------------------
# show_thinking_stream toggle (legacy streaming reasoning preview)
# ---------------------------------------------------------------------------


class TestProductionPathBoundaryContract:
    """Validate that _ContentBlock never commits a GFM table header row
    without its data row."""

    _FULL_TABLE = "Intro paragraph.\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\nAfter.\n"

    @staticmethod
    def _assert_no_mid_table_commit(block: _ContentBlock) -> None:
        # Check at the offset layer — never at rendered output (Rich converts
        # GFM table syntax to box-drawing chars, so "---" won't appear in text).
        table_header_start = block.raw_text.find("| A | B |")
        table_data_end_idx = block.raw_text.find("| 1 | 2 |")
        if table_header_start == -1 or table_data_end_idx == -1:
            return
        table_data_end = table_data_end_idx + len("| 1 | 2 |")
        committed_at = block._committed_len
        assert not (table_header_start < committed_at < table_data_end), (
            f"header-only table committed at offset {committed_at} "
            f"before data row arrived (data row ends at {table_data_end})"
        )

    def test_unpaced_composing_block_does_not_commit_table_mid_row(self):
        block = _ContentBlock(is_think=False)
        for ch in self._FULL_TABLE:
            block.append(ch)
            self._assert_no_mid_table_commit(block)

    def test_paced_composing_block_does_not_commit_table_mid_row(self):
        block = _ContentBlock(is_think=False, paced=True)
        for ch in self._FULL_TABLE:
            block.append(ch)
            block.reveal_tick()
            self._assert_no_mid_table_commit(block)
        while block.reveal_tick():
            self._assert_no_mid_table_commit(block)


class TestShowThinkingStream:
    """The ``show_thinking_stream`` flag opts back into the pre-1.32 behavior
    where thinking content is rendered as a 6-line scrolling preview during
    streaming and committed to history as full markdown when the block ends.
    The default (``False``) keeps the compact 'Thinking ...' indicator and
    one-line ``✻ Cogitated for ...`` trace introduced in 1.32.
    """

    def test_compact_mode_compose_returns_compact_text(self):
        from rich.text import Text

        block = _ContentBlock(is_think=True)
        block.append("Some reasoning content")
        result = block.compose()
        assert isinstance(result, Text)
        assert "Thinking" in result.plain
        # Compact mode never renders the raw reasoning text
        assert "reasoning content" not in result.plain

    def test_stream_mode_compose_returns_group_with_preview(self):
        from rich.console import Group

        block = _ContentBlock(is_think=True, show_thinking_stream=True)
        block.append("line 1\nline 2\nline 3")
        result = block.compose()
        assert isinstance(result, Group)

    def test_stream_mode_compose_no_pending_returns_status_text(self):
        from rich.text import Text

        block = _ContentBlock(is_think=True, show_thinking_stream=True)
        result = block.compose()
        assert isinstance(result, Text)
        assert "Thinking" in result.plain

    def test_stream_mode_status_includes_token_count(self):
        """Stream mode shows token count after thinking content arrives."""
        from rich.console import Group
        from rich.text import Text

        block = _ContentBlock(is_think=True, show_thinking_stream=True)
        block.append("reasoning content")
        result = block.compose()
        assert isinstance(result, Group)
        result = result.renderables[0]
        assert isinstance(result, Text)
        plain = result.plain
        assert "Thinking" in plain
        assert "tokens" in plain

    def test_compact_mode_compose_final_returns_trace_line(self):
        from rich.text import Text

        block = _ContentBlock(is_think=True)
        block.append("Some thought content")
        result = block.compose_final()
        assert isinstance(result, Text)
        assert result.plain.startswith("✻ Cogitated for ")
        assert "tokens" not in result.plain
        # Compact trace must not contain the raw reasoning content
        assert "thought content" not in result.plain

    def test_stream_mode_compose_final_returns_markdown_bullet(self):
        """Stream mode commits the full reasoning to history (legacy behavior)."""
        from pythinker_code.utils.rich.columns import BulletColumns

        block = _ContentBlock(is_think=True, show_thinking_stream=True)
        block.append("Some thought content")
        result = block.compose_final()
        assert isinstance(result, BulletColumns)

    def test_stream_mode_compose_final_empty_returns_empty_text(self):
        from rich.text import Text

        block = _ContentBlock(is_think=True, show_thinking_stream=True)
        result = block.compose_final()
        assert isinstance(result, Text)
        assert result.plain == ""

    def test_compact_mode_has_pending_with_content(self):
        block = _ContentBlock(is_think=True)
        block.append("anything")
        assert block.has_pending()

    def test_compact_mode_has_pending_without_content(self):
        block = _ContentBlock(is_think=True)
        assert not block.has_pending()

    def test_stream_mode_has_pending_with_content(self):
        block = _ContentBlock(is_think=True, show_thinking_stream=True)
        block.append("anything")
        assert block.has_pending()

    def test_stream_mode_has_pending_without_content(self):
        block = _ContentBlock(is_think=True, show_thinking_stream=True)
        assert not block.has_pending()

    def test_thinking_never_commits_in_either_mode(self):
        """Thinking blocks must never commit incrementally regardless of mode."""
        for stream in (False, True):
            block = _ContentBlock(is_think=True, show_thinking_stream=stream)
            block.append("First.\n\nSecond.\n\nThird.")
            assert block._committed_len == 0

    def test_preview_constant_is_six_lines(self):
        """Stream preview window matches the historical 6-line tail."""
        from pythinker_code.ui.shell.visualize._blocks import _THINKING_PREVIEW_LINES

        assert _THINKING_PREVIEW_LINES == 6

    def test_show_thinking_stream_ignored_for_composing_blocks(self):
        """The flag only affects thinking blocks — composing path is unchanged."""
        block_off = _ContentBlock(is_think=False, show_thinking_stream=False)
        block_on = _ContentBlock(is_think=False, show_thinking_stream=True)
        for block in (block_off, block_on):
            block.append("hello\n\nworld")
        # Both should commit identically
        assert block_off._committed_len == block_on._committed_len


# ---------------------------------------------------------------------------
# Space-aligned report preview wrapping
# ---------------------------------------------------------------------------

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


def _preview_orphan_lines(output: str) -> list[str]:
    """Lines that look like wrap fragments stranded at column 0."""
    orphans: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or line.startswith((" ", "•", "⏺", "├", "└", "│", "-")):
            continue
        if stripped.split()[0].lower() in {
            "but",
            "llm",
            "arrowly",
            "cycle.",
            "invisible",
            "fixture",
        }:
            orphans.append(line)
    return orphans


class TestSpaceAlignedPreviewWrapping:
    def test_normalize_preview_converts_space_columns_to_list_fields(self):
        normalized = _normalize_streaming_preview_text(_FINDINGS_PREVIEW_SAMPLE)
        assert "- Severity: medium" in normalized
        assert "Severity  medium" not in normalized

    def test_wrap_preview_line_hangs_continuation_indent(self):
        line = (
            "    What      Host allowlist is a single-member frozenset; safe-by-default but "
            "invisible on new genuine-Anthropic hosts."
        )
        wrapped = _wrap_preview_line(line, 72)
        assert wrapped.startswith("    What")
        assert "\nbut invisible" not in wrapped
        assert "\n    invisible" in wrapped or "\n    but invisible" in wrapped

    def test_composing_preview_has_no_orphan_wrap_fragments(self, monkeypatch):
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        monkeypatch.setattr(blocks_module, "current_console_width", lambda: 72)
        block = _ContentBlock(is_think=False)
        block.append(_FINDINGS_PREVIEW_SAMPLE)
        console = Console(record=True, width=72, color_system=None)
        console.print(block.compose())
        output = console.export_text()
        assert _preview_orphan_lines(output) == []

    def test_finalize_scrollback_uses_normalized_render_not_raw_columns(self, monkeypatch):
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        monkeypatch.setattr(blocks_module, "current_console_width", lambda: 72)
        block = _ContentBlock(is_think=False)
        block.append(_FINDINGS_PREVIEW_SAMPLE)
        block.reveal_all()
        block._flush_committed()
        renderable = block.promote_to_scrollback()
        assert renderable is not None
        console = Console(record=True, width=72, color_system=None)
        console.print(renderable)
        output = console.export_text()
        assert "Severity: medium" in output
        assert "Severity  medium" not in output
        assert _preview_orphan_lines(output) == []


# ---------------------------------------------------------------------------
# Incomplete ```report fence suppression in the active streaming preview
# ---------------------------------------------------------------------------
# Root cause: the transient preview renders the uncommitted pending tail as
# plain text. An unterminated ```report fence is held in the pending buffer
# (markdown can't commit an open fence), so the raw findings JSON would leak
# token-by-token. The preview suppresses only the *open* report fence; ordinary
# fences keep streaming and the finalized report panel is unchanged.
# See tasks/streaming-render-rootcause.md.

_PARTIAL_REPORT_STREAM = (
    "Verification\n\n"
    "Findings:\n\n"
    "```report\n"
    '{"title": "LSP module review", "findings": [\n'
    '  {"title": "Diagnostic dedup", "severity": "low", '
    '"location": "x.py:1", "body": "details'
)

_COMPLETE_REPORT_STREAM = (
    "Findings:\n\n"
    "```report\n"
    '{"title": "LSP module review", "findings": '
    '[{"title": "Diagnostic dedup", "severity": "low", '
    '"location": "x.py:1", "body": "details"}]}\n'
    "```\n\n"
    "Overall the module is in good shape.\n"
)

_JSON_LEAK_TOKENS = ('"title"', '"severity"', '"location"', '"body"', "},", "{")


class TestReportFenceSuppression:
    def test_helper_replaces_open_report_body_with_placeholder(self):
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        out = blocks_module._suppress_unclosed_report_fence_preview(_PARTIAL_REPORT_STREAM)
        assert "collecting findings" in out
        assert "Findings:" in out
        assert "```report" not in out
        for token in _JSON_LEAK_TOKENS:
            assert token not in out, f"{token!r} leaked through suppression"

    def test_helper_leaves_closed_report_block_untouched(self):
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        out = blocks_module._suppress_unclosed_report_fence_preview(_COMPLETE_REPORT_STREAM)
        assert out == _COMPLETE_REPORT_STREAM

    def test_helper_ignores_ordinary_code_fence(self):
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        text = 'Example:\n\n```python\nprint("hello")'
        assert blocks_module._suppress_unclosed_report_fence_preview(text) == text

    def test_helper_no_report_fence_is_noop(self):
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        text = "Just some prose with no fences at all."
        assert blocks_module._suppress_unclosed_report_fence_preview(text) == text

    def test_normalize_preview_suppresses_open_report_fence(self):
        normalized = _normalize_streaming_preview_text(_PARTIAL_REPORT_STREAM)
        assert "collecting findings" in normalized
        for token in _JSON_LEAK_TOKENS:
            assert token not in normalized

    def test_composing_preview_hides_partial_report_json(self):
        block = _ContentBlock(is_think=False)
        for i in range(0, len(_PARTIAL_REPORT_STREAM), 17):
            block.append(_PARTIAL_REPORT_STREAM[i : i + 17])
        console = Console(record=True, width=100, color_system=None)
        console.print(block.compose())
        output = console.export_text()

        assert "Composing" in output
        assert "Findings:" in output  # streamed prose before the fence stays visible
        assert "collecting findings" in output
        for token in _JSON_LEAK_TOKENS:
            assert token not in output, f"{token!r} leaked into the active preview"

    def test_paced_composing_preview_hides_partial_report_json(self):
        block = _ContentBlock(is_think=False, paced=True)
        for i in range(0, len(_PARTIAL_REPORT_STREAM), 13):
            block.append(_PARTIAL_REPORT_STREAM[i : i + 13])
        for _ in range(200):
            if not block.reveal_tick():
                break
        console = Console(record=True, width=100, color_system=None)
        console.print(block.compose())
        output = console.export_text()
        for token in _JSON_LEAK_TOKENS:
            assert token not in output, f"{token!r} leaked into the paced preview"

    def test_ordinary_code_fence_still_streams_in_preview(self):
        """Closed ordinary fences stream through the preview untouched.

        Only the *open* body is held back; a fully-closed ```` ```python ````
        block must reach the preview verbatim because the finalize path will
        commit the whole block in one go. (See ``TestCodeFenceSuppression``
        for the open-fence contract.)
        """
        block = _ContentBlock(is_think=False)
        text = 'Example:\n\n```python\nprint("hello")\n```'
        for ch in text:
            block.append(ch)
        console = Console(record=True, width=100, color_system=None)
        console.print(block.compose())
        assert 'print("hello")' in console.export_text()

    def test_completed_report_still_renders_clean_panel(self):
        block = _ContentBlock(is_think=False)
        for ch in _COMPLETE_REPORT_STREAM:
            block.append(ch)
        renderable = block.promote_to_scrollback()
        assert renderable is not None
        console = Console(record=True, width=100, color_system=None)
        console.print(renderable)
        output = console.export_text()
        # Final scrollback is the clean panel: title + finding visible, raw JSON gone.
        assert "LSP module review" in output
        assert "Diagnostic dedup" in output
        assert '"severity"' not in output
        assert "collecting findings" not in output


# ---------------------------------------------------------------------------
# Open ```python / ```ts / ```json … fence suppression in the active preview
# ---------------------------------------------------------------------------
# Root cause (paired with ``tasks/streaming-render-rootcause.md``): the transient
# composing preview renders the uncommitted pending tail as plain text. An
# unterminated ```` ```python ```` fence is held in the pending buffer (markdown
# can't commit an open fence), so the raw code — including long hard-coded
# paths, the unclosed ```` ``` ```` marker, and the streaming caret — would
# otherwise leak token-by-token and wrap badly inside the Live area. We hold
# the open body back behind a stable placeholder; once the matching closer
# arrives the helper returns the text unchanged so the finalize path commits
# the full block. ```` ```report ```` is intentionally excluded — it has its
# own, more specific suppression so streaming findings JSON does not flash a
# misleading "code block" placeholder mid-report.

_OPEN_PYTHON_FENCE_STREAM = (
    "Evidence: The diff adds:\n\n"
    "```python\n"
    "_AGENT_DEBUG_LOG_PATH = '/Users/panda/Projects/active/Projects/pythinker-code-main/.cursor/debug-e13c80.log'\n"
    "_FENCE_OPEN_RE = re.compile(r'(?m)^(```|~~~)([^\\n]*)$')\n"
)

_CLOSED_PYTHON_FENCE_STREAM = (
    "Evidence: The diff adds:\n\n```python\n_PATH = '/tmp/example.log'\nprint(_PATH)\n```\n"
)

_TILDE_OPEN_FENCE_STREAM = "Intro:\n\n~~~ts\nconst x: number = 1;\n"
_TILDE_CLOSED_FENCE_STREAM = "Intro:\n\n~~~ts\nconst x: number = 1;\n~~~\n"

_NO_LANG_OPEN_FENCE_STREAM = "Intro:\n\n```\nplain text inside fence\n"

_CODE_LEAK_TOKENS = (
    "_AGENT_DEBUG_LOG_PATH",
    "_FENCE_OPEN_RE",
    "re.compile",
    "/Users/panda/Projects",
)


class TestCodeFenceSuppression:
    def test_helper_replaces_open_python_body_with_placeholder(self):
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        out = blocks_module._suppress_unclosed_code_fence_preview(_OPEN_PYTHON_FENCE_STREAM)
        assert "Evidence: The diff adds:" in out
        assert "streaming code block" in out
        assert "python" in out  # language tag surfaces in the placeholder
        assert "```python" not in out
        for token in _CODE_LEAK_TOKENS:
            assert token not in out, f"{token!r} leaked through suppression"

    def test_helper_leaves_closed_code_block_untouched(self):
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        out = blocks_module._suppress_unclosed_code_fence_preview(_CLOSED_PYTHON_FENCE_STREAM)
        assert out == _CLOSED_PYTHON_FENCE_STREAM
        assert "_PATH" in out
        assert "print(_PATH)" in out

    def test_helper_supports_tilde_fence(self):
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        open_out = blocks_module._suppress_unclosed_code_fence_preview(_TILDE_OPEN_FENCE_STREAM)
        assert "const x" not in open_out
        assert "streaming code block" in open_out
        assert "ts" in open_out

        closed_out = blocks_module._suppress_unclosed_code_fence_preview(_TILDE_CLOSED_FENCE_STREAM)
        assert closed_out == _TILDE_CLOSED_FENCE_STREAM

    def test_helper_leaves_bare_fence_line_untouched(self):
        """A bare triple-backtick line is structurally a closer in this
        codebase (``_FENCE_CLOSE_RE``), so an opener without a language tag
        cannot be told apart from a closer. The helper intentionally
        suppresses only fences that carry a language tag; otherwise it would
        risk eating real closers. Confirms the conservative contract.
        """
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        out = blocks_module._suppress_unclosed_code_fence_preview(_NO_LANG_OPEN_FENCE_STREAM)
        assert out == _NO_LANG_OPEN_FENCE_STREAM

    def test_helper_does_not_touch_report_fence(self):
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        out = blocks_module._suppress_unclosed_code_fence_preview(_PARTIAL_REPORT_STREAM)
        # ```report is excluded; raw JSON must reach the report-suppression stage.
        assert "```report" in out

    def test_helper_no_fence_is_noop(self):
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        text = "Just some prose with no fences at all."
        assert blocks_module._suppress_unclosed_code_fence_preview(text) == text

    def test_normalize_preview_suppresses_open_code_fence(self):
        normalized = _normalize_streaming_preview_text(_OPEN_PYTHON_FENCE_STREAM)
        assert "streaming code block" in normalized
        for token in _CODE_LEAK_TOKENS:
            assert token not in normalized

    def test_composing_preview_hides_open_python_fence(self):
        block = _ContentBlock(is_think=False)
        block.append(_OPEN_PYTHON_FENCE_STREAM)
        console = Console(record=True, width=100, color_system=None)
        console.print(block.compose())
        output = console.export_text()

        assert "Composing" in output
        assert "Evidence: The diff adds:" in output
        assert "streaming code block" in output
        for token in _CODE_LEAK_TOKENS:
            assert token not in output, f"{token!r} leaked into the active preview"
        # The raw open fence marker must not be shown mid-stream.
        assert "```python" not in output

    def test_composing_preview_keeps_closed_python_fence_visible(self):
        block = _ContentBlock(is_think=False)
        for ch in _CLOSED_PYTHON_FENCE_STREAM:
            block.append(ch)
        console = Console(record=True, width=100, color_system=None)
        console.print(block.compose())
        output = console.export_text()

        assert "Composing" in output
        assert "_PATH" in output
        assert "print(_PATH)" in output
        assert "```python" in output

    def test_paced_composing_preview_hides_open_python_fence(self):
        block = _ContentBlock(is_think=False, paced=True)
        block.append(_OPEN_PYTHON_FENCE_STREAM)
        for _ in range(200):
            if not block.reveal_tick():
                break
        console = Console(record=True, width=100, color_system=None)
        console.print(block.compose())
        output = console.export_text()

        for token in _CODE_LEAK_TOKENS:
            assert token not in output, f"{token!r} leaked into the paced preview"
        assert "```python" not in output

    def test_composing_status_is_not_in_committed_body(self):
        """The Composing status row is pinned above the preview — it must never
        become part of the committed markdown body that scrolls into history.
        """
        block = _ContentBlock(is_think=False)
        for ch in _OPEN_PYTHON_FENCE_STREAM:
            block.append(ch)
        # Stage some committed prose before the fence.
        block.append("Evidence: The diff adds:\n\n")
        assert block._committed_renderables
        # The Composing label is rendered by ``_compose_spinner``; verify it
        # never appears inside the committed renderables.
        for renderable in block._committed_renderables:
            console = Console(record=True, width=100, color_system=None)
            console.print(renderable)
            assert "Composing" not in console.export_text()

    def test_finalize_scrollback_still_contains_full_code_block(self):
        block = _ContentBlock(is_think=False)
        for ch in _CLOSED_PYTHON_FENCE_STREAM:
            block.append(ch)
        renderable = block.promote_to_scrollback()
        assert renderable is not None
        console = Console(record=True, width=100, color_system=None)
        console.print(renderable)
        output = console.export_text()
        assert "_PATH" in output
        assert "print(_PATH)" in output
        assert "streaming code block" not in output


_PARTIAL_INTERRUPTED_REPORT = (
    'Verification\n\nFindings:\n\n```report\n{"title": "LSP module review", "findings": [{"title":'
)


class TestFinalizeContinuity:
    def test_promote_to_scrollback_is_idempotent(self) -> None:
        block = _ContentBlock(is_think=False)
        block.append("Hello from the assistant.\n")
        first = block.promote_to_scrollback()
        second = block.promote_to_scrollback()
        assert first is not None
        assert second is None
        assert block.is_promoted

    def test_flush_content_uses_single_promotion(self) -> None:
        from unittest.mock import patch

        from pythinker_code.ui.shell.visualize._live_view import _LiveView
        from pythinker_code.wire.types import StatusUpdate

        view = _LiveView(StatusUpdate())
        view._current_content_block = _ContentBlock(is_think=False)
        view._current_content_block.append("one-shot promotion test")
        with patch.object(
            view._current_content_block,
            "promote_to_scrollback",
            wraps=view._current_content_block.promote_to_scrollback,
        ) as promote:
            view.flush_content()
            assert promote.call_count == 1

    def test_final_report_does_not_flash_raw_preview(self) -> None:
        block = _ContentBlock(is_think=False)
        for ch in _COMPLETE_REPORT_STREAM:
            block.append(ch)
        block.prepare_for_finalize(FlushReason.TURN_END)
        block._flush_committed()
        renderable = block.promote_to_scrollback()
        assert renderable is not None
        console = Console(record=True, width=100, color_system=None)
        console.print(renderable)
        output = console.export_text()
        assert "LSP module review" in output
        assert "collecting findings" not in output
        assert '"severity"' not in output


class TestCancelMidReportFence:
    def test_sanitize_final_replaces_open_report_body(self) -> None:
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        out = blocks_module._sanitize_unclosed_report_fence_for_final(_PARTIAL_INTERRUPTED_REPORT)
        assert "Report generation was interrupted" in out
        assert "Verification" in out
        assert "Findings:" in out
        assert "```report" not in out
        for token in _JSON_LEAK_TOKENS:
            assert token not in out

    def test_cancel_mid_report_fence_final_does_not_leak_json(self) -> None:
        block = _ContentBlock(is_think=False)
        block.append(_PARTIAL_INTERRUPTED_REPORT)
        block.prepare_for_finalize(FlushReason.CANCEL)
        renderable = block.promote_to_scrollback()
        assert renderable is not None
        console = Console(record=True, width=100, color_system=None)
        console.print(renderable)
        output = console.export_text()
        assert "interrupted" in output.lower()
        for token in _JSON_LEAK_TOKENS:
            assert token not in output

    def test_closed_report_final_unchanged(self) -> None:
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        out = blocks_module._sanitize_unclosed_report_fence_for_final(_COMPLETE_REPORT_STREAM)
        assert out == _COMPLETE_REPORT_STREAM

    def test_unclosed_python_fence_final_unchanged(self) -> None:
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        text = 'Example:\n\n```python\nprint("hello")'
        assert blocks_module._sanitize_unclosed_report_fence_for_final(text) == text

    def test_interrupted_report_does_not_render_fake_panel(self) -> None:
        block = _ContentBlock(is_think=False)
        block.append(_PARTIAL_INTERRUPTED_REPORT)
        block.prepare_for_finalize(FlushReason.CANCEL)
        renderable = block.promote_to_scrollback()
        assert renderable is not None
        console = Console(record=True, width=100, color_system=None)
        console.print(renderable)
        output = console.export_text()
        assert "LSP module review" not in output
        assert "interrupted" in output.lower()


class TestPreviewCache:
    def test_preview_cache_reuses_identical_frame(self) -> None:
        block = _ContentBlock(is_think=False)
        block.append("cache me once")
        pending = block._pending_text()
        first = block._build_preview_cached(pending, max_lines=12, reserve_caret=True)
        second = block._build_preview_cached(pending, max_lines=12, reserve_caret=True)
        assert first == second
        assert block._preview_text_cache_key is not None

    def test_preview_cache_invalidates_on_append(self) -> None:
        block = _ContentBlock(is_think=False)
        block.append("first")
        pending = block._pending_text()
        block._build_preview_cached(pending, max_lines=12, reserve_caret=True)
        key_before = block._preview_text_cache_key
        block.append(" second")
        assert block._preview_text_cache_key is None or block._preview_text_cache_key != key_before

    def test_preview_cache_preserves_report_suppression(self) -> None:
        block = _ContentBlock(is_think=False)
        block.append(_PARTIAL_REPORT_STREAM)
        pending = block._pending_text()
        preview = block._build_preview_cached(pending, max_lines=12, reserve_caret=True)
        assert "collecting findings" in preview
        for token in _JSON_LEAK_TOKENS:
            assert token not in preview


# ---------------------------------------------------------------------------
# Active preview row budget — prompt_toolkit preamble clipping
# ---------------------------------------------------------------------------

_TERMINAL_COLUMNS = 80
_TERMINAL_ROWS = 24


def _tui_design_report_stream() -> str:
    """Long assistant TUI report with prose, a boxed layer map, and a trailing section."""
    tree = "\n".join(
        (
            "╭────────────────────────────╮",
            "│ ui/                        │",
            "│ shell/                     │",
            "│ visualize/                 │",
            "│ _blocks.py                 │",
            "│ _live_view.py              │",
            "│ _interactive.py            │",
            "│ prompt.py                  │",
            "╰────────────────────────────╯",
        )
    )
    return (
        "Pythinker TUI Design & Render Subsystem\n\n"
        "1. TUI Architecture\n\n"
        "The interactive shell routes wire events through visualize and prompt_toolkit "
        "layers. Committed markdown blocks accumulate in the transient preamble while "
        "only the pending tail streams in the live preview.\n\n"
        "2. Render Layer Map\n\n"
        f"{tree}\n\n"
        "3. More sections follow with additional streaming content here.\n"
    )


def _stream_tui_report_to_mid_box(block: _ContentBlock) -> str:
    """Stream through section 2 until the box panel is partially pending."""
    text = _tui_design_report_stream()
    cut = text.index("│ _interactive.py")
    for ch in text[:cut]:
        block.append(ch)
    return text


def _fit_agent_status_like_prompt(
    ansi: str,
    *,
    columns: int = _TERMINAL_COLUMNS,
    terminal_rows: int = _TERMINAL_ROWS,
    pinned: str = "Actioning…",
) -> str:
    from prompt_toolkit.formatted_text import FormattedText, to_formatted_text

    from pythinker_code.ui.shell.prompt import CustomPromptSession, _prompt_preamble_max_rows

    max_rows = _prompt_preamble_max_rows(terminal_rows)
    clipped = CustomPromptSession._fit_preamble_with_pinned_tail(
        to_formatted_text(ansi),
        FormattedText([("", f"{pinned}\n")]),
        columns,
        max_rows,
    )
    return "".join(fragment for _, fragment, *_ in clipped)


def _interactive_body_row_budget(terminal_rows: int = _TERMINAL_ROWS) -> int:
    from pythinker_code.ui.shell.prompt import _prompt_preamble_max_rows

    return max(1, _prompt_preamble_max_rows(terminal_rows) - 1)


_PARTIAL_VISUAL_BOX_STREAM = (
    "2. Render Layer Map\n\n"
    "╭────────────────────────────╮\n"
    "│ ui/                        │\n"
    "│ shell/                     │\n"
    "│ visualize/                 │\n"
    "│ _blocks.py                 │\n"
)

_COMPLETE_VISUAL_BOX_STREAM = (
    "2. Render Layer Map\n\n"
    "╭────────────────────────────╮\n"
    "│ ui/                        │\n"
    "│ shell/                     │\n"
    "╰────────────────────────────╯\n"
)


class TestVisualBlockHoldback:
    def test_helper_replaces_open_box_with_placeholder(self) -> None:
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        out = blocks_module._suppress_unclosed_visual_block_preview(_PARTIAL_VISUAL_BOX_STREAM)
        assert "formatting diagram" in out
        assert "Render Layer Map" in out
        assert "╭" not in out
        assert "│ ui/" not in out

    def test_helper_leaves_closed_box_untouched(self) -> None:
        from pythinker_code.ui.shell.visualize import _blocks as blocks_module

        out = blocks_module._suppress_unclosed_visual_block_preview(_COMPLETE_VISUAL_BOX_STREAM)
        assert out == _COMPLETE_VISUAL_BOX_STREAM

    def test_normalize_preview_suppresses_open_box(self) -> None:
        normalized = _normalize_streaming_preview_text(_PARTIAL_VISUAL_BOX_STREAM)
        assert "formatting diagram" in normalized
        assert "╭" not in normalized

    def test_composing_preview_hides_partial_box_lines(self) -> None:
        block = _ContentBlock(is_think=False)
        block.append(_PARTIAL_VISUAL_BOX_STREAM)
        console = Console(record=True, width=100, color_system=None)
        console.print(block.compose())
        output = console.export_text()

        assert "Render Layer Map" in output
        assert "formatting diagram" in output
        assert "╭" not in output
        assert "│ ui/" not in output

    def test_completed_box_still_streams_in_preview(self) -> None:
        block = _ContentBlock(is_think=False)
        block.append(_COMPLETE_VISUAL_BOX_STREAM)
        console = Console(record=True, width=100, color_system=None)
        console.print(block.compose())
        output = console.export_text()

        assert "╭" in output
        assert "╰" in output


class TestActivePreviewRowBudget:
    """Active preview stays within the interactive prompt row budget."""

    def test_compose_fits_preamble_body_budget_when_budget_set(self) -> None:
        from pythinker_code.ui.shell.console import render_to_ansi

        block = _ContentBlock(is_think=False)
        _stream_tui_report_to_mid_box(block)
        block.set_preview_row_budget(_interactive_body_row_budget())
        ansi = render_to_ansi(block.compose(), columns=_TERMINAL_COLUMNS)

        assert len(ansi.splitlines()) <= _interactive_body_row_budget()

    def test_prompt_fit_does_not_cut_box_panel_when_budget_set(self) -> None:
        from pythinker_code.ui.shell.console import render_to_ansi

        block = _ContentBlock(is_think=False)
        _stream_tui_report_to_mid_box(block)
        block.set_preview_row_budget(_interactive_body_row_budget())
        ansi = render_to_ansi(block.compose(), columns=_TERMINAL_COLUMNS)
        clipped = _fit_agent_status_like_prompt(ansi)

        assert "output clipped to fit terminal" not in clipped
        assert "Render Layer Map" in clipped
        assert "╭" not in clipped
        assert "formatting diagram" in clipped

    def test_unbudgeted_compose_can_still_exceed_preamble(self) -> None:
        from pythinker_code.ui.shell.console import render_to_ansi

        architecture = "\n\n".join(
            f"Architecture note {i}: routes wire events through visualize and prompt_toolkit "
            f"layers with enough prose to grow the transient preamble."
            for i in range(1, 8)
        )
        tree = "\n".join(
            (
                "╭────────────────────────────╮",
                "│ ui/                        │",
                "│ shell/                     │",
                "│ visualize/                 │",
            )
        )
        text = (
            "Pythinker TUI Design & Render Subsystem\n\n"
            "1. TUI Architecture\n\n"
            f"{architecture}\n\n"
            "2. Render Layer Map\n\n"
            f"{tree}\n"
        )
        block = _ContentBlock(is_think=False)
        block.append(text[: text.index("│ visualize/")])
        ansi = render_to_ansi(block.compose(), columns=_TERMINAL_COLUMNS)

        assert len(ansi.splitlines()) > _interactive_body_row_budget()

    def test_finalize_scrollback_stays_full_while_preview_is_compact(self) -> None:
        from pythinker_code.ui.shell.console import render_to_ansi

        block = _ContentBlock(is_think=False)
        text = _stream_tui_report_to_mid_box(block)
        block.set_preview_row_budget(_interactive_body_row_budget())
        mid_stream = render_to_ansi(block.compose(), columns=_TERMINAL_COLUMNS)
        clipped = _fit_agent_status_like_prompt(mid_stream)

        assert "output clipped to fit terminal" not in clipped
        assert "╭" not in clipped

        for ch in text[len(block.raw_text) :]:
            block.append(ch)
        renderable = block.promote_to_scrollback()
        assert renderable is not None
        final = render_to_ansi(renderable, columns=_TERMINAL_COLUMNS)
        assert "╰" in final
        assert "More sections follow" in final


class TestActivePreviewRowBudgetRepro:
    """Legacy repro assertions — kept to guard Rich Live / unbudgeted paths."""

    def test_unbudgeted_preview_can_still_show_partial_box_before_holdback_only(self) -> None:
        """Holdback removes partial boxes even without a row budget."""
        from pythinker_code.ui.shell.console import render_to_ansi

        block = _ContentBlock(is_think=False)
        _stream_tui_report_to_mid_box(block)
        ansi = render_to_ansi(block.compose(), columns=_TERMINAL_COLUMNS)

        assert "formatting diagram" in ansi
        assert "╭" not in ansi
