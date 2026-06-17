"""Tests for inline input highlighting (slash commands, @mentions, ! prefix)."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples

from pythinker_code.ui.shell.prompt import (
    InputHighlightLexer,
    _command_name_set,
)
from pythinker_code.ui.shell.slash import slash_command_arg_suggestions
from pythinker_code.utils.slashcmd import SlashCommand


def _noop(app: object, args: str) -> None:
    pass


def _make_command(
    name: str, *, aliases: Iterable[str] = ()
) -> SlashCommand[Callable[[object, str], None]]:
    return SlashCommand(
        name=name,
        description=f"{name} command",
        func=_noop,
        aliases=list(aliases),
    )


_KNOWN = _command_name_set(
    [
        _make_command("clear"),
        _make_command("statusline", aliases=["sl"]),
        _make_command("skill:best-practices"),
        _make_command("theme", aliases=["color"]),
    ]
)


def _lex_line(text: str, lineno: int = 0, *, agent_mode: bool = True) -> StyleAndTextTuples:
    lexer = InputHighlightLexer(
        lambda: _KNOWN,
        agent_mode=lambda: agent_mode,
        arg_suggestions=slash_command_arg_suggestions,
    )
    return list(lexer.lex_document(Document(text))(lineno))


def _styled(fragments: StyleAndTextTuples, style: str) -> list[str]:
    return [frag[1] for frag in fragments if frag[0] == style]


def _highlighted(fragments: StyleAndTextTuples) -> list[str]:
    return _styled(fragments, "class:slash-command")


def _arg_highlighted(fragments: StyleAndTextTuples) -> list[str]:
    return _styled(fragments, "class:slash-arg")


def test_known_command_highlighted_mid_text():
    fragments = _lex_line("we need commands like /clear here")
    assert _highlighted(fragments) == ["/clear"]
    assert "".join(frag[1] for frag in fragments) == "we need commands like /clear here"


def test_known_command_highlighted_at_start():
    assert _highlighted(_lex_line("/clear")) == ["/clear"]


def test_unknown_command_not_highlighted():
    assert _highlighted(_lex_line("run deep review /best now")) == []


def test_partial_name_is_highlighted():
    assert _highlighted(_lex_line("/cle")) == ["/cle"]
    assert _highlighted(_lex_line("/skill:best")) == ["/skill:best"]


def test_alias_and_namespaced_command_highlighted():
    fragments = _lex_line("/sl then /skill:best-practices")
    assert _highlighted(fragments) == ["/sl", "/skill:best-practices"]


def test_case_insensitive_match():
    assert _highlighted(_lex_line("try /CLEAR now")) == ["/CLEAR"]


def test_bare_word_with_slash_not_highlighted():
    assert _highlighted(_lex_line("see src/clear here")) == []


def test_command_followed_by_subpath_not_highlighted():
    assert _highlighted(_lex_line("open /clear/subdir please")) == []


def test_multiline_highlights_each_line():
    text = "first /clear line\nsecond /statusline line"
    assert _highlighted(_lex_line(text, lineno=0)) == ["/clear"]
    assert _highlighted(_lex_line(text, lineno=1)) == ["/statusline"]


def test_out_of_range_line_returns_empty():
    assert _lex_line("/clear", lineno=5) == []


def test_trailing_punctuation_keeps_highlight():
    assert _highlighted(_lex_line("use /clear, then continue")) == ["/clear"]


def _mentions(fragments: StyleAndTextTuples) -> list[str]:
    return _styled(fragments, "class:file-mention")


def test_at_mention_highlighted_at_boundary():
    fragments = _lex_line("please read @src/main.py now")
    assert _mentions(fragments) == ["@src/main.py"]
    assert "".join(frag[1] for frag in fragments) == "please read @src/main.py now"


def test_at_mention_at_start_highlighted():
    assert _mentions(_lex_line("@README.md")) == ["@README.md"]


def test_email_like_at_not_highlighted():
    # "@" glued to an alphanumeric is not a mention boundary.
    assert _mentions(_lex_line("ping foo@bar.com")) == []


def test_mention_suppressed_outside_agent_mode():
    assert _mentions(_lex_line("read @src/main.py", agent_mode=False)) == []


def test_slash_and_mention_compose_on_one_line():
    fragments = _lex_line("/clear then read @src/app.py")
    assert _highlighted(fragments) == ["/clear"]
    assert _mentions(fragments) == ["@src/app.py"]


def test_theme_subcommand_argument_highlighted():
    fragments = _lex_line("/theme cur")
    assert _highlighted(fragments) == ["/theme"]
    assert _arg_highlighted(fragments) == ["cur"]


def test_theme_subcommand_full_argument_highlighted():
    fragments = _lex_line("/theme current")
    assert _highlighted(fragments) == ["/theme"]
    assert _arg_highlighted(fragments) == ["current"]


def _bash(fragments: StyleAndTextTuples) -> list[str]:
    return _styled(fragments, "class:bash-prefix")


def test_leading_bang_highlighted():
    fragments = _lex_line("!ls -la")
    assert _bash(fragments) == ["!"]
    assert "".join(frag[1] for frag in fragments) == "!ls -la"


def test_bang_without_command_not_highlighted():
    assert _bash(_lex_line("!")) == []
    assert _bash(_lex_line("!   ")) == []


def test_bang_only_at_buffer_start():
    # Leading whitespace means it is not a one-shot shell command.
    assert _bash(_lex_line(" !ls")) == []
    # A "!" later in the line is not a prefix.
    assert _bash(_lex_line("echo !ls")) == []


def test_bang_suppressed_outside_agent_mode():
    assert _bash(_lex_line("!ls", agent_mode=False)) == []


def test_bang_only_on_first_line():
    text = "first line\n!ls"
    assert _bash(_lex_line(text, lineno=1)) == []
