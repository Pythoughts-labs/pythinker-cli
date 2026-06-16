"""Backward-compatible re-exports for the markdown package.

Prefer importing from :mod:`pythinker_code.ui.shell.markdown` in new code.
"""

# pyright: reportPrivateUsage=false
# This shim intentionally re-exports private internals for backward compat.
from __future__ import annotations

from pythinker_code.ui.shell.markdown import (
    MAX_MARKDOWN_NORMALIZE_BYTES,
    MAX_STREAM_PARSE_BYTES,
    MarkdownNormalizationResult,
    PythinkerMarkdown,
    PythinkerMarkdownStream,
    markdown_commit_boundary,
    normalize_model_markdown,
    pythinker_markdown,
    pythinker_report_markdown,
)
from pythinker_code.ui.shell.markdown.elements import _BorderedCodeBlock, _ReportTableElement
from pythinker_code.ui.shell.markdown.normalizers import (
    _escape_code_span_pipes,
    _loosen_tight_ordered_lists,
    _normalize_markdown_tables,
    _normalize_space_aligned_report_blocks,
    _normalize_table_block,
    _parse_aligned_field_line,
    _repair_crammed_markdown_tables,
    _simplify_markdown_report_icons,
    _unwrap_fenced_markdown_tables,
    loosen_tight_ordered_lists,
    normalize_markdown_tables,
    normalize_space_aligned_report_blocks,
    normalize_table_block,
    parse_aligned_field_line,
    repair_crammed_markdown_tables,
    simplify_markdown_report_icons,
    unwrap_fenced_markdown_tables,
)
from pythinker_code.ui.shell.markdown.renderer import _markdown_style_overrides
from pythinker_code.ui.shell.markdown.streaming import (
    _get_md_parser,
    _markdown_commit_boundary_cached,
)

__all__ = [
    "MarkdownNormalizationResult",
    "MAX_MARKDOWN_NORMALIZE_BYTES",
    "MAX_STREAM_PARSE_BYTES",
    "PythinkerMarkdown",
    "PythinkerMarkdownStream",
    "markdown_commit_boundary",
    "normalize_model_markdown",
    "pythinker_markdown",
    "pythinker_report_markdown",
]

# Private re-exports consumed by tests and characterization pins.
__all__ += [
    "_BorderedCodeBlock",
    "_ReportTableElement",
    "_escape_code_span_pipes",
    "_get_md_parser",
    "_loosen_tight_ordered_lists",
    "_markdown_commit_boundary_cached",
    "_markdown_style_overrides",
    "_normalize_markdown_tables",
    "_normalize_space_aligned_report_blocks",
    "_normalize_table_block",
    "_parse_aligned_field_line",
    "_repair_crammed_markdown_tables",
    "_simplify_markdown_report_icons",
    "_unwrap_fenced_markdown_tables",
    "loosen_tight_ordered_lists",
    "normalize_markdown_tables",
    "normalize_space_aligned_report_blocks",
    "normalize_table_block",
    "parse_aligned_field_line",
    "repair_crammed_markdown_tables",
    "simplify_markdown_report_icons",
    "unwrap_fenced_markdown_tables",
]
