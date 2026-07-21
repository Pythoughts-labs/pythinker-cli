"""Internal prompt completion primitives."""

from pythinker_code.ui.shell.prompting.completion.context import (
    CompletionContext,
    CompletionKind,
    parse_completion_context,
)
from pythinker_code.ui.shell.prompting.completion.menus import (
    LocalFileMentionMenuControl,
    SlashCommandMenuControl,
    truncate_to_width,
    wrap_to_width,
)
from pythinker_code.ui.shell.prompting.completion.slash import (
    InputHighlightLexer,
    SlashCommandAutoSuggest,
    SlashCommandCompleter,
)
from pythinker_code.ui.shell.prompting.completion.workspace import (
    HostFileMentionCompleter,
    LocalFileMentionCompleter,
    WorkspaceEntry,
    WorkspaceIndex,
    WorkspaceSnapshot,
)

__all__ = (
    "CompletionContext",
    "CompletionKind",
    "HostFileMentionCompleter",
    "InputHighlightLexer",
    "LocalFileMentionCompleter",
    "LocalFileMentionMenuControl",
    "SlashCommandAutoSuggest",
    "SlashCommandMenuControl",
    "SlashCommandCompleter",
    "WorkspaceEntry",
    "WorkspaceIndex",
    "WorkspaceSnapshot",
    "parse_completion_context",
    "truncate_to_width",
    "wrap_to_width",
)
