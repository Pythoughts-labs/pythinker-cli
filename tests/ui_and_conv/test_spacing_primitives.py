from __future__ import annotations

from prompt_toolkit.formatted_text import StyleAndTextTuples
from rich.text import Text

from pythinker_code.ui.shell import spacing


def test_blank_row_is_empty_text() -> None:
    assert spacing.BLANK_ROW.plain == ""
    assert spacing.blank_row().plain == ""


def test_blank_row_is_not_a_space() -> None:
    # The canonical spacer must be an empty row, never a single space.
    assert spacing.BLANK_ROW.plain != " "
    assert spacing.blank_row().plain != " "


def test_blank_row_returns_fresh_instances() -> None:
    assert spacing.blank_row() is not spacing.blank_row()


def test_append_gap_default_one_row() -> None:
    blocks: list = []
    spacing.append_gap(blocks)
    assert len(blocks) == 1
    assert isinstance(blocks[0], Text)
    assert blocks[0].plain == ""


def test_append_gap_multiple_rows() -> None:
    blocks: list = []
    spacing.append_gap(blocks, rows=3)
    assert len(blocks) == 3
    assert all(isinstance(b, Text) and b.plain == "" for b in blocks)


def test_append_gap_zero_or_negative_is_noop() -> None:
    blocks: list = []
    spacing.append_gap(blocks, rows=0)
    spacing.append_gap(blocks, rows=-2)
    assert blocks == []


def test_padding_constants_have_zero_vertical() -> None:
    # Vertical padding stays 0 so the stream spacer is the only inter-block gap.
    for pad in (
        spacing.CARD_PADDING,
        spacing.TINTED_CARD_PADDING,
        spacing.DIALOG_PANEL_PADDING,
        spacing.WORKLOG_PANEL_PADDING,
        spacing.CODE_BLOCK_PADDING,
    ):
        assert pad[0] == 0


def test_emit_scrollback_block_appends_trailing_blank() -> None:
    from rich.console import Console
    from rich.text import Text

    console = Console(record=True, width=40, color_system=None)
    spacing.emit_scrollback_block(console, Text("tool body"))
    lines = console.export_text().splitlines()
    assert lines[-2].endswith("tool body")
    assert lines[-1] == ""


def test_ensure_prompt_newline_appends_when_missing() -> None:
    fragments: StyleAndTextTuples = [("", "hello")]
    spacing.ensure_prompt_newline(fragments)
    assert fragments[-1] == ("", "\n")


def test_ensure_prompt_newline_noop_when_already_newline() -> None:
    fragments: StyleAndTextTuples = [("", "hello\n")]
    spacing.ensure_prompt_newline(fragments)
    assert len(fragments) == 1


def test_ensure_prompt_newline_noop_when_empty() -> None:
    fragments: StyleAndTextTuples = []
    spacing.ensure_prompt_newline(fragments)
    assert fragments == []
