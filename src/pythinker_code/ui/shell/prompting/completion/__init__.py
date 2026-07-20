"""Internal prompt completion primitives."""

from pythinker_code.ui.shell.prompting.completion.context import (
    CompletionContext,
    CompletionKind,
    parse_completion_context,
)
from pythinker_code.ui.shell.prompting.completion.slash import (
    InputHighlightLexer,
    SlashCommandAutoSuggest,
    SlashCommandCompleter,
)

__all__ = (
    "CompletionContext",
    "CompletionKind",
    "InputHighlightLexer",
    "SlashCommandAutoSuggest",
    "SlashCommandCompleter",
    "parse_completion_context",
)
