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
from pythinker_code.ui.shell.markdown import elements as _md_elements
from pythinker_code.ui.shell.markdown import normalizers as _md_normalizers
from pythinker_code.ui.shell.markdown import renderer as _md_renderer
from pythinker_code.ui.shell.markdown import streaming as _md_streaming

_BorderedCodeBlock = _md_elements._BorderedCodeBlock
_ReportTableElement = _md_elements._ReportTableElement
_escape_code_span_pipes = _md_normalizers._escape_code_span_pipes
_loosen_tight_ordered_lists = _md_normalizers._loosen_tight_ordered_lists
_normalize_markdown_tables = _md_normalizers._normalize_markdown_tables
_normalize_space_aligned_report_blocks = _md_normalizers._normalize_space_aligned_report_blocks
_normalize_table_block = _md_normalizers._normalize_table_block
_parse_aligned_field_line = _md_normalizers._parse_aligned_field_line
_repair_crammed_markdown_tables = _md_normalizers._repair_crammed_markdown_tables
_simplify_markdown_report_icons = _md_normalizers._simplify_markdown_report_icons
_unwrap_fenced_markdown_tables = _md_normalizers._unwrap_fenced_markdown_tables
loosen_tight_ordered_lists = _md_normalizers.loosen_tight_ordered_lists
normalize_markdown_tables = _md_normalizers.normalize_markdown_tables
normalize_space_aligned_report_blocks = _md_normalizers.normalize_space_aligned_report_blocks
normalize_table_block = _md_normalizers.normalize_table_block
parse_aligned_field_line = _md_normalizers.parse_aligned_field_line
repair_crammed_markdown_tables = _md_normalizers.repair_crammed_markdown_tables
simplify_markdown_report_icons = _md_normalizers.simplify_markdown_report_icons
unwrap_fenced_markdown_tables = _md_normalizers.unwrap_fenced_markdown_tables
_markdown_style_overrides = _md_renderer._markdown_style_overrides
_get_md_parser = _md_streaming._get_md_parser
_markdown_commit_boundary_cached = _md_streaming._markdown_commit_boundary_cached

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

# Keep characterization-pin re-exports reachable; referenced so static analysis
# treats the module-level bindings as intentionally exported, not dead code.
_REEXPORT_REGISTRY: tuple[object, ...] = (
    _BorderedCodeBlock,
    _ReportTableElement,
    _escape_code_span_pipes,
    _get_md_parser,
    _loosen_tight_ordered_lists,
    _markdown_commit_boundary_cached,
    _markdown_style_overrides,
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
