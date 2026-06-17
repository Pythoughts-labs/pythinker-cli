"""Render unified diffs into __new hunk__/__old hunk__ review blocks."""

from __future__ import annotations

import re
from dataclasses import dataclass

_FILE_HDR = re.compile(r"^diff --git a/(.+) b/(.+)$")
_HUNK_HDR = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass(frozen=True, slots=True)
class StructuredHunk:
    header: str
    new_block: str
    old_block: str


@dataclass(frozen=True, slots=True)
class StructuredFile:
    path: str
    rendered: str
    hunks: tuple[StructuredHunk, ...]


def render_structured_diff(patch_text: str) -> list[StructuredFile]:
    return [sf for raw in _split_files(patch_text) if (sf := _render_file(raw)) is not None]


def _split_files(patch_text: str) -> list[list[str]]:
    out: list[list[str]] = []
    current: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            if current:
                out.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        out.append(current)
    return out


def _render_file(file_lines: list[str]) -> StructuredFile | None:
    if not file_lines:
        return None
    match = _FILE_HDR.match(file_lines[0])
    if not match or any(line.startswith("Binary files ") for line in file_lines):
        return None
    path = match.group(2)

    idx = 0
    while idx < len(file_lines) and not file_lines[idx].startswith("@@"):
        if file_lines[idx].startswith("+++ b/"):
            path = file_lines[idx][len("+++ b/") :].strip()
        idx += 1

    hunks: list[StructuredHunk] = []
    while idx < len(file_lines):
        header = file_lines[idx]
        idx += 1
        body: list[str] = []
        while idx < len(file_lines) and not file_lines[idx].startswith("@@"):
            body.append(file_lines[idx])
            idx += 1
        hunk = _render_hunk(header, body)
        if hunk is not None:
            hunks.append(hunk)

    if not hunks:
        return None
    parts = [f"## File: '{path}'", ""]
    for hunk in hunks:
        parts.extend(
            [hunk.header, "__new hunk__", hunk.new_block, "__old hunk__", hunk.old_block, ""]
        )
    return StructuredFile(path=path, rendered="\n".join(parts), hunks=tuple(hunks))


def _render_hunk(header: str, body: list[str]) -> StructuredHunk | None:
    match = _HUNK_HDR.match(header)
    if not match:
        return None
    new_lineno = int(match.group(3))
    old_block_lines: list[str] = []
    new_block_lines: list[str] = []
    for line in body:
        if line.startswith("+") and not line.startswith("+++"):
            new_block_lines.append(f"{new_lineno} {line}")
            new_lineno += 1
        elif line.startswith("-") and not line.startswith("---"):
            old_block_lines.append(line)
        else:
            content = line[1:] if line.startswith(" ") else line
            new_block_lines.append(f"{new_lineno}   {content}")
            old_block_lines.append(f"  {content}")
            new_lineno += 1
    return StructuredHunk(
        header=header,
        new_block="\n".join(new_block_lines),
        old_block="\n".join(old_block_lines),
    )
