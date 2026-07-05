from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

SYNC_OUTPUT_START = "\x1b[?2026h"
SYNC_OUTPUT_END = "\x1b[?2026l"


@dataclass(frozen=True)
class LinePatch:
    start: int
    delete: int
    insert: tuple[str, ...]


def plan_line_diff(old: Sequence[str], new: Sequence[str]) -> list[LinePatch]:
    patches: list[LinePatch] = []
    matcher = SequenceMatcher(a=list(old), b=list(new), autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        patches.append(
            LinePatch(
                start=old_start,
                delete=old_end - old_start,
                insert=tuple(new[new_start:new_end]),
            )
        )
    return patches


def synchronized_output(payload: str) -> str:
    if not payload:
        return payload
    return f"{SYNC_OUTPUT_START}{payload}{SYNC_OUTPUT_END}"
