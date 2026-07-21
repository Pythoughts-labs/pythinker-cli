"""Bounded, locked persistence for prompt input history."""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from pythinker_code.utils.io import file_lock
from pythinker_code.utils.logging import logger

_DEFAULT_MAX_ENTRIES = 1000
_MAX_SERIALIZED_RECORD_BYTES = 256 * 1024
_ROTATE_AT_BYTES = 10 * 1024 * 1024
_LOCK_TIMEOUT_SECONDS = 1.0
_DEFAULT_SENSITIVE_COMMANDS = frozenset({"login", "logout", "setup"})

_HISTORY_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)\b((?:authorization\s*:\s*)?(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{8,}"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|token|secret|password|access[_-]?token|"
            r"refresh[_-]?token|id[_-]?token|session[_-]?token)[\"']?\s*[:=]\s*[\"'])"
            r"([^\"'\r\n]{8,})([\"'])"
        ),
        r"\1[REDACTED]\3",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|access[_-]?token|"
            r"refresh[_-]?token|id[_-]?token|session[_-]?token)(\s*[:=]\s*)([^\s'\"&]{8,})"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"\b(sk-[A-Za-z0-9][A-Za-z0-9_-]{16,})\b"), "[REDACTED]"),
    (re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED]"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "[REDACTED]"),
    (re.compile(r"\b(AIza[0-9A-Za-z_-]{20,})\b"), "[REDACTED]"),
)


class HistoryEntry(BaseModel):
    content: str


@dataclass(frozen=True, slots=True)
class PromptHistoryStatus:
    """Current prompt-history paths, counts, and storage sizes."""

    enabled: bool
    entries: int
    path: Path
    rotated_path: Path
    size_bytes: int
    rotated_size_bytes: int


class PromptHistoryError(RuntimeError):
    """A locked prompt-history mutation could not be completed."""


class PromptHistoryStore:
    """Own one workspace's JSONL prompt history."""

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        sensitive_commands: Collection[str] = _DEFAULT_SENSITIVE_COMMANDS,
    ) -> None:
        self.path = path
        self.rotated_path = path.with_name(path.name + ".1")
        self.enabled = enabled
        self.max_entries = max(1, max_entries)
        self.sensitive_commands = frozenset(command.lower() for command in sensitive_commands)
        self._last_content: str | None = None
        if enabled:
            ensure_private_history_path(path)

    def load(self) -> list[HistoryEntry]:
        """Load at most ``max_entries`` recent records using tail reads."""
        if not self.enabled:
            return []
        current_lines = self._tail_lines(self.path, self.max_entries)
        remaining = self.max_entries - len(current_lines)
        rotated_lines = self._tail_lines(self.rotated_path, remaining) if remaining > 0 else []
        entries: list[HistoryEntry] = []
        for source, lines in (
            (self.rotated_path, rotated_lines),
            (self.path, current_lines),
        ):
            entries.extend(self._parse_lines(source, lines))
        entries = entries[-self.max_entries :]
        self._last_content = entries[-1].content if entries else None
        return entries

    def append(self, text: str) -> bool:
        """Append a redacted record; return true only when it was persisted."""
        if not self.enabled:
            return False
        content = redact_history_secrets(text.strip())
        if not content or self._is_sensitive_command(content) or content == self._last_content:
            return False
        entry = HistoryEntry(content=content)
        serialized = entry.model_dump_json(ensure_ascii=False) + "\n"
        encoding = "utf-8"
        serialized_bytes = serialized.encode(encoding=encoding, errors="replace")
        if len(serialized_bytes) > _MAX_SERIALIZED_RECORD_BYTES:
            logger.warning(
                "Prompt history record exceeds the size limit; skipping: bytes={}",
                len(serialized_bytes),
            )
            return False

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(self.path, timeout=_LOCK_TIMEOUT_SECONDS):
                self._rotate_if_needed(len(serialized_bytes))
                ensure_private_history_path(self.path)
                fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "a", encoding=encoding) as stream:
                    stream.write(serialized)
                with contextlib.suppress(OSError):
                    os.chmod(self.path, 0o600)
                self._last_content = content
            return True
        except OSError as exc:
            message = f"Could not append prompt history at {self.path}: {exc}"
            raise PromptHistoryError(message) from exc

    def clear(self) -> PromptHistoryStatus:
        """Remove current and rotated records under the store lock."""
        try:
            with file_lock(self.path, timeout=_LOCK_TIMEOUT_SECONDS):
                for candidate in (self.path, self.rotated_path):
                    with contextlib.suppress(FileNotFoundError):
                        candidate.unlink()
                if self.path.exists() or self.rotated_path.exists():
                    raise OSError("history files still exist after removal")
                self._last_content = None
        except OSError as exc:
            message = f"Could not clear prompt history at {self.path}: {exc}"
            raise PromptHistoryError(message) from exc
        return self.status()

    def status(self) -> PromptHistoryStatus:
        """Report the current paths, retained count, and file sizes."""
        entries = len(self.load()) if self.enabled else 0
        return PromptHistoryStatus(
            enabled=self.enabled,
            entries=entries,
            path=self.path,
            rotated_path=self.rotated_path,
            size_bytes=self._file_size(self.path),
            rotated_size_bytes=self._file_size(self.rotated_path),
        )

    async def aclose(self) -> None:
        """Close hook for uniform prompt lifecycle ownership."""

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        if self._file_size(self.path) + incoming_bytes <= _ROTATE_AT_BYTES:
            return
        if self.path.exists():
            os.replace(self.path, self.rotated_path)
            with contextlib.suppress(OSError):
                os.chmod(self.rotated_path, 0o600)

    def _is_sensitive_command(self, content: str) -> bool:
        command = content.lstrip().removeprefix("/").split(maxsplit=1)[0].lower()
        return command in self.sensitive_commands

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0
        except OSError as exc:
            logger.warning("Failed to inspect prompt history file: file={} error={!r}", path, exc)
            return 0

    @staticmethod
    def _tail_lines(path: Path, limit: int) -> list[tuple[int, bytes]]:
        if limit <= 0:
            return []
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                position = stream.tell()
                chunks: list[bytes] = []
                newline_count = 0
                while position > 0 and newline_count <= limit:
                    read_size = min(64 * 1024, position)
                    position -= read_size
                    stream.seek(position)
                    chunk = stream.read(read_size)
                    chunks.append(chunk)
                    newline_count += chunk.count(b"\n")
                data = b"".join(reversed(chunks))
                if position > 0:
                    first_newline = data.find(b"\n")
                    data = data[first_newline + 1 :] if first_newline >= 0 else b""
                lines = data.splitlines()[-limit:]
                total = len(lines)
                return [(-(total - index), line) for index, line in enumerate(lines)]
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("Failed to load prompt history file: file={} error={!r}", path, exc)
            return []

    @staticmethod
    def _parse_lines(path: Path, lines: list[tuple[int, bytes]]) -> list[HistoryEntry]:
        entries: list[HistoryEntry] = []
        encoding = "utf-8"
        for line_number, raw_line in lines:
            if not raw_line.strip() or len(raw_line) > _MAX_SERIALIZED_RECORD_BYTES:
                continue
            try:
                decoded = raw_line.decode(encoding=encoding, errors="replace")
                loaded = json.loads(decoded)
                if not isinstance(loaded, dict):
                    raise ValueError("record is not an object")
                record = cast(dict[str, Any], loaded)
                entries.append(HistoryEntry.model_validate(record))
            except (ValidationError, ValueError):
                logger.warning(
                    "Failed to parse prompt history record; skipping: file={} line_from_end={}",
                    path,
                    line_number,
                )
        return entries


def redact_history_secrets(text: str) -> str:
    redacted = text
    for pattern, replacement in _HISTORY_SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def ensure_private_history_path(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    if path.exists():
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def load_history_entries(history_file: Path) -> list[HistoryEntry]:
    """Compatibility facade for loading one prompt-history path."""
    return PromptHistoryStore(history_file).load()


__all__ = (
    "HistoryEntry",
    "PromptHistoryError",
    "PromptHistoryStatus",
    "PromptHistoryStore",
    "ensure_private_history_path",
    "load_history_entries",
    "redact_history_secrets",
)
