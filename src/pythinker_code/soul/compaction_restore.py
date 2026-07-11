from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from pythinker_core.message import Message
from pythinker_host.path import HostPath

from pythinker_code.skill import (
    Skill,
    normalize_skill_name,
    read_skill_text_with_local_specialization,
)
from pythinker_code.soul.message import system_reminder

MAX_RESTORED_FILES = 12
MAX_RESTORED_SKILL_CHARS = 12_000
MAX_RESTORED_SKILL_CHARS_PER_SKILL = 4_000

_PATH_KEYS = frozenset(
    {
        "file",
        "file_path",
        "filepath",
        "fromPath",
        "old_path",
        "output_path",
        "path",
        "paths",
        "toPath",
        "new_path",
    }
)
_READ_TOOL_NAMES = frozenset({"ReadFile", "ReadMediaFile"})
_FILE_MENTION_RE = re.compile(r"(?<![\w@])@(?P<path>[^\s`'\"<>()]+)")


@dataclass(frozen=True, slots=True)
class CompactionRestoreContext:
    """Bounded context that is restored immediately after compaction."""

    read_files: tuple[str, ...] = ()
    referenced_files: tuple[str, ...] = ()
    restored_skills: tuple[str, ...] = ()
    messages: tuple[Message, ...] = ()

    def display_text(self) -> str:
        """Return a concise transcript notice for the user."""
        lines = ["Conversation compacted."]
        for path in self.referenced_files:
            if path not in self.read_files:
                lines.append(f"  ⎿  Referenced file {path}")
        for path in self.read_files:
            lines.append(f"  ⎿  Read {path}")
        if self.restored_skills:
            lines.append(f"  ⎿  Skills restored ({', '.join(self.restored_skills)})")
        return "\n".join(lines)


async def build_compaction_restore_context(
    history: Sequence[Message],
    *,
    work_dir: HostPath,
    additional_dirs: Sequence[HostPath] = (),
    active_skill_names: Sequence[str] = (),
    skills_by_name: Mapping[str, Skill] | None = None,
    max_files: int = MAX_RESTORED_FILES,
) -> CompactionRestoreContext:
    """Build post-compaction reminders for facts that summaries often drop.

    The restored context is intentionally bounded. It does not re-read arbitrary
    files; it only preserves the names of files that were already referenced or
    read, and the bodies of skills the user explicitly invoked in this session.
    """

    files = collect_recent_file_context(
        history, work_dir=work_dir, additional_dirs=additional_dirs, max_files=max_files
    )
    skill_context, restored_skills = await _restore_active_skills(
        active_skill_names,
        skills_by_name or {},
    )

    sections: list[str] = []
    if files.referenced_files or files.read_files:
        lines = [
            "Post-compaction file context restored. These files were referenced before "
            "compaction; re-read them before editing if exact contents matter."
        ]
        if files.referenced_files:
            lines.append("Referenced files:")
            lines.extend(f"- {path}" for path in files.referenced_files)
        if files.read_files:
            lines.append("Recently read files:")
            lines.extend(f"- {path}" for path in files.read_files)
        sections.append("\n".join(lines))

    if skill_context:
        sections.append(skill_context)

    messages = (
        (Message(role="user", content=[system_reminder("\n\n".join(sections))]),)
        if sections
        else ()
    )
    return CompactionRestoreContext(
        read_files=files.read_files,
        referenced_files=files.referenced_files,
        restored_skills=restored_skills,
        messages=messages,
    )


@dataclass(frozen=True, slots=True)
class _FileContext:
    read_files: tuple[str, ...] = ()
    referenced_files: tuple[str, ...] = ()


def collect_recent_file_context(
    history: Sequence[Message],
    *,
    work_dir: HostPath,
    additional_dirs: Sequence[HostPath] = (),
    max_files: int = MAX_RESTORED_FILES,
) -> _FileContext:
    """Collect recently referenced/read paths from message text and tool calls."""
    referenced: list[str] = []
    read: list[str] = []

    for message in history:
        for path in _extract_file_mentions(message.extract_text(" ")):
            _append_unique(referenced, path, work_dir=work_dir, additional_dirs=additional_dirs)

        for tool_call in message.tool_calls or []:
            args = _parse_tool_arguments(tool_call.function.arguments)
            paths = list(_extract_path_values(args))
            for path in paths:
                _append_unique(referenced, path, work_dir=work_dir, additional_dirs=additional_dirs)
            if tool_call.function.name in _READ_TOOL_NAMES:
                for path in paths:
                    _append_unique(read, path, work_dir=work_dir, additional_dirs=additional_dirs)

    return _FileContext(
        read_files=tuple(_most_recent(read, max_files)),
        referenced_files=tuple(_most_recent(referenced, max_files)),
    )


def compact_summary_text(messages: Sequence[Message]) -> str:
    """Extract the human-readable compaction summary from compacted messages."""
    if not messages:
        return ""
    return messages[0].extract_text("\n").strip()


def build_hook_context_message(contexts: Iterable[str]) -> Message | None:
    """Build a bounded post-compaction hook context reminder, if supplied."""
    cleaned: list[str] = []
    total = 0
    for context in contexts:
        text = context.strip()
        if not text:
            continue
        remaining = MAX_RESTORED_SKILL_CHARS - total
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining].rstrip() + "\n...[truncated]"
        cleaned.append(text)
        total += len(text)

    if not cleaned:
        return None
    body = (
        "Additional context supplied by post-compaction hooks. Treat this as factual "
        "session context restored after compaction:\n\n" + "\n\n---\n\n".join(cleaned)
    )
    return Message(role="user", content=[system_reminder(body)])


def _parse_tool_arguments(raw: str | None) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw, strict=False)
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_path_values(value: Any, *, parent_key: str = "", depth: int = 0) -> Iterable[str]:
    if depth > 4:
        return
    if isinstance(value, dict):
        for key, child in cast(dict[str, Any], value).items():  # pyright: ignore[reportUnknownVariableType]
            key_str = str(key)  # pyright: ignore[reportUnknownArgumentType]
            if key_str in _PATH_KEYS:
                yield from _string_values(child)
            elif isinstance(child, dict | list | tuple):
                yield from _extract_path_values(child, parent_key=key_str, depth=depth + 1)
        return
    if parent_key in _PATH_KEYS:
        yield from _string_values(value)


def _string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list | tuple):
        for item in cast(list[Any], value):  # pyright: ignore[reportUnknownVariableType]
            if isinstance(item, str):
                yield item


def _extract_file_mentions(text: str) -> Iterable[str]:
    for match in _FILE_MENTION_RE.finditer(text):
        yield match.group("path")


def _append_unique(
    paths: list[str],
    raw_path: str,
    *,
    work_dir: HostPath,
    additional_dirs: Sequence[HostPath] = (),
) -> None:
    display = _display_path(raw_path, work_dir=work_dir, additional_dirs=additional_dirs)
    if display and display not in paths:
        paths.append(display)


def _is_contained(raw: str, root: str) -> bool:
    try:
        rel = os.path.relpath(raw, root)
    except ValueError:
        # Cannot relativize (e.g. different drive on Windows).
        return False
    return not rel.startswith(".." + os.sep) and rel != ".."


def _display_path(
    raw_path: str,
    *,
    work_dir: HostPath,
    additional_dirs: Sequence[HostPath] = (),
) -> str | None:
    raw = raw_path.strip().strip("`'\"")
    if not raw or "\n" in raw or "\x00" in raw:
        return None
    # Avoid turning prose or URLs into fake path context.
    if "://" in raw or raw.startswith(("mailto:", "data:")):
        return None

    work = str(work_dir)
    if os.path.isabs(raw):
        if _is_contained(raw, work):
            rel = os.path.relpath(raw, work)
            return None if rel == "." else rel
        # Additional workspace roots (--add-dir) are legitimate session members;
        # keep them absolute so they stay unambiguous next to work-relative paths.
        if any(_is_contained(raw, str(d)) for d in additional_dirs):
            return raw
        # Absolute path escaping the workspace -> skip; do not surface
        # out-of-workspace absolutes (e.g. /etc/passwd) in restore reminders.
        return None
    return raw.removeprefix("./")


def _most_recent(paths: list[str], limit: int) -> list[str]:
    if limit <= 0:
        return []
    return paths[-limit:]


async def _restore_active_skills(
    active_skill_names: Sequence[str],
    skills_by_name: Mapping[str, Skill],
) -> tuple[str, tuple[str, ...]]:
    if not active_skill_names or not skills_by_name:
        return "", ()

    sections: list[str] = []
    restored: list[str] = []
    total_chars = 0
    for raw_name in active_skill_names:
        skill = skills_by_name.get(normalize_skill_name(raw_name))
        if skill is None:
            continue
        skill_text = await read_skill_text_with_local_specialization(skill, skills_by_name)
        if not skill_text:
            continue
        remaining = MAX_RESTORED_SKILL_CHARS - total_chars
        if remaining <= 0:
            break
        limit = min(MAX_RESTORED_SKILL_CHARS_PER_SKILL, remaining)
        if len(skill_text) > limit:
            skill_text = skill_text[:limit].rstrip() + "\n...[truncated]"
        sections.append(f"## Skill restored after compaction: {skill.name}\n\n{skill_text}")
        restored.append(skill.name)
        total_chars += len(skill_text)

    if not sections:
        return "", ()
    header = (
        "Active skill instructions restored after compaction. Continue following "
        "these procedures when they are relevant to the current task."
    )
    return header + "\n\n" + "\n\n---\n\n".join(sections), tuple(restored)
