"""Canonical parsing for prompt completion tokens."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Set
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from prompt_toolkit.document import Document


class CompletionKind(StrEnum):
    """The completion grammar selected at the cursor."""

    SLASH_COMMAND = "slash_command"
    SLASH_ARGUMENT = "slash_argument"
    FILE = "file"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class CompletionContext:
    """A parsed completion token and its replacement coordinates."""

    kind: CompletionKind
    token: str
    start_position: int
    command: str | None
    argument_index: int | None
    quoted: bool = False


_NONE = CompletionContext(CompletionKind.NONE, "", 0, None, None)
_COMMAND_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:.-")
_MENTION_TRIGGER_GUARDS = frozenset((".", "-", "_", "`", "'", '"', ":", "@", "#", "~"))
_SLASH_TOKEN_RE = re.compile(r"(?<!\S)/([A-Za-z0-9][A-Za-z0-9_:.-]*)")
_ARGUMENT_RE = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_:.-]*)(?:\s+(\S+))")


def _mention_boundary(text: str, index: int) -> bool:
    if index == 0:
        return True
    previous = text[index - 1]
    return not (previous.isalnum() or previous in _MENTION_TRIGGER_GUARDS)


def _unescape_quoted(value: str) -> str:
    out: list[str] = []
    escaped = False
    for character in value:
        if escaped:
            if character in {'"', "\\"}:
                out.append(character)
            else:
                out.extend(("\\", character))
            escaped = False
        elif character == "\\":
            escaped = True
        else:
            out.append(character)
    if escaped:
        out.append("\\")
    return "".join(out)


def _file_context(text: str) -> CompletionContext:
    index = text.rfind("@")
    if index < 0 or not _mention_boundary(text, index):
        return _NONE

    raw = text[index + 1 :]
    if raw.startswith('"'):
        content = raw[1:]
        escaped = False
        closing_index: int | None = None
        for position, character in enumerate(content):
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                closing_index = position
                break
        if closing_index is not None and content[closing_index + 1 :]:
            return _NONE
        raw_token = content if closing_index is None else content[:closing_index]
        return CompletionContext(
            CompletionKind.FILE,
            _unescape_quoted(raw_token),
            -(len(raw_token) + 1 + int(closing_index is not None)),
            None,
            None,
            quoted=True,
        )

    if any(character.isspace() for character in raw) or "@" in raw:
        return _NONE
    return CompletionContext(CompletionKind.FILE, raw, -len(raw), None, None)


def _slash_context(
    document: Document,
    *,
    known_commands: Set[str],
    argument_commands: Set[str],
    slash_activation: Literal["root", "any"],
) -> CompletionContext:
    if document.text_after_cursor.strip():
        return _NONE

    line = document.current_line_before_cursor
    if slash_activation == "any":
        token_start = len(line)
        while token_start > 0 and not line[token_start - 1].isspace():
            token_start -= 1
        candidate = line[token_start:]
        if candidate.startswith("/") and all(
            character in _COMMAND_CHARS for character in candidate[1:]
        ):
            return CompletionContext(
                CompletionKind.SLASH_COMMAND,
                candidate,
                -len(candidate),
                None,
                None,
            )

    if line.startswith("/"):
        command_end = 1
        while command_end < len(line) and line[command_end] in _COMMAND_CHARS:
            command_end += 1
        if command_end > 1 and command_end < len(line) and line[command_end].isspace():
            command = line[1:command_end].lower()
            if command in known_commands:
                remainder = line[command_end:]
                leading = len(remainder) - len(remainder.lstrip())
                arguments = remainder[leading:]
                if not arguments:
                    argument_index = 0
                    token = ""
                else:
                    parts = arguments.split()
                    trailing_space = arguments[-1].isspace()
                    argument_index = len(parts) if trailing_space else len(parts) - 1
                    token = "" if trailing_space else parts[-1]
                return CompletionContext(
                    CompletionKind.SLASH_ARGUMENT,
                    token,
                    -len(token),
                    command,
                    argument_index,
                )

    if slash_activation == "root":
        stripped = line.lstrip(" ")
        if len(stripped) != len(line) and not line[: -len(stripped)].isspace():
            return _NONE
        token_start = len(line) - len(stripped)
    else:
        return _NONE

    candidate = line[token_start:]
    if candidate.startswith("/") and all(
        character in _COMMAND_CHARS for character in candidate[1:]
    ):
        if slash_activation == "root" and line[:token_start].strip():
            return _NONE
        return CompletionContext(
            CompletionKind.SLASH_COMMAND,
            candidate,
            -len(candidate),
            None,
            None,
        )

    return _NONE


def parse_completion_context(
    document: Document,
    *,
    known_commands: Set[str] = frozenset(),
    argument_commands: Set[str] = frozenset(),
    slash_activation: Literal["root", "any"] = "root",
    allow_slash: bool = True,
    allow_file: bool = True,
) -> CompletionContext:
    """Parse the active slash or file token at the cursor exactly once.

    ``slash_activation`` preserves the two user-interface facets: ``root`` gates
    dropdown completion to a leading slash command, while ``any`` recognizes the
    final slash token for inline ghost suggestions and highlighting.
    """
    if allow_slash:
        slash = _slash_context(
            document,
            known_commands=known_commands,
            argument_commands=argument_commands,
            slash_activation=slash_activation,
        )
        if slash.kind is CompletionKind.SLASH_COMMAND:
            return slash
        if (
            slash.kind is CompletionKind.SLASH_ARGUMENT
            and slash.command in argument_commands
            and slash.argument_index == 0
        ):
            return slash
        if slash.kind is CompletionKind.SLASH_ARGUMENT:
            return slash
    if allow_file:
        return _file_context(document.text_before_cursor)
    return _NONE


def iter_completion_contexts(
    document: Document,
    *,
    known_commands: Set[str],
    argument_suggestions: Mapping[str, tuple[str, ...]],
    include_files: bool,
) -> Iterator[tuple[int, int, CompletionContext]]:
    """Yield parser-produced contexts for syntax highlighting."""
    for line_number, line in enumerate(document.lines):
        for match in _SLASH_TOKEN_RE.finditer(line):
            if match.end() < len(line) and line[match.end()] == "/":
                continue
            parsed = parse_completion_context(
                Document(line[: match.end()], cursor_position=match.end()),
                known_commands=known_commands,
                argument_commands=argument_suggestions.keys(),
                slash_activation="any",
                allow_file=False,
            )
            if parsed.kind is CompletionKind.SLASH_COMMAND:
                yield line_number, match.end(), parsed

        argument = _ARGUMENT_RE.match(line)
        if argument is not None:
            parsed = parse_completion_context(
                Document(line[: argument.end(2)], cursor_position=argument.end(2)),
                known_commands=known_commands,
                argument_commands=argument_suggestions.keys(),
                allow_file=False,
            )
            if parsed.kind is CompletionKind.SLASH_ARGUMENT:
                yield line_number, argument.end(2), parsed

        if not include_files:
            continue
        for index, character in enumerate(line):
            if character != "@" or not _mention_boundary(line, index):
                continue
            end = index + 1
            quoted = end < len(line) and line[end] == '"'
            if quoted:
                end += 1
                escaped = False
                while end < len(line):
                    current = line[end]
                    end += 1
                    if escaped:
                        escaped = False
                    elif current == "\\":
                        escaped = True
                    elif current == '"':
                        break
            else:
                while end < len(line) and not line[end].isspace() and line[end] != "@":
                    end += 1
            parsed = parse_completion_context(
                Document(line[:end], cursor_position=end),
                allow_slash=False,
            )
            if parsed.kind is CompletionKind.FILE:
                yield line_number, end, parsed
