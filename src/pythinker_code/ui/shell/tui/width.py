from __future__ import annotations

import textwrap


def pad_line(value: str, width: int) -> str:
    return value[:width].ljust(width)


def wrap_plain_text(value: str, width: int) -> list[str]:
    if not value:
        return [""]
    wrapped: list[str] = []
    for raw_line in value.splitlines() or [value]:
        wrapped.extend(textwrap.wrap(raw_line, width=width, replace_whitespace=False) or [""])
    return wrapped
