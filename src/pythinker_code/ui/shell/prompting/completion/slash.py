"""Slash completion, suggestion, and input highlighting."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any, override

from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.lexers import Lexer

from pythinker_code.ui.shell.prompting.completion.context import (
    CompletionKind,
    iter_completion_contexts,
    parse_completion_context,
)
from pythinker_code.utils.slashcmd import SlashCommand


def command_name_set(commands: Sequence[SlashCommand[Any]]) -> frozenset[str]:
    """Return lower-cased command names and aliases."""
    names: set[str] = set()
    for command in commands:
        names.add(command.name.lower())
        names.update(alias.lower() for alias in command.aliases)
    return frozenset(names)


def _is_known_slash_command_prefix(name: str, known: frozenset[str]) -> bool:
    lower = name.lower()
    return lower in known or any(command_name.startswith(lower) for command_name in known)


def _fuzzy_subsequence(needle: str, haystack: str) -> bool:
    position = 0
    for character in needle:
        found = haystack.find(character, position)
        if found < 0:
            return False
        position = found + 1
    return True


def _no_exact_suggestions() -> dict[str, str]:
    return {}


def _no_arg_suggestions() -> dict[str, tuple[str, ...]]:
    return {}


def discard_slash_command(buffer: Buffer) -> bool:
    """Cancel slash completion and remove the in-progress root slash command."""
    document = buffer.document
    context = parse_completion_context(document, allow_file=False)
    if context.kind is not CompletionKind.SLASH_COMMAND:
        return False

    buffer.cancel_completion()
    prefix = document.text_before_cursor[: context.start_position]
    new_text = prefix + document.text_after_cursor
    if not new_text.strip():
        buffer.set_document(Document(), bypass_readonly=True)
        return True

    buffer.set_document(
        Document(new_text, cursor_position=min(len(prefix), len(new_text))),
        bypass_readonly=True,
    )
    return True


class InputHighlightLexer(Lexer):
    """Highlight slash commands, slash arguments, file mentions, and ``!``."""

    def __init__(
        self,
        known_names: Callable[[], frozenset[str]],
        *,
        agent_mode: Callable[[], bool],
        arg_suggestions: Callable[[], dict[str, tuple[str, ...]]] | None = None,
    ) -> None:
        self._known_names = known_names
        self._agent_mode = agent_mode
        self._arg_suggestions = arg_suggestions or _no_arg_suggestions

    @override
    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        known = self._known_names()
        arguments = self._arg_suggestions()
        agent_mode = self._agent_mode()
        lines = document.lines
        parsed_by_line: dict[int, list[tuple[int, int, str]]] = {}

        for line_number, end, context in iter_completion_contexts(
            document,
            known_commands=known,
            argument_suggestions=arguments,
            include_files=agent_mode,
        ):
            start = end + context.start_position
            if context.kind is CompletionKind.SLASH_COMMAND:
                name = context.token[1:]
                if not _is_known_slash_command_prefix(name, known):
                    continue
                style = "class:slash-command"
            elif context.kind is CompletionKind.SLASH_ARGUMENT:
                options = arguments.get(context.command or "")
                if not options or not any(
                    option.startswith(context.token.lower()) for option in options
                ):
                    continue
                style = "class:slash-arg"
            elif context.kind is CompletionKind.FILE:
                start -= 1
                style = "class:file-mention"
            else:
                continue
            parsed_by_line.setdefault(line_number, []).append((start, end, style))

        def get_line(line_number: int) -> StyleAndTextTuples:
            try:
                line = lines[line_number]
            except IndexError:
                return []
            spans = parsed_by_line.get(line_number, [])
            if agent_mode and line_number == 0 and line.startswith("!") and line[1:].strip():
                spans.append((0, 1, "class:bash-prefix"))
            spans.sort(key=lambda span: span[0])
            fragments: StyleAndTextTuples = []
            position = 0
            for start, end, style in spans:
                if start < position:
                    continue
                if start > position:
                    fragments.append(("", line[position:start]))
                fragments.append((style, line[start:end]))
                position = end
            if position < len(line):
                fragments.append(("", line[position:]))
            return fragments

        return get_line


class SlashCommandAutoSuggest(AutoSuggest):
    """Inline ghost-text completion for slash commands and first arguments."""

    def __init__(
        self,
        known_names: Callable[[], frozenset[str]],
        *,
        exact_suggestions: Callable[[], dict[str, str]] | None = None,
        arg_suggestions: Callable[[], dict[str, tuple[str, ...]]] | None = None,
    ) -> None:
        self._known_names = known_names
        self._exact_suggestions = exact_suggestions or _no_exact_suggestions
        self._arg_suggestions = arg_suggestions or _no_arg_suggestions

    @override
    def get_suggestion(self, buffer: Buffer, document: Document) -> Suggestion | None:
        known = self._known_names()
        arguments = self._arg_suggestions()
        context = parse_completion_context(
            document,
            known_commands=known,
            argument_commands=arguments.keys(),
            slash_activation="any",
            allow_file=False,
        )
        if context.kind is CompletionKind.SLASH_ARGUMENT:
            if context.argument_index != 0 or context.command not in arguments:
                return None
            partial = context.token
            options = arguments[context.command]
            partial_lower = partial.lower()
            if not partial:
                return Suggestion(options[0])
            matches = [
                option
                for option in options
                if option.startswith(partial_lower) and len(option) > len(partial)
            ]
            return Suggestion(matches[0][len(partial) :]) if matches else None

        if context.kind is not CompletionKind.SLASH_COMMAND or len(context.token) < 2:
            return None
        typed = context.token[1:]
        typed_lower = typed.lower()
        exact = self._exact_suggestions().get(typed_lower)
        if exact is not None and typed_lower in known:
            return Suggestion(exact)
        matches = sorted(
            name
            for name in known
            if name.lower().startswith(typed_lower) and len(name) > len(typed)
        )
        return Suggestion(matches[0][len(typed) :]) if matches else None


class SlashCommandCompleter(Completer):
    """Complete and rank root slash commands and fixed first arguments."""

    def __init__(
        self,
        available_commands: Sequence[SlashCommand[Any]],
        *,
        annotate_meta: bool = False,
        command_scope: str = "command",
        is_task_running: Callable[[], bool] | None = None,
        arg_suggestions: Callable[[], dict[str, tuple[str, ...]]] | None = None,
    ) -> None:
        super().__init__()
        self._available_commands = sorted(available_commands, key=lambda command: command.name)
        self._command_names = command_name_set(available_commands)
        self._annotate_meta = annotate_meta
        self._command_scope = command_scope
        self._is_task_running = is_task_running
        self._arg_suggestions = arg_suggestions or _no_arg_suggestions

    def _context(self, document: Document):
        arguments = self._arg_suggestions()
        return parse_completion_context(
            document,
            known_commands=self._command_names,
            argument_commands=arguments.keys(),
            allow_file=False,
        )

    def completion_active(self, document: Document) -> bool:
        context = self._context(document)
        return context.kind is CompletionKind.SLASH_COMMAND or (
            context.kind is CompletionKind.SLASH_ARGUMENT
            and context.argument_index == 0
            and context.command in self._arg_suggestions()
        )

    @staticmethod
    def should_complete(document: Document) -> bool:
        context = parse_completion_context(document, allow_file=False)
        return context.kind is CompletionKind.SLASH_COMMAND

    @override
    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        context = self._context(document)
        if context.kind is CompletionKind.SLASH_ARGUMENT:
            if context.argument_index != 0 or context.command not in self._arg_suggestions():
                return
            partial = context.token
            for option in self._arg_suggestions()[context.command]:
                if partial and not option.startswith(partial.lower()):
                    continue
                yield Completion(
                    text=option[len(partial) :] if partial else option,
                    start_position=context.start_position,
                    display=option,
                )
            return
        if context.kind is not CompletionKind.SLASH_COMMAND:
            return

        typed = context.token[1:]
        typed_lower = typed.lower()
        seen: set[str] = set()

        def emit(command: SlashCommand[Any], label: str | None = None) -> Iterable[Completion]:
            if command.name in seen:
                return
            seen.add(command.name)
            shown = label or command.name
            yield Completion(
                text=f"/{shown}",
                start_position=context.start_position,
                display=f"/{shown}",
                display_meta=self._display_meta(command),
            )

        if not typed:
            for command in self._available_commands:
                yield from emit(command)
            return

        def match_tier(command: SlashCommand[Any]) -> tuple[int, str] | None:
            name_lower = command.name.lower()
            if name_lower == typed_lower:
                return (0, command.name)
            if name_lower.startswith(typed_lower):
                return (1, command.name)
            alias_prefix: str | None = None
            for alias in command.aliases:
                alias_lower = alias.lower()
                if alias_lower == typed_lower:
                    return (2, alias)
                if alias_prefix is None and alias_lower.startswith(typed_lower):
                    alias_prefix = alias
            if alias_prefix is not None:
                return (3, alias_prefix)
            segment = name_lower.split(":", 1)[1] if ":" in name_lower else name_lower
            if ":" in name_lower:
                if segment == typed_lower:
                    return (4, command.name)
                if segment.startswith(typed_lower):
                    return (5, command.name)
            if len(typed_lower) >= 2 and _fuzzy_subsequence(typed_lower, segment):
                return (6, command.name)
            return None

        matched: list[tuple[int, int, str, str, SlashCommand[Any]]] = []
        for command in self._available_commands:
            result = match_tier(command)
            if result is not None:
                tier, label = result
                matched.append((tier, len(command.name), command.name, label, command))
        matched.sort(key=lambda item: (item[0], item[1], item[2]))
        if matched and matched[0][0] < 6:
            matched = [item for item in matched if item[0] < 6]
        for _, _, _, label, command in matched:
            yield from emit(command, label)

    def _disabled_during_task(self, command: SlashCommand[Any]) -> bool:
        if self._is_task_running is None or not self._is_task_running():
            return False
        from pythinker_code.ui.shell.slash import registry as shell_registry

        shell_command = shell_registry.find_command(command.name)
        return shell_command is not None and not shell_command.available_during_task

    def _display_meta(self, command: SlashCommand[Any]) -> str:
        if self._disabled_during_task(command):
            return "disabled while a task is in progress"
        if not self._annotate_meta:
            return command.description
        if command.name.startswith("skill:"):
            kind: str | None = "skill"
        elif command.name.startswith("flow:"):
            kind = "flow"
        else:
            kind = None
        parts: list[str] = []
        if kind is not None:
            parts.append(f"[{kind}]")
        parts.append(command.description)
        if command.aliases:
            parts.append(f"aliases: {', '.join('/' + alias for alias in command.aliases)}")
        return "  ".join(part for part in parts if part)
