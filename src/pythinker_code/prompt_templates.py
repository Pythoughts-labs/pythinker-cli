from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pythinker_host.path import HostPath

from pythinker_code.utils.frontmatter import parse_frontmatter, strip_frontmatter
from pythinker_code.utils.logging import logger
from pythinker_code.utils.path import find_project_root

PromptTemplateScope = Literal["project", "user", "plugin"]


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """A markdown prompt template exposed as a slash command."""

    name: str
    description: str
    content: str
    file_path: HostPath
    scope: PromptTemplateScope
    argument_hint: str | None = None


def parse_command_args(args_string: str) -> list[str]:
    """Parse template arguments using simple shell quoting."""
    args: list[str] = []
    current = ""
    quote: str | None = None

    for char in args_string:
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current += char
        elif char in {'"', "'"}:
            quote = char
        elif char in {" ", "\t"}:
            if current:
                args.append(current)
                current = ""
        else:
            current += char

    if current:
        args.append(current)
    return args


def substitute_args(content: str, args: list[str]) -> str:
    """Substitute prompt-template placeholders.

    Supports ``$1``, ``$2``, ``$@``, ``$ARGUMENTS``, ``${@:N}``, and
    ``${@:N:L}``. Replacement is one-pass; inserted argument values are not
    recursively substituted.
    """
    import re

    def positional(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        return args[index] if 0 <= index < len(args) else ""

    result = re.sub(r"\$(\d+)", positional, content)

    def sliced(match: re.Match[str]) -> str:
        start = max(0, int(match.group(1)) - 1)
        length_group = match.group(2)
        if length_group is None:
            return " ".join(args[start:])
        length = int(length_group)
        return " ".join(args[start : start + length])

    result = re.sub(r"\$\{@:(\d+)(?::(\d+))?\}", sliced, result)
    all_args = " ".join(args)
    result = result.replace("$ARGUMENTS", all_args)
    result = result.replace("$@", all_args)
    return result


def expand_prompt_template(template: PromptTemplate, args_string: str) -> str:
    """Expand *template* with a raw slash-command argument string."""
    return substitute_args(template.content, parse_command_args(args_string))


async def discover_prompt_templates(work_dir: HostPath) -> dict[str, PromptTemplate]:
    """Discover project/user prompt templates.

    Project templates win over user templates.
    """
    from pythinker_code.plugin.integration import plugin_command_dirs

    roots: list[tuple[PromptTemplateScope, HostPath]] = []
    project_root = await find_project_root(work_dir)
    roots.extend(
        [
            ("project", project_root / ".pythinker" / "prompts"),
            ("user", HostPath.home() / ".pythinker" / "prompts"),
        ]
    )
    # Enabled plugins contribute commands at lowest priority (project/user win).
    roots.extend(("plugin", HostPath.unsafe_from_local_path(d)) for d in plugin_command_dirs())

    templates: dict[str, PromptTemplate] = {}
    for scope, root in roots:
        for template in await _load_templates_from_dir(root, scope=scope):
            templates.setdefault(template.name, template)
    return dict(sorted(templates.items()))


async def _load_templates_from_dir(
    directory: HostPath,
    *,
    scope: PromptTemplateScope,
) -> list[PromptTemplate]:
    try:
        if not await directory.is_dir():
            return []
    except OSError as exc:
        logger.debug("Cannot stat prompt templates dir {path}: {error}", path=directory, error=exc)
        return []

    templates: list[PromptTemplate] = []
    try:
        async for entry in directory.iterdir():
            try:
                if await entry.is_dir() or not entry.name.lower().endswith(".md"):
                    continue
                content = await entry.read_text(encoding="utf-8")
            except OSError as exc:
                logger.info(
                    "Skipping unreadable prompt template {path}: {error}",
                    path=entry,
                    error=exc,
                )
                continue
            try:
                templates.append(parse_prompt_template_text(content, file_path=entry, scope=scope))
            except Exception as exc:
                logger.info(
                    "Skipping invalid prompt template {path}: {error}",
                    path=entry,
                    error=exc,
                )
    except OSError as exc:
        logger.warning(
            "Failed to iterate prompt templates dir {path}: {error}",
            path=directory,
            error=exc,
        )
    return sorted(templates, key=lambda t: t.name)


def parse_prompt_template_text(
    text: str,
    *,
    file_path: HostPath,
    scope: PromptTemplateScope,
) -> PromptTemplate:
    """Parse a markdown prompt template file."""
    frontmatter = parse_frontmatter(text) or {}
    body = strip_frontmatter(text).strip()
    name = file_path.name.rsplit(".", 1)[0]
    description = str(frontmatter.get("description") or "").strip()
    if not description:
        description = _first_body_line(body)
    argument_hint = frontmatter.get("argument-hint")
    return PromptTemplate(
        name=name,
        description=description,
        argument_hint=str(argument_hint).strip() if argument_hint else None,
        content=body,
        file_path=file_path,
        scope=scope,
    )


def _first_body_line(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:60] + ("..." if len(stripped) > 60 else "")
    return "Prompt template"
