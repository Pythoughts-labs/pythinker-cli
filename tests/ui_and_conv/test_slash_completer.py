"""Tests for slash command completer behavior."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from types import SimpleNamespace

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import CompleteEvent, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.layout.containers import ConditionalContainer, FloatContainer, HSplit, Window
from prompt_toolkit.utils import get_cwidth

import pythinker_code.ui.shell.prompt as prompt_mod
from pythinker_code.ui.shell.prompt import (
    LocalFileMentionCompleter,
    LocalFileMentionMenuControl,
    SlashCommandAutoSuggest,
    SlashCommandCompleter,
    SlashCommandMenuControl,
    _discard_slash_command,
    _find_prompt_float_container,
    _wrap_to_width,
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


def _completion_texts(completer: SlashCommandCompleter, text: str) -> list[str]:
    document = Document(text=text, cursor_position=len(text))
    event = CompleteEvent(completion_requested=True)
    return [completion.text for completion in completer.get_completions(document, event)]


def _completions(completer: SlashCommandCompleter, text: str):
    document = Document(text=text, cursor_position=len(text))
    event = CompleteEvent(completion_requested=True)
    return list(completer.get_completions(document, event))


def _theme_completer() -> SlashCommandCompleter:
    return SlashCommandCompleter(
        [_make_command("theme", aliases=["color"]), _make_command("help")],
        arg_suggestions=slash_command_arg_suggestions,
    )


def test_exact_command_match_keeps_completions_visible():
    """Exact matches should still show completions so the slash menu stays open."""
    completer = SlashCommandCompleter(
        [
            _make_command("mcp"),
            _make_command("mcp-server"),
            _make_command("help", aliases=["h"]),
        ]
    )

    texts = _completion_texts(completer, "/mcp")

    assert "/mcp" in texts


def test_exact_alias_match_keeps_completions_visible():
    """Exact alias matches should still show the canonical completion."""
    completer = SlashCommandCompleter(
        [
            _make_command("help", aliases=["h"]),
            _make_command("history"),
        ]
    )

    texts = _completion_texts(completer, "/h")

    assert "/help" in texts


def test_command_name_prefix_outranks_exact_alias_match():
    """Typing toward a command name surfaces that command first, even when a
    different command claims the typed text as an exact alias. ``/report`` lists
    ``/reports`` above ``/report_error`` (whose alias is ``report``)."""
    completer = SlashCommandCompleter(
        [
            _make_command("report_error", aliases=["report-error", "report"]),
            _make_command("reports"),
        ]
    )

    texts = _completion_texts(completer, "/report")

    assert texts == ["/reports", "/report_error"]


def test_alias_only_match_surfaces_matched_alias():
    """When the typed prefix matches only via an alias, the menu surfaces the
    matched alias (``/res`` → ``/resume``) so the user sees what they typed
    toward, even though it maps to a differently-named command (``/sessions``).
    Name matches still outrank alias-only matches."""
    completer = SlashCommandCompleter(
        [
            _make_command("sessions", aliases=["resume", "session"]),
            _make_command("restore"),
        ]
    )

    assert _completion_texts(completer, "/res") == ["/restore", "/resume"]


def test_exact_alias_match_surfaces_matched_alias():
    """An exact alias match shows the alias the user typed, not the canonical name."""
    completer = SlashCommandCompleter([_make_command("clear", aliases=["reset"])])

    assert _completion_texts(completer, "/reset") == ["/reset"]


def test_namespaced_command_matches_bare_segment_inserts_canonical():
    """Typing the bare segment of a `skill:`/`flow:` command surfaces it and
    inserts the canonical name, so `/designer` -> `/skill:designer-skill`."""
    completer = SlashCommandCompleter(
        [_make_command("skill:designer-skill"), _make_command("help")]
    )

    # Exact-segment, prefix-segment, and the namespaced prefix all resolve to
    # the canonical command name (never the bare segment).
    assert _completion_texts(completer, "/designer-skill") == ["/skill:designer-skill"]
    assert _completion_texts(completer, "/designer") == ["/skill:designer-skill"]
    assert _completion_texts(completer, "/design") == ["/skill:designer-skill"]
    assert _completion_texts(completer, "/skill:design") == ["/skill:designer-skill"]


def test_name_prefix_outranks_namespaced_segment_match():
    """A direct name-prefix match ranks above a namespaced segment match."""
    completer = SlashCommandCompleter(
        [_make_command("skill:design-system"), _make_command("design")]
    )

    # `/design` prefix-matches the plain `design` command (tier 1) above the
    # `skill:design-system` segment match (tier 5).
    assert _completion_texts(completer, "/design") == [
        "/design",
        "/skill:design-system",
    ]


def test_shorter_command_name_prefix_ranks_first():
    """Within the same match tier the closest (shortest) command name wins."""
    completer = SlashCommandCompleter(
        [
            _make_command("settings"),
            _make_command("set"),
            _make_command("setup-wizard"),
        ]
    )

    assert _completion_texts(completer, "/set") == [
        "/set",
        "/settings",
        "/setup-wizard",
    ]


def test_should_complete_only_for_root_slash_token():
    assert SlashCommandCompleter.should_complete(Document(text="/", cursor_position=1))
    assert SlashCommandCompleter.should_complete(Document(text="  /he", cursor_position=5))
    assert not SlashCommandCompleter.should_complete(Document(text="test /he", cursor_position=8))
    assert not SlashCommandCompleter.should_complete(Document(text="@src", cursor_position=4))
    assert not SlashCommandCompleter.should_complete(Document(text="/he next", cursor_position=8))


def test_completion_active_for_theme_subcommand():
    completer = _theme_completer()
    assert completer.completion_active(Document(text="/theme ", cursor_position=len("/theme ")))
    assert completer.completion_active(
        Document(text="/theme cur", cursor_position=len("/theme cur"))
    )
    assert not completer.completion_active(
        Document(text="/help foo", cursor_position=len("/help foo"))
    )


def _suggestion_text(names: frozenset[str], text: str) -> str | None:
    suggest = SlashCommandAutoSuggest(
        lambda: names,
        arg_suggestions=slash_command_arg_suggestions,
    )
    document = Document(text=text, cursor_position=len(text))
    suggestion = suggest.get_suggestion(Buffer(), document)
    return suggestion.text if suggestion else None


def _suggestion_text_with_exact(
    names: frozenset[str], exact: dict[str, str], text: str
) -> str | None:
    suggest = SlashCommandAutoSuggest(lambda: names, exact_suggestions=lambda: exact)
    document = Document(text=text, cursor_position=len(text))
    suggestion = suggest.get_suggestion(Buffer(), document)
    return suggestion.text if suggestion else None


def test_auto_suggest_completes_best_prefix_match():
    """Typing a slash prefix ghost-renders the remainder of the first matching
    command (alphabetical), which Tab accepts inline."""
    names = frozenset({"clean-code-guard", "clear", "help"})
    assert _suggestion_text(names, "/clean") == "-code-guard"
    assert _suggestion_text(names, "/cl") == "ean-code-guard"
    assert _suggestion_text(names, "/h") == "elp"


def test_auto_suggest_completes_mid_line_slash_token():
    """A slash token after other words still ghost-completes (Tab fills it in),
    while the dropdown menu stays line-start-only."""
    names = frozenset({"designer-skill:designer-skill", "help"})
    assert _suggestion_text(names, "use /designer") == "-skill:designer-skill"
    assert _suggestion_text(names, "please run /he") == "lp"
    # The completion *menu* must not open mid-line.
    assert not SlashCommandCompleter.should_complete(
        Document(text="use /designer", cursor_position=len("use /designer"))
    )


def test_auto_suggest_inactive_outside_root_slash_token():
    names = frozenset({"help"})
    assert _suggestion_text(names, "/") is None  # bare slash: menu handles discovery
    assert _suggestion_text(names, "/zzz") is None  # no match
    assert _suggestion_text(names, "/help") is None  # already complete
    assert _suggestion_text(names, "path/he") is None  # glued, not a slash token
    assert _suggestion_text(names, "/he next") is None  # slash token isn't last
    assert _suggestion_text(names, "plain text") is None


def test_auto_suggest_exact_recap_toggle_hint():
    names = frozenset({"recap", "help"})
    assert _suggestion_text_with_exact(names, {"recap": " off"}, "/recap") == " off"
    assert _suggestion_text_with_exact(names, {"recap": " off"}, "please /recap") == " off"
    assert _suggestion_text_with_exact(names, {"recap": " off"}, "/recap today") is None


def test_auto_suggest_is_case_insensitive_on_typed_prefix():
    names = frozenset({"help"})
    assert _suggestion_text(names, "/He") == "lp"


def test_auto_suggest_theme_subcommand_after_space():
    names = frozenset({"theme", "color"})
    assert _suggestion_text(names, "/theme ") == "current"


def test_auto_suggest_theme_subcommand_prefix():
    names = frozenset({"theme"})
    assert _suggestion_text(names, "/theme cur") == "rent"
    assert _suggestion_text(names, "/theme doc") == "tor"


def test_theme_subcommand_completions():
    completer = _theme_completer()
    texts = _completion_texts(completer, "/theme ")
    assert "current" in texts
    assert "doctor" in texts
    assert "tokens" in texts


def test_theme_subcommand_prefix_completions():
    completer = _theme_completer()
    completions = _completions(completer, "/theme cur")
    assert len(completions) == 1
    assert completions[0].text == "rent"


def test_file_mention_should_complete_for_active_at_fragment():
    assert LocalFileMentionCompleter.should_complete(
        Document(text="check @src", cursor_position=10)
    )
    assert LocalFileMentionCompleter.should_complete(Document(text="check @", cursor_position=7))
    assert not LocalFileMentionCompleter.should_complete(
        Document(text="email test@example.com", cursor_position=22)
    )
    assert not LocalFileMentionCompleter.should_complete(
        Document(text="check @src next", cursor_position=15)
    )


def test_discard_slash_command_clears_root_slash_draft():
    buffer = Buffer()
    buffer.set_document(Document(text="/theme", cursor_position=6), bypass_readonly=True)

    assert _discard_slash_command(buffer) is True
    assert buffer.text == ""


def test_discard_slash_command_ignores_non_root_slash_text():
    buffer = Buffer()
    buffer.set_document(Document(text="ask /theme", cursor_position=10), bypass_readonly=True)

    assert _discard_slash_command(buffer) is False
    assert buffer.text == "ask /theme"


def test_completion_display_uses_canonical_command_name():
    completer = SlashCommandCompleter(
        [
            _make_command("help", aliases=["h", "?"]),
            _make_command("history"),
        ]
    )

    completions = _completions(completer, "/he")

    assert len(completions) == 1
    assert completions[0].text == "/help"
    assert completions[0].display_text == "/help"
    assert completions[0].display_meta_text == "help command"


def test_annotated_command_meta_drops_generic_tag_but_keeps_aliases():
    """Plain commands no longer carry a redundant scope tag in the menu; the
    description and aliases remain so the row stays informative."""
    completer = SlashCommandCompleter(
        [_make_command("help", aliases=["h", "?"])],
        annotate_meta=True,
        command_scope="shell",
    )

    completions = _completions(completer, "/h")

    assert len(completions) == 1
    assert completions[0].display_meta_text == "help command  aliases: /h, /?"
    assert "[shell]" not in completions[0].display_meta_text
    assert "[command]" not in completions[0].display_meta_text


def test_annotated_skill_completion_uses_skill_kind():
    completer = SlashCommandCompleter(
        [_make_command("skill:demo")],
        annotate_meta=True,
    )

    completions = _completions(completer, "/skill:de")

    assert len(completions) == 1
    assert completions[0].display_meta_text.startswith("[skill]")


def test_skill_completion_path_still_returns_registered_skill_command():
    completer = SlashCommandCompleter(
        [
            _make_command("skill:demo"),
            _make_command("help"),
        ]
    )

    assert _completion_texts(completer, "/skill:de") == ["/skill:demo"]
    assert _completion_texts(completer, "/skill:demo") == ["/skill:demo"]


def test_flow_completion_path_still_returns_registered_flow_command():
    completer = SlashCommandCompleter(
        [
            _make_command("flow:demo"),
            _make_command("help"),
        ]
    )

    assert _completion_texts(completer, "/flow:de") == ["/flow:demo"]
    assert _completion_texts(completer, "/flow:demo") == ["/flow:demo"]


def test_wrap_to_width_respects_width():
    lines = _wrap_to_width(
        "Help address review issue comments on the open GitHub PR",
        18,
    )

    assert len(lines) > 1
    assert all(get_cwidth(line) <= 18 for line in lines)


def test_wrap_to_width_respects_max_lines():
    lines = _wrap_to_width(
        "Help address review issue comments on the open GitHub PR for the current branch",
        20,
        max_lines=2,
    )

    assert len(lines) == 2
    assert all(get_cwidth(line) <= 20 for line in lines)
    assert lines[-1].endswith("...")


def test_file_mention_menu_renders_clean_two_column_layout(monkeypatch):
    completions = [
        Completion(text=".coderabbit.yaml", start_position=0, display=".coderabbit.yaml"),
        Completion(text=".dockerignore", start_position=0, display=".dockerignore"),
        Completion(text=".pytest_cache/", start_position=0, display=".pytest_cache/"),
    ]
    complete_state = SimpleNamespace(completions=completions, complete_index=None)
    app = SimpleNamespace(current_buffer=SimpleNamespace(complete_state=complete_state))
    monkeypatch.setattr(prompt_mod, "get_app_or_none", lambda: app)

    control = LocalFileMentionMenuControl(left_padding=lambda: 0)
    content = control.create_content(width=80, height=6)
    rendered_lines = [
        "".join(fragment[1] for fragment in content.get_line(i)) for i in range(content.line_count)
    ]

    assert content.cursor_position.y == 0
    assert rendered_lines[0].startswith("→ .coderabbit.yaml")
    assert ".coderabbit.yaml" in rendered_lines[0][20:]
    assert rendered_lines[1].startswith("  .dockerignore")
    assert ".pytest_cache" in rendered_lines[2]
    assert rendered_lines[-1].strip() == "(1/3)"


def test_file_mention_menu_counter_tracks_selected_completion(monkeypatch):
    completions = [
        Completion(text=f"path-{index}.py", start_position=0, display=f"path-{index}.py")
        for index in range(6)
    ]
    complete_state = SimpleNamespace(completions=completions, complete_index=3)
    app = SimpleNamespace(current_buffer=SimpleNamespace(complete_state=complete_state))
    monkeypatch.setattr(prompt_mod, "get_app_or_none", lambda: app)

    control = LocalFileMentionMenuControl(left_padding=lambda: 0)
    content = control.create_content(width=48, height=4)
    rendered_lines = [
        "".join(fragment[1] for fragment in content.get_line(i)) for i in range(content.line_count)
    ]

    assert any(line.startswith("→ path-3.py") for line in rendered_lines)
    assert rendered_lines[-1].strip() == "(4/6)"


def test_slash_menu_preselects_first_item_when_index_unset(monkeypatch):
    """When the slash menu opens with `complete_index is None`, the first row
    must render as visually highlighted (`❯` marker, current style) and the
    cursor must land on that row. This matches what Enter actually commits
    (the first completion) and what `_install_prompt_buffer_visibility`'s
    `on_completions_changed` handler sets `complete_index` to as soon as the
    menu materializes."""
    completions = [
        Completion(
            text="/editor",
            start_position=0,
            display="/editor",
            display_meta="Set default external editor for Ctrl-O",
        ),
        Completion(
            text="/exit",
            start_position=0,
            display="/exit",
            display_meta="Exit the application",
        ),
    ]
    complete_state = SimpleNamespace(completions=completions, complete_index=None)
    app = SimpleNamespace(current_buffer=SimpleNamespace(complete_state=complete_state))
    monkeypatch.setattr(prompt_mod, "get_app_or_none", lambda: app)

    control = SlashCommandMenuControl(left_padding=lambda: 0)
    content = control.create_content(width=80, height=6)

    rendered_lines = [
        "".join(fragment[1] for fragment in content.get_line(i)) for i in range(content.line_count)
    ]

    # A blank gap line precedes the list; a blank separator and footer legend
    # follow it, so the menu reads as its own region.
    assert content.line_count == len(completions) + 3
    assert rendered_lines[0].strip() == ""
    assert content.cursor_position.y == 1
    # First item row is highlighted, second is not. U+276F is the selection
    # marker; built via chr() to avoid RUF001 ambiguous-glyph lint.
    selected_marker = chr(0x276F)
    assert selected_marker in rendered_lines[1]
    assert selected_marker not in rendered_lines[2]
    assert "Ctrl-O" in rendered_lines[1]
    assert rendered_lines[1].count("/editor") == 1
    # Blank separator then the footer legend on the last two lines.
    assert rendered_lines[-2].strip() == ""
    assert rendered_lines[-1].strip() == "Enter to select · ↑/↓ to navigate · Esc to cancel"


def _slash_completions(count: int) -> list[Completion]:
    return [
        Completion(
            text=f"/cmd{i}",
            start_position=0,
            display=f"/cmd{i}",
            display_meta=f"command number {i}",
        )
        for i in range(count)
    ]


def test_slash_menu_footer_folds_in_overflow_count_when_list_exceeds_height(monkeypatch):
    """When more completions exist than fit, the footer leads with the hidden
    count alongside the navigation legend instead of silently truncating."""
    completions = _slash_completions(20)
    complete_state = SimpleNamespace(completions=completions, complete_index=None)
    app = SimpleNamespace(current_buffer=SimpleNamespace(complete_state=complete_state))
    monkeypatch.setattr(prompt_mod, "get_app_or_none", lambda: app)

    control = SlashCommandMenuControl(left_padding=lambda: 0)
    content = control.create_content(width=80, height=5)

    rendered_lines = [
        "".join(fragment[1] for fragment in content.get_line(i)) for i in range(content.line_count)
    ]

    # Gap line + visible items + blank separator + footer, within the 5-row budget.
    assert content.line_count == 5
    assert rendered_lines[0].strip() == ""
    assert rendered_lines[-2].strip() == ""
    footer = rendered_lines[-1].strip()
    visible_items = content.line_count - 3  # minus gap, separator, footer
    assert footer.startswith(f"+{20 - visible_items} more · ")
    assert footer.endswith("Enter to select · ↑/↓ to navigate · Esc to cancel")


def test_slash_menu_footer_shows_legend_without_count_when_list_fits(monkeypatch):
    completions = _slash_completions(2)
    complete_state = SimpleNamespace(completions=completions, complete_index=None)
    app = SimpleNamespace(current_buffer=SimpleNamespace(complete_state=complete_state))
    monkeypatch.setattr(prompt_mod, "get_app_or_none", lambda: app)

    control = SlashCommandMenuControl(left_padding=lambda: 0)
    content = control.create_content(width=80, height=6)

    rendered_lines = [
        "".join(fragment[1] for fragment in content.get_line(i)) for i in range(content.line_count)
    ]

    assert content.line_count == len(completions) + 3  # gap + items + separator + footer
    assert rendered_lines[-2].strip() == ""
    assert rendered_lines[-1].strip() == "Enter to select · ↑/↓ to navigate · Esc to cancel"
    assert not any("more · " in line for line in rendered_lines)


def test_annotated_plain_command_meta_has_no_tag():
    completer = SlashCommandCompleter(
        [_make_command("help")],
        annotate_meta=True,
        command_scope="command",
    )

    completions = _completions(completer, "/he")

    assert completions[0].display_meta_text == "help command"
    assert "[command]" not in completions[0].display_meta_text


def test_find_prompt_float_container_supports_conditional_container_shape():
    float_container = FloatContainer(content=Window(), floats=[])
    root = HSplit(
        [
            ConditionalContainer(
                content=Window(),
                filter=True,
                alternative_content=float_container,
            )
        ]
    )

    assert _find_prompt_float_container(root) is float_container


def test_find_prompt_float_container_supports_direct_float_container_shape():
    float_container = FloatContainer(content=Window(), floats=[])
    root = HSplit([float_container])

    assert _find_prompt_float_container(root) is float_container


def test_task_unavailable_commands_annotated_during_run():
    """Shell commands not flagged task-safe show a disabled meta while a turn runs."""
    completer = SlashCommandCompleter(
        [_make_command("settings"), _make_command("statusline")],
        annotate_meta=True,
        is_task_running=lambda: True,
    )
    metas = {c.text: c.display_meta_text for c in _completions(completer, "/")}
    assert metas["/settings"] == "disabled while a task is in progress"
    assert metas["/statusline"] != "disabled while a task is in progress"


def test_no_disabled_annotation_when_idle():
    completer = SlashCommandCompleter(
        [_make_command("settings")],
        annotate_meta=True,
        is_task_running=lambda: False,
    )
    metas = {c.text: c.display_meta_text for c in _completions(completer, "/")}
    assert "disabled" not in metas["/settings"]
