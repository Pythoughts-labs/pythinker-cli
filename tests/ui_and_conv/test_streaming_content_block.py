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
    _tail_lines,
    _truncate_to_display_width,
)
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
