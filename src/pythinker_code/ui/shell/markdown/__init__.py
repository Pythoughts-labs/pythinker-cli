"""Pythinker markdown renderer package.

Public entry points for themed Rich markdown rendering, model-output repair,
and streaming commit boundaries.
"""

from __future__ import annotations

from pythinker_code.ui.shell.markdown.audit import detect_audit_report, normalize_audit_report
from pythinker_code.ui.shell.markdown.normalizers import (
    MAX_MARKDOWN_NORMALIZE_BYTES,
    MarkdownNormalizationResult,
    normalize_model_markdown,
)
from pythinker_code.ui.shell.markdown.renderer import (
    PythinkerMarkdown,
    pythinker_markdown,
    pythinker_report_markdown,
)
from pythinker_code.ui.shell.markdown.streaming import (
    MAX_STREAM_PARSE_BYTES,
    PythinkerMarkdownStream,
    markdown_commit_boundary,
)

__all__ = [
    "MarkdownNormalizationResult",
    "MAX_MARKDOWN_NORMALIZE_BYTES",
    "MAX_STREAM_PARSE_BYTES",
    "PythinkerMarkdown",
    "PythinkerMarkdownStream",
    "detect_audit_report",
    "markdown_commit_boundary",
    "normalize_audit_report",
    "normalize_model_markdown",
    "pythinker_markdown",
    "pythinker_report_markdown",
]
