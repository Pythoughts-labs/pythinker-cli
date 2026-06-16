"""Extract symbol context around a file position."""

from __future__ import annotations

import re
from pathlib import Path

MAX_READ_BYTES = 64 * 1024
MAX_SYMBOL_LEN = 30
_SYMBOL_PATTERN = re.compile(r"[\w$'!]+|[+\-*/%&|^~<>=]+")


def get_symbol_at_position(file_path: str | Path, line: int, character: int) -> str | None:
    """Return the symbol at a 1-based line/character, or None on failure."""
    context = get_symbol_context(file_path, line, character, context_lines=0)
    if context is None:
        return None
    first_line = context.splitlines()[0]
    prefix = "Symbol: "
    if first_line.startswith(prefix):
        symbol = first_line[len(prefix) :]
        return symbol[:MAX_SYMBOL_LEN] if symbol else None
    return None


def get_symbol_context(
    file_path: str | Path,
    line: int,
    character: int,
    *,
    context_lines: int = 2,
) -> str | None:
    """Return the symbol and surrounding lines for a 1-based position."""
    if line < 1 or character < 1:
        return None

    path = Path(file_path)
    try:
        file_size = path.stat().st_size
        with path.open("rb") as handle:
            chunk = handle.read(MAX_READ_BYTES)
    except OSError:
        return None

    content = chunk.decode(encoding="utf-8", errors="replace")

    lines = content.splitlines()
    zero_line = line - 1
    zero_char = character - 1

    if zero_line < 0 or zero_line >= len(lines):
        return None
    if file_size > MAX_READ_BYTES and zero_line == len(lines) - 1:
        return None

    line_content = lines[zero_line]
    if zero_char < 0 or zero_char > len(line_content):
        return None

    symbol: str | None = None
    for match in _SYMBOL_PATTERN.finditer(line_content):
        start = match.start()
        end = start + len(match.group(0))
        if zero_char >= start and zero_char < end:
            symbol = match.group(0)[:MAX_SYMBOL_LEN]
            break

    parts: list[str] = []
    if symbol:
        parts.append(f"Symbol: {symbol}")

    if context_lines > 0:
        start = max(0, zero_line - context_lines)
        end = min(len(lines), zero_line + context_lines + 1)
        snippet = "\n".join(f"{start + idx + 1:>4}| {lines[idx]}" for idx in range(start, end))
        parts.append(snippet)

    return "\n".join(parts) if parts else None
