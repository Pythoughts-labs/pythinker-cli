"""Streaming markdown commit boundary helpers."""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from markdown_it import MarkdownIt

MAX_STREAM_PARSE_BYTES = 16_000
_SENTENCE_END = (".", "!", "?", ";")
_SELF_CLOSING_BLOCKS = frozenset(("fence", "code_block", "hr", "html_block"))

_md_parser: MarkdownIt | None = None


def _get_md_parser() -> MarkdownIt:
    global _md_parser
    if _md_parser is None:
        from markdown_it import MarkdownIt

        _md_parser = MarkdownIt().enable("strikethrough").enable("table")
    return _md_parser


def _markdown_commit_boundary_uncached(text: str) -> int | None:
    md = _get_md_parser()
    tokens = md.parse(text)

    block_maps: list[list[int]] = []
    depth = 0
    for token in tokens:
        if token.nesting == 1:
            if depth == 0 and token.map is not None:
                block_maps.append(token.map)
            depth += 1
        elif token.nesting == -1:
            depth -= 1
        elif depth == 0 and token.type in _SELF_CLOSING_BLOCKS and token.map is not None:
            block_maps.append(token.map)

    if len(block_maps) < 2:
        return None

    target_line = block_maps[-2][1]
    offset = 0
    for _ in range(target_line):
        offset = text.index("\n", offset) + 1
    return offset


@functools.lru_cache(maxsize=64)
def _markdown_commit_boundary_cached(text: str) -> int | None:
    return _markdown_commit_boundary_uncached(text)


def _cheap_newline_boundary(text: str) -> int | None:
    """Fallback for very large streams: commit at the last completed line."""
    if "\n\n" not in text:
        return None
    return text.rfind("\n\n") + 2


def markdown_commit_boundary(text: str) -> int | None:
    """Return the offset up to which streamed markdown can be committed."""
    if not text:
        return None
    if len(text) > MAX_STREAM_PARSE_BYTES:
        return _cheap_newline_boundary(text)
    return _markdown_commit_boundary_cached(text)


def _find_stream_safe_boundary(text: str) -> int | None:
    boundary = markdown_commit_boundary(text)
    if boundary is not None:
        return boundary

    if "\n" in text:
        return None
    stripped = text.rstrip()
    if stripped.endswith(_SENTENCE_END):
        return len(text)
    return None


@dataclass(slots=True)
class PythinkerMarkdownStream:
    """Buffer streamed markdown deltas and yield safe-to-render slices."""

    pending: str = field(default="")

    def push(self, delta: str) -> str | None:
        self.pending += delta
        cut = _find_stream_safe_boundary(self.pending)
        if cut is None or cut == 0:
            return None
        ready = self.pending[:cut]
        self.pending = self.pending[cut:]
        return ready

    def flush(self) -> str | None:
        if not self.pending.strip():
            self.pending = ""
            return None
        pending = self.pending
        self.pending = ""
        return pending
