"""Shared fenced-code scanning for markdown normalizers."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})")

MARKDOWN_FENCE_INFOS = frozenset({"md", "markdown"})


@dataclass(slots=True)
class FenceState:
    """Track CommonMark-style fenced-code regions while scanning line by line."""

    active: bool = False
    char: str = ""
    length: int = 0

    def feed(self, body: str, *, strict_close: bool = False) -> None:
        """Update state from one line body (without trailing EOL)."""
        match = FENCE_RE.match(body)
        if self.active:
            if match is not None:
                fence = match.group("fence")
                if fence[0] == self.char and len(fence) >= self.length:
                    if strict_close and body[match.end() :].strip():
                        return
                    self.active = False
                    self.char = ""
                    self.length = 0
            return
        if match is not None:
            fence = match.group("fence")
            self.active = True
            self.char = fence[0]
            self.length = len(fence)

    def copy(self) -> FenceState:
        return FenceState(active=self.active, char=self.char, length=self.length)


def iter_fence_aware_lines(
    markup: str,
    *,
    keepends: bool = True,
    strict_close: bool = False,
) -> Iterator[tuple[str, bool]]:
    """Yield ``(line, inside_fence)`` for each line in *markup*.

    *inside_fence* is True for lines that occur while a fence is open,
    including the opening and closing fence markers.
    """
    state = FenceState()
    for line in markup.splitlines(keepends=keepends):
        body = line.rstrip("\r\n")
        was_active = state.active
        state.feed(body, strict_close=strict_close)
        yield line, was_active or state.active
