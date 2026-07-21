from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from prompt_toolkit.completion import Completion

from pythinker_code.llm import ModelCapability
from pythinker_code.soul import StatusSnapshot
from pythinker_code.ui.shell import prompt as shell_prompt
from pythinker_code.ui.shell.glyphs import SPINNER_FRAMES
from pythinker_code.ui.shell.prompt import (
    PROMPT_SYMBOL,
    BgTaskCounts,
    CustomPromptSession,
    PromptMode,
    SlashCommandCompleter,
    SlashCommandMenuControl,
    UserInput,
    _build_toolbar_tips,
    _display_width,
    _format_git_badge,
    _PromptRightPaddingMargin,
    _shorten_cwd,
    _toast_queues,
    _truncate_left,
    _truncate_right,
    toast,
)
from pythinker_code.utils.slashcmd import SlashCommand

# ── Shared helpers ─────────────────────────────────────────────────────────────


def test_shortcut_help_popup_fits_narrow_columns() -> None:
    prompt_session = object.__new__(CustomPromptSession)

    fragments = prompt_session._render_shortcut_help(columns=20)
    plain = "".join(fragment[1] for fragment in fragments)

    assert "Shortcuts" in plain
    assert all(len(line) <= 20 for line in plain.splitlines())


def test_shortcut_help_popup_lists_prompt_and_transcript_shortcuts() -> None:
    prompt_session = object.__new__(CustomPromptSession)

    fragments = prompt_session._render_shortcut_help(columns=90)
    plain = "".join(fragment[1] for fragment in fragments)

    assert "!" in plain
    assert "run one shell command" in plain
    assert "expand/collapse tool output (transcript)" in plain


def test_prompt_continuation_aligns_wrapped_input_with_text_start() -> None:
    prompt_session = object.__new__(CustomPromptSession)

    fragments = prompt_session._render_prompt_continuation(
        width=2,
        line_number=1,
        is_soft_wrap=True,
    )

    assert "".join(fragment[1] for fragment in fragments) == "  "


def test_agent_input_text_keeps_default_color_when_thinking_changes() -> None:
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._mode = PromptMode.AGENT
    prompt_session._thinking_effort = "high"
    prompt_session._thinking = True

    assert prompt_session._thinking_input_style() == "class:compact-input"
    assert prompt_session._thinking_prompt_prefix_style() == "class:compact-input.prompt"


def test_shell_input_keeps_default_compact_input_style() -> None:
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._mode = PromptMode.SHELL
    prompt_session._thinking_effort = "high"
    prompt_session._thinking = True

    assert prompt_session._thinking_input_style() == "class:compact-input"


def test_prompt_right_padding_margin_reserves_blank_edge_columns() -> None:
    margin = _PromptRightPaddingMargin(lambda: 2)
    render_info = SimpleNamespace(displayed_lines=[0, 0, 0])

    fragments = margin.create_margin(cast(Any, render_info), width=2, height=3)

    assert margin.get_width(lambda: cast(Any, None)) == 2
    assert "".join(fragment[1] for fragment in fragments) == "  \n  \n  \n"


def test_prompt_toolkit_keyprocessor_shutdown_noise_is_filtered() -> None:
    unraisable = SimpleNamespace(
        exc_value=KeyError("__import__"),
        object="<coroutine object KeyProcessor._start_timeout.<locals>.wait at 0x123>",
    )

    assert shell_prompt._is_prompt_toolkit_keyprocessor_shutdown_noise(unraisable)


def test_other_unraisable_exceptions_are_not_filtered() -> None:
    unraisable = SimpleNamespace(
        exc_value=KeyError("other"),
        object="<coroutine object KeyProcessor._start_timeout.<locals>.wait at 0x123>",
    )

    assert not shell_prompt._is_prompt_toolkit_keyprocessor_shutdown_noise(unraisable)


class _DummyRunningPrompt:
    modal_priority = 10

    def render_running_prompt_body(self, columns: int) -> str:
        return f"live view ({columns})"

    def running_prompt_placeholder(self) -> str | None:
        return None

    def running_prompt_allows_text_input(self) -> bool:
        return True

    def running_prompt_hides_input_buffer(self) -> bool:
        return False

    def running_prompt_accepts_submission(self) -> bool:
        return True

    def should_handle_running_prompt_key(self, key: str) -> bool:
        return key == "enter"

    def handle_running_prompt_key(self, key: str, event) -> None:
        raise AssertionError("Should not be called in this test")


class _DummyReadOnlyModal:
    modal_priority = 20

    def render_running_prompt_body(self, columns: int) -> str:
        return f"modal body ({columns})"

    def running_prompt_placeholder(self) -> None:
        return None

    def running_prompt_allows_text_input(self) -> bool:
        return False

    def running_prompt_hides_input_buffer(self) -> bool:
        return True

    def running_prompt_accepts_submission(self) -> bool:
        return True

    def should_handle_running_prompt_key(self, key: str) -> bool:
        return key == "enter"

    def handle_running_prompt_key(self, key: str, event) -> None:
        raise AssertionError("Should not be called in this test")


def _make_toolbar_session(
    *,
    model_name: str | None = None,
    model_capabilities: set[ModelCapability] | None = None,
    tips: list[str] | None = None,
) -> Any:
    """Build a minimal CustomPromptSession for toolbar rendering tests."""
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._mode = PromptMode.AGENT
    prompt_session._model_name = model_name
    prompt_session._model_capabilities = model_capabilities or set()
    prompt_session._thinking = False
    prompt_session._status_provider = lambda: StatusSnapshot(context_usage=0.0)
    prompt_session._background_task_count_provider = None
    prompt_session._tips = tips if tips is not None else []
    prompt_session._tip_rotation_index = 0
    prompt_session._last_tip_rotate_time = float("inf")  # prevent time-based rotation
    return prompt_session


def _render_toolbar_fragments(
    prompt_session: Any,
    width: int,
    monkeypatch: Any,
    *,
    git_branch: str | None = None,
    git_status_result: tuple[bool, int, int] = (False, 0, 0),
    cwd: str = "~/proj",
    before_render: Callable[[], None] | None = None,
) -> list[tuple[str, str]]:
    """Patch the environment, optionally run setup, render the toolbar fragments."""

    class _DummyOutput:
        @staticmethod
        def get_size() -> Any:
            return SimpleNamespace(columns=width)

    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "pythinker")
    monkeypatch.setattr(
        shell_prompt, "get_app_or_none", lambda: SimpleNamespace(output=_DummyOutput())
    )
    monkeypatch.setattr(shell_prompt, "_get_git_branch", lambda: git_branch)
    monkeypatch.setattr(shell_prompt, "_get_git_status", lambda: git_status_result)
    monkeypatch.setattr(shell_prompt, "_shorten_cwd", lambda _: cwd)
    _toast_queues["left"].clear()
    _toast_queues["right"].clear()
    if before_render is not None:
        before_render()

    return list(prompt_session._render_bottom_toolbar())


def _render_toolbar_lines(
    prompt_session: Any,
    width: int,
    monkeypatch: Any,
    *,
    git_branch: str | None = None,
    git_status_result: tuple[bool, int, int] = (False, 0, 0),
    cwd: str = "~/proj",
    before_render: Callable[[], None] | None = None,
) -> list[str]:
    """Patch the environment, optionally run setup, render the toolbar, return lines."""
    rendered = _render_toolbar_fragments(
        prompt_session,
        width,
        monkeypatch,
        git_branch=git_branch,
        git_status_result=git_status_result,
        cwd=cwd,
        before_render=before_render,
    )
    plain = "".join(fragment[1] for fragment in rendered)
    return cast(list[str], plain.split("\n"))


# ── _build_toolbar_tips ────────────────────────────────────────────────────────


def test_build_toolbar_tips_without_clipboard() -> None:
    assert _build_toolbar_tips(clipboard_available=False) == [
        "?: shortcuts",
        "ctrl+x: toggle mode",
        "shift+tab: change thinking effort",
        "!: shell command",
        "ctrl+o: editor",
        "ctrl+t: toggle todos",
        "ctrl+j/alt+enter: newline",
        "/feedback: send feedback",
        "/theme: switch dark/light",
        "@: mention files",
    ]


def test_build_toolbar_tips_with_clipboard() -> None:
    assert _build_toolbar_tips(clipboard_available=True) == [
        "?: shortcuts",
        "ctrl+x: toggle mode",
        "shift+tab: change thinking effort",
        "!: shell command",
        "ctrl+o: editor",
        "ctrl+t: toggle todos",
        "ctrl+j/alt+enter: newline",
        "/feedback: send feedback",
        "/theme: switch dark/light",
        "ctrl+v: paste clipboard",
        "@: mention files",
    ]


def test_working_spinner_tip_mentions_thinking_effort_shortcut() -> None:
    from pythinker_code.ui.shell.tips import current_tip

    assert current_tip(0) == "Shift+Tab changes thinking effort levels"


def test_feature_tips_reference_only_real_slash_commands() -> None:
    """Every /command a tip advertises must exist in a slash registry.

    Tips are marketing for real features; a tip naming a nonexistent
    command (e.g. a past "/verify" tip) erodes trust in all of them.
    """
    import re as _re

    from pythinker_code.soul.slash import registry as soul_registry
    from pythinker_code.ui.shell.slash import registry as shell_registry
    from pythinker_code.ui.shell.slash import shell_mode_registry
    from pythinker_code.ui.shell.tips import FEATURE_TIPS

    known = (
        set(soul_registry._command_aliases)
        | set(shell_registry._command_aliases)
        | set(shell_mode_registry._command_aliases)
    )
    for tip in FEATURE_TIPS:
        for command in _re.findall(r"/([a-z][a-z-]*)", tip):
            assert command in known, f"tip advertises unknown command /{command}: {tip!r}"


# ── _display_width ─────────────────────────────────────────────────────────────


def test_display_width_empty() -> None:
    assert _display_width("") == 0


def test_display_width_ascii() -> None:
    assert _display_width("hello") == 5


# ── _truncate_left / _truncate_right ──────────────────────────────────────────


def test_truncate_left_within_limit_unchanged() -> None:
    assert _truncate_left("hello", 10) == "hello"


def test_truncate_left_ascii_exceeds_limit() -> None:
    # "abcde" width=5, max=4 → budget=3 → keep last 3 chars → "…cde"
    result = _truncate_left("abcde", 4)
    assert result == "…cde"
    assert _display_width(result) == 4


def test_truncate_right_within_limit_unchanged() -> None:
    assert _truncate_right("hello", 10) == "hello"


def test_truncate_right_ascii_exceeds_limit() -> None:
    # "abcde" width=5, max=4 → budget=3 → keep first 3 chars → "abc…"
    result = _truncate_right("abcde", 4)
    assert result == "abc…"
    assert _display_width(result) == 4


def test_truncate_right_zero_max_cols_returns_empty() -> None:
    # Contract: output width must be ≤ max_cols; when max_cols=0, must return ""
    assert _truncate_right("hello", 0) == ""


def test_truncate_left_zero_max_cols_returns_empty() -> None:
    # Contract: output width must be ≤ max_cols; when max_cols=0, must return ""
    assert _truncate_left("hello", 0) == ""


# ── _shorten_cwd ──────────────────────────────────────────────────────────────


def test_shorten_cwd_home_itself() -> None:
    home = os.path.expanduser("~")
    assert _shorten_cwd(home) == "~"


def test_shorten_cwd_subdirectory() -> None:
    home = os.path.expanduser("~")
    subdir = os.path.join(home, "projects", "myapp")
    assert _shorten_cwd(subdir) == "~/projects/myapp"


def test_shorten_cwd_unrelated_path() -> None:
    # A path outside of home is returned unchanged.
    assert _shorten_cwd("/etc/hosts") == "/etc/hosts"


# ── _format_git_badge ─────────────────────────────────────────────────────────


def test_format_git_badge_clean() -> None:
    assert _format_git_badge("main", False, 0, 0) == "main"


def test_format_git_badge_dirty_only() -> None:
    assert _format_git_badge("main", True, 0, 0) == "main [±]"


def test_format_git_badge_ahead_only() -> None:
    assert _format_git_badge("main", False, 3, 0) == "main [↑3]"


def test_format_git_badge_behind_only() -> None:
    assert _format_git_badge("main", False, 0, 1) == "main [↓1]"


def test_format_git_badge_all_three() -> None:
    assert _format_git_badge("main", True, 3, 1) == "main [± ↑3↓1]"


# ── Tip rotation logic ────────────────────────────────────────────────────────


def test_rotating_tips_empty_returns_none() -> None:
    session = _make_toolbar_session(tips=[])
    assert session._get_two_rotating_tips() is None
    assert session._get_one_rotating_tip() is None


def test_rotating_tips_single_tip_always_returned() -> None:
    session = _make_toolbar_session(tips=["ctrl-x: toggle mode"])
    assert session._get_two_rotating_tips() == "ctrl-x: toggle mode"
    assert session._get_one_rotating_tip() == "ctrl-x: toggle mode"


@pytest.mark.parametrize(
    "index,expected_two,expected_one",
    [
        (0, "tip-a | tip-b", "tip-a"),
        (1, "tip-b | tip-c", "tip-b"),
        (2, "tip-c | tip-a", "tip-c"),  # pair wraps around end of list
    ],
)
def test_rotating_tips_rotation_and_wrap(index: int, expected_two: str, expected_one: str) -> None:
    session = _make_toolbar_session(tips=["tip-a", "tip-b", "tip-c"])
    session._tip_rotation_index = index
    assert session._get_two_rotating_tips() == expected_two
    assert session._get_one_rotating_tip() == expected_one


def test_footer_hint_fragments_emphasize_keys_without_changing_text() -> None:
    fragments: list[tuple[str, str]] = []

    shell_prompt._append_footer_hint_fragments(
        fragments,
        "ctrl-x: toggle mode | /theme: switch dark/light",
        tip_style="tip",
        key_style="key",
    )

    assert (
        "".join(text for _, text in fragments) == "ctrl-x: toggle mode | /theme: switch dark/light"
    )
    assert fragments == [
        ("key", "ctrl-x"),
        ("tip", ": toggle mode"),
        ("tip", " | "),
        ("key", "/theme"),
        ("tip", ": switch dark/light"),
    ]


def test_background_task_summary_uses_single_task_command() -> None:
    assert shell_prompt._background_task_summary(BgTaskCounts()) is None
    assert (
        shell_prompt._background_task_summary(BgTaskCounts(bash=2, agent=1))
        == "3 background tasks running (2 bash, 1 agent) · /task to view"
    )


def test_bottom_toolbar_does_not_render_mcp_startup_status(monkeypatch: Any) -> None:
    prompt_session = _make_toolbar_session(tips=[])
    prompt_session._status_block_provider = lambda _columns: [
        ("", "• Booting MCP server: context7")
    ]

    fragments = _render_toolbar_fragments(prompt_session, 80, monkeypatch)
    plain = "".join(text for _, text in fragments)

    assert "Booting MCP server" not in plain


# ── Toolbar overflow invariants ───────────────────────────────────────────────


@pytest.mark.parametrize("width", [40, 60, 80])
def test_bottom_toolbar_never_overflows(width: int, monkeypatch: Any) -> None:
    # Toolbar must always render exactly 3 lines (separator + info + toast/context).
    # Neither content line may exceed the terminal width, even with a tip far longer
    # than the terminal width or on narrow terminals where degradation kicks in.
    prompt_session = _make_toolbar_session(tips=["x" * (width * 2)])

    lines = _render_toolbar_lines(prompt_session, width, monkeypatch)

    assert len(lines) == 3, f"expected 3 lines, got {len(lines)}: {lines!r}"
    assert _display_width(lines[1]) <= width, (
        f"info line overflows at width={width}: {_display_width(lines[1])}"
    )
    assert _display_width(lines[2]) <= width, (
        f"toast/context line overflows at width={width}: {_display_width(lines[2])}"
    )


@pytest.mark.parametrize("width", [40, 50, 60])
def test_bottom_toolbar_narrow_terminal_with_full_decoration(width: int, monkeypatch: Any) -> None:
    # Regression: on narrow terminals, combining a long CWD path, a git branch badge
    # (dirty + ahead + behind), an active bg bash task, and a model name must never
    # push line 1 past the terminal width. The toolbar must degrade gracefully.
    prompt_session = _make_toolbar_session(
        model_name="pythinker-latest",
        tips=["ctrl-x: toggle mode"],
    )
    prompt_session._background_task_count_provider = lambda: BgTaskCounts(bash=2, agent=0)

    lines = _render_toolbar_lines(
        prompt_session,
        width,
        monkeypatch,
        git_branch="feature/very-long-branch-name",
        git_status_result=(True, 3, 1),
        cwd="~/src/project/subdir",
    )

    assert len(lines) == 3, f"expected 3 lines at width={width}, got {len(lines)}"
    assert _display_width(lines[1]) <= width, (
        f"info line overflows at width={width}: {_display_width(lines[1])}"
    )
    assert _display_width(lines[2]) <= width, (
        f"toast/context line overflows at width={width}: {_display_width(lines[2])}"
    )


def test_bottom_toolbar_shows_bash_and_agent_badges_together(monkeypatch: Any) -> None:
    prompt_session = _make_toolbar_session(tips=[])
    prompt_session._background_task_count_provider = lambda: BgTaskCounts(bash=3, agent=1)

    lines = _render_toolbar_lines(prompt_session, 120, monkeypatch)

    assert "◇ bash: 3" in lines[1], f"bash badge missing: {lines[1]!r}"
    assert "◇ agent: 1" in lines[1], f"agent badge missing: {lines[1]!r}"
    assert lines[1].index("◇ bash: 3") < lines[1].index("◇ agent: 1"), (
        f"bash badge must come before agent badge: {lines[1]!r}"
    )


def test_bottom_toolbar_shows_agent_badge_alone_when_no_bash(monkeypatch: Any) -> None:
    prompt_session = _make_toolbar_session(tips=[])
    prompt_session._background_task_count_provider = lambda: BgTaskCounts(bash=0, agent=2)

    lines = _render_toolbar_lines(prompt_session, 120, monkeypatch)

    assert "◇ bash" not in lines[1], f"bash badge must not appear when count is 0: {lines[1]!r}"
    assert "◇ agent: 2" in lines[1], f"agent badge missing: {lines[1]!r}"


def test_background_working_status_uses_braille_spinner(monkeypatch: Any) -> None:
    prompt_session = _make_toolbar_session()
    prompt_session._background_task_count_provider = lambda: BgTaskCounts(bash=0, agent=1)

    monkeypatch.setattr(shell_prompt.time, "monotonic", lambda: 10.0)
    first = "".join(text for _, text, *_ in prompt_session._render_background_working_status(80))
    monkeypatch.setattr(shell_prompt.time, "monotonic", lambda: 10.08)
    second = "".join(text for _, text, *_ in prompt_session._render_background_working_status(80))

    assert first != second
    assert first[0] in SPINNER_FRAMES
    assert second[0] in SPINNER_FRAMES
    assert "background agent" not in first  # footer owns the count
    assert "background agent" not in second  # footer owns the count


def test_card_toolbar_shows_compact_background_task_summary(monkeypatch: Any) -> None:
    prompt_session = _make_toolbar_session(model_name="fast-model", tips=[])
    prompt_session._background_task_count_provider = lambda: BgTaskCounts(bash=2, agent=1)

    class _DummyOutput:
        @staticmethod
        def get_size() -> Any:
            return SimpleNamespace(columns=120)

    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "card")
    monkeypatch.setattr(
        shell_prompt, "get_app_or_none", lambda: SimpleNamespace(output=_DummyOutput())
    )
    monkeypatch.setattr(shell_prompt, "_get_git_branch", lambda: None)
    monkeypatch.setattr(shell_prompt, "_shorten_cwd", lambda _: "~/proj")
    monkeypatch.setattr("pythinker_code.extensions.footer_statuses", lambda: {})

    rendered = prompt_session._render_bottom_toolbar()
    plain = "".join(fragment[1] for fragment in rendered)

    assert "3 background tasks running (2 bash, 1 agent) · /task to view" in plain
    assert "/task list" not in plain


def test_card_toolbar_shows_model_via_statusline_renderer(monkeypatch: Any) -> None:
    from pythinker_code.ui.theme import get_statusline_colors, set_active_theme

    prompt_session = _make_toolbar_session(model_name="fast-model", tips=[])

    class _DummyOutput:
        @staticmethod
        def get_size() -> Any:
            return SimpleNamespace(columns=120)

    set_active_theme("dark")
    colors = get_statusline_colors()
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "card")
    monkeypatch.setattr(
        shell_prompt, "get_app_or_none", lambda: SimpleNamespace(output=_DummyOutput())
    )
    monkeypatch.setattr(shell_prompt, "_get_git_branch", lambda: None)
    monkeypatch.setattr(shell_prompt, "_get_git_diffstat", lambda: None)
    monkeypatch.setattr(shell_prompt, "_shorten_cwd", lambda _: "~/proj")
    monkeypatch.setattr("pythinker_code.extensions.footer_statuses", lambda: {})

    fragments = list(prompt_session._render_bottom_toolbar())

    # The v2 footer renders the model name through the statusline segment
    # registry with its dedicated model color (not the legacy theme token).
    assert (colors.model, "fast-model") in fragments
    # The legacy "context: <pct>%" label is gone; context renders only when a
    # context budget is known.
    assert not any("context:" in text for _, text in fragments)


def test_card_toolbar_separator_is_static_grey_regardless_of_effort(monkeypatch: Any) -> None:
    # The input border no longer borrows the thinking-effort color; every
    # separator is the one static frame grey (the effort signal moved to the
    # top-border label instead).
    from pythinker_code.ui.theme import set_active_theme

    prompt_session = _make_toolbar_session(
        model_name="fast-model", model_capabilities={"thinking"}, tips=[]
    )
    prompt_session._thinking = True
    prompt_session._thinking_effort = "xhigh"

    class _DummyOutput:
        @staticmethod
        def get_size() -> Any:
            return SimpleNamespace(columns=120)

    set_active_theme("dark")
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "card")
    monkeypatch.setattr(
        shell_prompt, "get_app_or_none", lambda: SimpleNamespace(output=_DummyOutput())
    )
    monkeypatch.setattr(shell_prompt, "_get_git_branch", lambda: None)
    monkeypatch.setattr(shell_prompt, "_shorten_cwd", lambda _: "~/proj")
    monkeypatch.setattr("pythinker_code.extensions.footer_statuses", lambda: {})

    fragments = list(prompt_session._render_bottom_toolbar())

    assert fragments[0] == (
        "class:compact-input.frame",
        shell_prompt._prompt_rule(120),
    )


def test_input_top_border_shows_effort_label() -> None:
    from prompt_toolkit.utils import get_cwidth

    from pythinker_code.ui.theme import set_active_theme, thinking_dot_style

    set_active_theme("dark")
    session = _make_toolbar_session(
        model_name="fast-model", model_capabilities={"thinking"}, tips=[]
    )
    session._thinking = True
    session._thinking_effort = "high"

    fragments = session._render_input_top_border(80, "class:fallback")

    # Border stays static grey; effort color lives only in the trailing dot.
    assert fragments[0][0] == "class:compact-input.frame"
    assert fragments[-2] == (thinking_dot_style("high", theme="dark"), "● ")
    assert fragments[-1] == ("class:compact-input.effort", "high")
    # The whole line stays within the rule budget so it never wraps.
    total = sum(get_cwidth(ch) for _, text in fragments for ch in text)
    assert total == len(shell_prompt._prompt_rule(80))


def test_input_top_border_hides_effort_label_for_native_and_nonthinking() -> None:
    # Native-thinking model (always_thinking, no user dial): no label.
    native = _make_toolbar_session(
        model_name="MiniMax M2.7", model_capabilities={"always_thinking"}, tips=[]
    )
    assert native._effort_label_fragments() == []
    assert native._render_input_top_border(80, "class:fallback") == [
        ("class:compact-input.frame", shell_prompt._prompt_rule(80))
    ]

    # Non-thinking model: no label either.
    plain = _make_toolbar_session(model_name="fast-model", model_capabilities=set(), tips=[])
    assert plain._effort_label_fragments() == []
    assert plain._render_input_top_border(80, "class:fallback") == [
        ("class:compact-input.frame", shell_prompt._prompt_rule(80))
    ]


def test_input_top_border_falls_back_to_plain_rule_when_too_narrow() -> None:
    from prompt_toolkit.utils import get_cwidth

    from pythinker_code.ui.theme import set_active_theme

    set_active_theme("dark")
    session = _make_toolbar_session(
        model_name="fast-model", model_capabilities={"thinking"}, tips=[]
    )
    session._thinking = True
    session._thinking_effort = "high"

    # At this width the rule is too short for the label plus its gap; appending
    # the flushed-right label would overflow the rule and wrap the line, so the
    # border must collapse to the plain full-width rule instead.
    narrow = 6
    fragments = session._render_input_top_border(narrow, "class:fallback")

    assert fragments == [("class:compact-input.frame", shell_prompt._prompt_rule(narrow))]
    total = sum(get_cwidth(ch) for _, text in fragments for ch in text)
    assert total == len(shell_prompt._prompt_rule(narrow))


def test_card_toolbar_separator_uses_standard_frame_for_non_thinking_models(
    monkeypatch: Any,
) -> None:
    prompt_session = _make_toolbar_session(
        model_name="fast-model", model_capabilities=set(), tips=[]
    )
    prompt_session._thinking = False
    prompt_session._thinking_effort = "off"

    class _DummyOutput:
        @staticmethod
        def get_size() -> Any:
            return SimpleNamespace(columns=120)

    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "card")
    monkeypatch.setattr(
        shell_prompt, "get_app_or_none", lambda: SimpleNamespace(output=_DummyOutput())
    )
    monkeypatch.setattr(shell_prompt, "_get_git_branch", lambda: None)
    monkeypatch.setattr(shell_prompt, "_shorten_cwd", lambda _: "~/proj")
    monkeypatch.setattr("pythinker_code.extensions.footer_statuses", lambda: {})

    fragments = list(prompt_session._render_bottom_toolbar())

    assert fragments[0] == (
        "class:compact-input.frame",
        shell_prompt._prompt_rule(120),
    )


def test_bottom_toolbar_drops_agent_badge_before_bash_when_narrow(monkeypatch: Any) -> None:
    # With only ~width budget for one badge after CWD/mode, keeping bash and
    # dropping agent is the documented priority.
    prompt_session = _make_toolbar_session(tips=[])
    prompt_session._background_task_count_provider = lambda: BgTaskCounts(bash=5, agent=5)

    lines = _render_toolbar_lines(prompt_session, 40, monkeypatch)

    # Must never overflow and the bash badge is preferred over the agent badge.
    assert _display_width(lines[1]) <= 40
    if "◇ agent" in lines[1]:
        # Only acceptable if bash also fit — otherwise priority is violated.
        assert "◇ bash" in lines[1], (
            f"agent badge appeared without bash badge at narrow width: {lines[1]!r}"
        )


def test_mode_shows_full_with_model_name_on_wide_terminal(monkeypatch: Any) -> None:
    """On a wide terminal the full mode string includes the model, but no effort."""
    session = _make_toolbar_session(model_name="fast-model")
    session._thinking = False
    lines = _render_toolbar_lines(session, 80, monkeypatch)
    assert "fast-model" in lines[1], f"model name missing on wide terminal: {lines[1]!r}"
    # Thinking effort is no longer shown in the footer (moved to the top border).
    assert "thinking" not in lines[1], f"footer should not show thinking effort: {lines[1]!r}"


def test_footer_omits_thinking_effort_for_all_models(monkeypatch: Any) -> None:
    # Neither controllable nor native-thinking models surface effort in the
    # footer anymore — the effort signal is the top-border label only.
    native = _make_toolbar_session(
        model_name="MiniMax M2.7",
        model_capabilities={"always_thinking"},
    )
    native._thinking = False
    native._thinking_effort = "off"
    lines = _render_toolbar_lines(native, 100, monkeypatch)
    assert "MiniMax M2.7" in lines[1]
    assert "native reasoning" not in lines[1]
    assert "thinking" not in lines[1]


def test_toolbar_mode_is_light_and_secondary_text_is_muted(monkeypatch: Any) -> None:
    from pythinker_code.ui.theme import get_tui_tokens, set_active_theme

    set_active_theme("dark")
    tokens = get_tui_tokens("dark")
    session = _make_toolbar_session(model_name="fast-model", tips=["?: shortcuts"])
    fragments = _render_toolbar_fragments(session, 120, monkeypatch)

    assert (
        f"fg:{tokens.text or tokens.activity_label}",
        "agent (fast-model)",
    ) in fragments
    assert (f"fg:{tokens.muted} bold", "?") in fragments
    assert (f"fg:{tokens.muted}", ": shortcuts") in fragments


def test_mode_drops_model_name_on_narrow_terminal(monkeypatch: Any) -> None:
    """On a terminal too narrow for "agent (model)", the model name is dropped to
    the bare mode and the footer never overflows."""
    # "agent (a-very-long-model-name-that-is-40-chars)" is ~47 cols; a 30-col
    # terminal can't fit it, so it degrades to the bare mode name.
    long_model = "a-very-long-model-name-that-is-40-chars"
    session = _make_toolbar_session(model_name=long_model)
    session._thinking = True
    lines = _render_toolbar_lines(session, 30, monkeypatch)
    assert long_model not in lines[1], (
        f"model name should be dropped on 30-col terminal: {lines[1]!r}"
    )
    assert "agent" in lines[1], f"bare mode name should remain: {lines[1]!r}"
    assert _display_width(lines[1]) <= 30


def test_mode_drops_model_name_and_dot_on_very_narrow_terminal(monkeypatch: Any) -> None:
    """On a terminal too narrow even for 'agent ○', only the bare mode name is shown."""
    # Force remaining to be tiny by using a very short model name but very narrow width.
    # "agent ○" = 8 cols; needs 10 cols with spacing. Use width=8 to force bare mode.
    session = _make_toolbar_session(model_name="m")
    session._thinking = False
    lines = _render_toolbar_lines(session, 8, monkeypatch)
    assert "m" not in lines[1] or lines[1].startswith("agent"), (
        f"bare mode expected on 8-col terminal: {lines[1]!r}"
    )
    assert _display_width(lines[1]) <= 8


# ── Line 2 structural correctness ─────────────────────────────────────────────


def test_toolbar_line2_context_appears_on_line2_not_line1(monkeypatch: Any) -> None:
    prompt_session = _make_toolbar_session(tips=[])
    lines = _render_toolbar_lines(prompt_session, 80, monkeypatch)

    assert "context: 0.0%" in lines[2]
    assert "context: 0.0%" not in lines[1]


def test_toolbar_line2_left_toast_appears_on_line2_not_line1(monkeypatch: Any) -> None:
    prompt_session = _make_toolbar_session(tips=[])

    lines = _render_toolbar_lines(
        prompt_session,
        80,
        monkeypatch,
        before_render=lambda: toast("mcp servers connected", topic="mcp", duration=10.0),
    )

    assert len(lines) == 3
    assert "mcp servers connected" in lines[2]
    assert "mcp servers connected" not in lines[1]


def test_toolbar_line2_long_left_toast_truncated_to_fit(monkeypatch: Any) -> None:
    width = 60
    prompt_session = _make_toolbar_session(tips=[])

    lines = _render_toolbar_lines(
        prompt_session,
        width,
        monkeypatch,
        before_render=lambda: toast("x" * 200, duration=10.0),
    )

    assert _display_width(lines[2]) <= width


def test_toolbar_line2_right_toast_replaces_context(monkeypatch: Any) -> None:
    prompt_session = _make_toolbar_session(tips=[])

    lines = _render_toolbar_lines(
        prompt_session,
        80,
        monkeypatch,
        before_render=lambda: toast("mcp connected", topic="mcp", duration=10.0, position="right"),
    )

    assert "mcp connected" in lines[2]
    assert "context:" not in lines[2]


def test_git_status_not_called_when_branch_is_none(monkeypatch: Any) -> None:
    """When not in a git repo (branch=None), _get_git_status must not be called.

    Avoids spawning a subprocess that will immediately fail in non-git directories.
    """
    status_call_count = 0

    def _fake_status() -> tuple[bool, int, int]:
        nonlocal status_call_count
        status_call_count += 1
        return (False, 0, 0)

    class _DummyOutput:
        @staticmethod
        def get_size() -> Any:
            return SimpleNamespace(columns=80)

    monkeypatch.setattr(
        shell_prompt, "get_app_or_none", lambda: SimpleNamespace(output=_DummyOutput())
    )
    monkeypatch.setattr(shell_prompt, "_get_git_branch", lambda: None)
    monkeypatch.setattr(shell_prompt, "_get_git_status", _fake_status)
    monkeypatch.setattr(shell_prompt, "_shorten_cwd", lambda _: "~/proj")
    _toast_queues["left"].clear()
    _toast_queues["right"].clear()

    prompt_session = _make_toolbar_session()
    prompt_session._render_bottom_toolbar()

    assert status_call_count == 0, "_get_git_status must not be called when branch is None"


# ── Prompt layout (lower text area, running/idle message) ─────────


def test_running_prompt_uses_shared_toolbar_and_bottom_input_layout(monkeypatch: Any) -> None:
    width = 72
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._mode = PromptMode.AGENT
    prompt_session._model_name = None
    prompt_session._running_prompt_delegate = _DummyRunningPrompt()
    prompt_session._turn_starting = False
    prompt_session._status_provider = lambda: StatusSnapshot(context_usage=0.0)
    prompt_session._background_task_count_provider = None
    prompt_session._thinking = False
    prompt_session._tips = ["tip"]
    prompt_session._tip_rotation_index = 0
    prompt_session._last_tip_rotate_time = float("inf")  # prevent time-based rotation

    class _DummyOutput:
        @staticmethod
        def get_size() -> Any:
            return SimpleNamespace(columns=width)

    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "pythinker")
    monkeypatch.setattr(
        shell_prompt, "get_app_or_none", lambda: SimpleNamespace(output=_DummyOutput())
    )
    monkeypatch.setattr(shell_prompt, "_get_git_branch", lambda: None)
    monkeypatch.setattr(shell_prompt, "_get_git_status", lambda: (False, 0, 0))
    monkeypatch.setattr(shell_prompt, "_shorten_cwd", lambda _: "~/proj")

    rendered_message = prompt_session._render_agent_prompt_message()
    plain_message = "".join(fragment[1] for fragment in rendered_message)
    assert plain_message == f"live view ({width})\n\n❯ "
    assert "─" not in plain_message
    assert "input" not in plain_message

    _toast_queues["left"].clear()
    _toast_queues["right"].clear()
    rendered_toolbar = prompt_session._render_bottom_toolbar()
    plain_toolbar = "".join(fragment[1] for fragment in rendered_toolbar)
    assert "tip" in plain_toolbar
    assert "context: 0.0%" in plain_toolbar


def test_running_prompt_preamble_is_clipped_on_short_terminals(monkeypatch: Any) -> None:
    width = 80
    rows = 12
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._mode = PromptMode.AGENT
    prompt_session._model_name = None
    prompt_session._status_provider = lambda: StatusSnapshot(context_usage=0.0)
    prompt_session._background_task_count_provider = None
    prompt_session._thinking = False

    class _TallRunningPrompt(_DummyRunningPrompt):
        def render_running_prompt_body(self, columns: int) -> str:
            return "\n".join(f"line {i}" for i in range(20))

    prompt_session._running_prompt_delegate = _TallRunningPrompt()
    prompt_session._turn_starting = False
    prompt_session._modal_delegates = []

    class _DummyOutput:
        @staticmethod
        def get_size() -> Any:
            return SimpleNamespace(columns=width, rows=rows)

    monkeypatch.setattr(
        shell_prompt, "get_app_or_none", lambda: SimpleNamespace(output=_DummyOutput())
    )

    rendered_message = prompt_session._render_agent_prompt_message()
    plain_message = "".join(fragment[1] for fragment in rendered_message)

    assert "earlier output hidden · Ctrl+O expand" in plain_message
    assert "Ctrl+E expand" not in plain_message
    assert len(plain_message.splitlines()) <= rows - 3
    assert plain_message.endswith("\n  ❯ ")


def test_clipped_agent_status_preserves_thinking_indicator(monkeypatch: Any) -> None:
    width = 80
    rows = 12
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._mode = PromptMode.AGENT
    prompt_session._model_name = None
    prompt_session._status_provider = lambda: StatusSnapshot(context_usage=0.0)
    prompt_session._background_task_count_provider = None
    prompt_session._thinking = False

    class _TallAgentStatus(_DummyRunningPrompt):
        def render_agent_status(self, columns: int) -> str:
            del columns
            tool_output = "\n".join(f"tool output line {i}" for i in range(20))
            return f"{tool_output}\n⠋ Prestigitating…"

        def render_running_prompt_body(self, columns: int) -> str:
            del columns
            return ""

    prompt_session._running_prompt_delegate = _TallAgentStatus()
    prompt_session._turn_starting = False
    prompt_session._modal_delegates = []

    class _DummyOutput:
        @staticmethod
        def get_size() -> Any:
            return SimpleNamespace(columns=width, rows=rows)

    monkeypatch.setattr(
        shell_prompt, "get_app_or_none", lambda: SimpleNamespace(output=_DummyOutput())
    )

    rendered_message = prompt_session._render_agent_prompt_message()
    plain_message = "".join(fragment[1] for fragment in rendered_message)

    assert "earlier output hidden · Ctrl+O expand" in plain_message
    assert "Prestigitating…" in plain_message
    assert plain_message.index("earlier output hidden · Ctrl+O expand") < plain_message.index(
        "Prestigitating…"
    )
    assert len(plain_message.splitlines()) <= rows - 3
    assert plain_message.endswith("\n  ❯ ")


def test_modal_prompt_hides_normal_separator_and_prompt_label(monkeypatch) -> None:
    width = 72
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._mode = PromptMode.AGENT
    prompt_session._model_name = None
    prompt_session._running_prompt_delegate = _DummyRunningPrompt()
    prompt_session._modal_delegates = [_DummyRunningPrompt()]
    prompt_session._status_provider = lambda: StatusSnapshot(context_usage=0.0)
    prompt_session._thinking = False

    class _DummyOutput:
        @staticmethod
        def get_size():
            return SimpleNamespace(columns=width)

    dummy_app = SimpleNamespace(output=_DummyOutput())
    monkeypatch.setattr(shell_prompt, "get_app_or_none", lambda: dummy_app)

    rendered_message = prompt_session._render_agent_prompt_message()
    plain_message = "".join(fragment[1] for fragment in rendered_message)

    assert plain_message == f"live view ({width})\n"
    assert f"\n{'─' * width}\n" not in plain_message
    assert not plain_message.endswith(f"{PROMPT_SYMBOL} ")


def test_modal_prompt_preserves_modal_body_when_agent_status_is_tall(monkeypatch) -> None:
    width = 72
    rows = 12
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._mode = PromptMode.AGENT
    prompt_session._model_name = None
    prompt_session._status_provider = lambda: StatusSnapshot(context_usage=0.0)
    prompt_session._background_task_count_provider = None
    prompt_session._thinking = False

    class _TallStatus(_DummyRunningPrompt):
        def render_agent_status(self, columns: int) -> str:
            del columns
            return "\n".join(f"old output {i}" for i in range(20))

    class _Modal(_DummyRunningPrompt):
        def render_running_prompt_body(self, columns: int) -> str:
            del columns
            return "approval body\n[1] Approve\n[2] Reject"

    prompt_session._running_prompt_delegate = _TallStatus()
    prompt_session._modal_delegates = [_Modal()]

    class _DummyOutput:
        @staticmethod
        def get_size():
            return SimpleNamespace(columns=width, rows=rows)

    dummy_app = SimpleNamespace(output=_DummyOutput())
    monkeypatch.setattr(shell_prompt, "get_app_or_none", lambda: dummy_app)

    rendered_message = prompt_session._render_agent_prompt_message()
    plain_message = "".join(fragment[1] for fragment in rendered_message)

    assert "approval body" in plain_message
    assert "[1] Approve" in plain_message
    assert "[2] Reject" in plain_message
    assert "earlier output hidden · Ctrl+O expand" in plain_message
    assert f"\n{'─' * width}\n" not in plain_message


def test_modal_prompt_hides_shell_prompt_label(monkeypatch) -> None:
    width = 72
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._mode = PromptMode.SHELL
    prompt_session._model_name = None
    prompt_session._running_prompt_delegate = None
    prompt_session._modal_delegates = [_DummyRunningPrompt()]
    prompt_session._status_provider = lambda: StatusSnapshot(context_usage=0.0)
    prompt_session._thinking = False

    class _DummyOutput:
        @staticmethod
        def get_size():
            return SimpleNamespace(columns=width)

    dummy_app = SimpleNamespace(output=_DummyOutput())
    monkeypatch.setattr(shell_prompt, "get_app_or_none", lambda: dummy_app)

    rendered_message = prompt_session._render_shell_prompt_message()
    plain_message = "".join(fragment[1] for fragment in rendered_message)

    assert plain_message == f"live view ({width})\n"
    assert "$ " not in plain_message


def test_modal_prompt_hides_input_buffer_when_text_input_is_not_allowed() -> None:
    prompt_session = CustomPromptSession(
        status_provider=lambda: StatusSnapshot(context_usage=0.0),
        model_capabilities=set(),
        model_name=None,
        thinking=False,
        agent_mode_slash_commands=[],
        shell_mode_slash_commands=[],
    )

    modal = _DummyReadOnlyModal()
    prompt_session.attach_modal(modal)

    assert prompt_session._prompt_buffer_container is not None
    assert prompt_session._should_render_input_buffer() is False


def test_prompt_buffer_window_can_grow_to_five_visible_rows() -> None:
    prompt_session = CustomPromptSession(
        status_provider=lambda: StatusSnapshot(context_usage=0.0),
        model_capabilities=set(),
        model_name=None,
        thinking=False,
        agent_mode_slash_commands=[],
        shell_mode_slash_commands=[],
    )

    assert prompt_session._prompt_buffer_container is not None
    buffer_window = prompt_session._prompt_buffer_container.content
    assert isinstance(buffer_window, shell_prompt.Window)

    assert buffer_window.dont_extend_height() is True
    height = cast(shell_prompt.Dimension, buffer_window.height)
    assert height.min == 1
    assert height.max == 5
    style = buffer_window.style
    assert callable(style)
    assert cast(Callable[[], str], style)() == prompt_session._thinking_input_style()


@pytest.mark.asyncio
async def test_prompt_buffer_expands_for_long_pasted_prompt() -> None:
    prompt_session = CustomPromptSession(
        status_provider=lambda: StatusSnapshot(context_usage=0.0),
        model_capabilities=set(),
        model_name=None,
        thinking=False,
        agent_mode_slash_commands=[],
        shell_mode_slash_commands=[],
    )

    assert prompt_session._prompt_buffer_container is not None
    buffer_window = prompt_session._prompt_buffer_container.content
    assert isinstance(buffer_window, shell_prompt.Window)

    prompt_session._suppress_auto_completion = True
    prompt_session._session.default_buffer.text = (
        "Review this codebase as a senior product designer and front-end UI auditor. "
        "Identify all UI improvements that would make the product more polished, modern, "
        "usable, and consistent. Inspect pages, components, styles, and user flows. "
        "Return a full categorized audit covering visual design, spacing, typography, "
        "colors, accessibility, responsiveness, navigation, component reuse, and "
        "interaction states. For each recommendation, provide the exact issue, why it "
        "matters, the suggested improvement, priority, and effort. Separate the output into:\n\n"
        "Quick wins\n\nModerate improvements\n\nHigh-impact strategic UI changes"
    )

    await asyncio.sleep(0)
    preferred_height = buffer_window.preferred_height(width=92, max_available_height=20)

    assert preferred_height.preferred == 5
    assert preferred_height.max == 5


def test_slash_menu_layout_sits_below_compact_agent_input() -> None:
    prompt_session = CustomPromptSession(
        status_provider=lambda: StatusSnapshot(context_usage=0.0),
        model_capabilities=set(),
        model_name=None,
        thinking=False,
        agent_mode_slash_commands=[],
        shell_mode_slash_commands=[],
    )

    root = prompt_session._session.layout.container
    assert isinstance(root, shell_prompt.HSplit)
    assert prompt_session._prompt_buffer_container is not None
    assert prompt_session._slash_menu_control is not None

    children = list(root.children)
    input_index = next(
        index
        for index, child in enumerate(children)
        if shell_prompt._container_contains(child, prompt_session._prompt_buffer_container)
    )
    slash_index = next(
        index
        for index, child in enumerate(children)
        if shell_prompt._container_contains(child, prompt_session._slash_menu_control)
    )

    assert slash_index == input_index + 1


def _dummy_slash_func(*_args: Any, **_kwargs: Any) -> None:
    return None


def test_slash_completer_uses_prefix_order_and_canonical_insertions() -> None:
    completer = SlashCommandCompleter(
        [
            SlashCommand(
                name="compact",
                description="Compact the conversation",
                func=_dummy_slash_func,
                aliases=[],
            ),
            SlashCommand(
                name="model",
                description="Choose model",
                func=_dummy_slash_func,
                aliases=["mo"],
            ),
            SlashCommand(
                name="memories",
                description="Manage memories",
                func=_dummy_slash_func,
                aliases=[],
            ),
            SlashCommand(
                name="action",
                description="Run an action",
                func=_dummy_slash_func,
                aliases=[],
            ),
        ]
    )

    mo = list(
        completer.get_completions(
            shell_prompt.Document(text="/mo", cursor_position=3),
            cast(Any, None),
        )
    )
    assert [completion.text for completion in mo] == ["/model"]

    ac = list(
        completer.get_completions(
            shell_prompt.Document(text="/ac", cursor_position=3),
            cast(Any, None),
        )
    )
    assert [completion.text for completion in ac] == ["/action"]


def test_slash_menu_highlights_typed_command_prefix(monkeypatch: Any) -> None:
    completions = [
        Completion(
            text="/model",
            start_position=-3,
            display="/model",
            display_meta="Choose what model and reasoning effort to use",
        )
    ]
    current_buffer = SimpleNamespace(
        document=shell_prompt.Document(text="/mo", cursor_position=3),
        complete_state=SimpleNamespace(completions=completions, complete_index=None),
    )
    monkeypatch.setattr(
        shell_prompt,
        "get_app_or_none",
        lambda: SimpleNamespace(current_buffer=current_buffer),
    )

    content = SlashCommandMenuControl(left_padding=lambda: 0).create_content(width=60, height=5)
    # Line 0 is the blank gap row; the first command renders on line 1.
    line = content.get_line(1)

    assert "".join(text for _, text, *_ in line).lstrip().startswith("❯ /model")
    highlighted = [(style, text) for style, text, *_ in line if "command.match" in style]
    assert highlighted[:2] == [
        ("class:slash-completion-menu.command.match.current", "m"),
        ("class:slash-completion-menu.command.match.current", "o"),
    ]


def test_bottom_toolbar_hides_status_while_slash_menu_is_active(monkeypatch: Any) -> None:
    completions = [
        Completion(
            text="/add-dir",
            start_position=0,
            display="/add-dir",
            display_meta="Add a directory to the workspace",
        )
    ]
    complete_state = SimpleNamespace(completions=completions, complete_index=0)
    default_buffer = SimpleNamespace(
        document=shell_prompt.Document(text="/", cursor_position=1),
        complete_state=complete_state,
    )
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._session = cast(Any, SimpleNamespace(default_buffer=default_buffer))
    # _render_bottom_toolbar consults the active-mode slash completer to decide
    # whether the menu is showing; a bare instance needs both wired up.
    prompt_session._mode = PromptMode.AGENT
    prompt_session._agent_slash_completer = cast(
        Any, SimpleNamespace(completion_active=lambda _document: True)
    )

    monkeypatch.setattr(shell_prompt, "get_app_or_none", lambda: object())

    assert prompt_session._render_bottom_toolbar() == []


def test_modal_prompt_keeps_input_buffer_when_text_input_is_allowed() -> None:
    prompt_session = CustomPromptSession(
        status_provider=lambda: StatusSnapshot(context_usage=0.0),
        model_capabilities=set(),
        model_name=None,
        thinking=False,
        agent_mode_slash_commands=[],
        shell_mode_slash_commands=[],
    )

    modal = _DummyRunningPrompt()
    prompt_session.attach_modal(modal)

    assert prompt_session._prompt_buffer_container is not None
    assert prompt_session._should_render_input_buffer() is True


def test_recap_slash_exact_suggestion_reflects_recaps_state() -> None:
    command = SlashCommand(
        name="recap",
        description="Recap sessions",
        func=_dummy_slash_func,
        aliases=[],
    )
    recaps_enabled = True
    prompt_session = CustomPromptSession(
        status_provider=lambda: StatusSnapshot(context_usage=0.0),
        model_capabilities=set(),
        model_name=None,
        thinking=False,
        agent_mode_slash_commands=[command],
        shell_mode_slash_commands=[],
        turn_recaps_provider=lambda: recaps_enabled,
    )
    document = shell_prompt.Document(text="/recap", cursor_position=len("/recap"))

    suggestion = prompt_session._slash_auto_suggest.get_suggestion(
        prompt_session._session.default_buffer,
        document,
    )
    assert suggestion is not None
    assert suggestion.text == " off"

    recaps_enabled = False
    suggestion = prompt_session._slash_auto_suggest.get_suggestion(
        prompt_session._session.default_buffer,
        document,
    )
    assert suggestion is not None
    assert suggestion.text == " on"


def test_modal_prompt_suspends_and_restores_existing_draft_when_input_is_hidden() -> None:
    prompt_session = CustomPromptSession(
        status_provider=lambda: StatusSnapshot(context_usage=0.0),
        model_capabilities=set(),
        model_name=None,
        thinking=False,
        agent_mode_slash_commands=[],
        shell_mode_slash_commands=[],
    )
    prompt_session._session.default_buffer.start_completion = lambda *args, **kwargs: None  # type: ignore[method-assign]
    prompt_session._session.default_buffer.validate_while_typing = lambda: False  # type: ignore[method-assign]
    prompt_session._session.default_buffer.document = shell_prompt.Document(
        text="keep this draft",
        cursor_position=len("keep this draft"),
    )

    modal = _DummyReadOnlyModal()
    prompt_session.attach_modal(modal)

    assert prompt_session._session.default_buffer.text == ""
    assert prompt_session._suspended_buffer_document is not None

    prompt_session.detach_modal(modal)

    assert prompt_session._session.default_buffer.text == "keep this draft"
    assert prompt_session._suspended_buffer_document is None


def test_prompt_rule_keeps_rightmost_column_clear() -> None:
    assert shell_prompt._prompt_rule(0) == ""
    assert shell_prompt._prompt_rule(1) == ""
    assert shell_prompt._prompt_rule(8) == "─" * 7


def test_idle_agent_prompt_uses_same_bottom_input_layout(monkeypatch: Any) -> None:
    width = 64
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._running_prompt_delegate = None
    prompt_session._turn_starting = False
    prompt_session._status_provider = lambda: StatusSnapshot(context_usage=0.0)
    prompt_session._thinking = False

    class _DummyOutput:
        @staticmethod
        def get_size() -> Any:
            return SimpleNamespace(columns=width)

    monkeypatch.setattr(
        shell_prompt, "get_app_or_none", lambda: SimpleNamespace(output=_DummyOutput())
    )

    rendered_message = prompt_session._render_agent_prompt_message()
    plain_message = "".join(fragment[1] for fragment in rendered_message)
    # Keep the rightmost terminal column clear. Full-width prompt-toolkit chrome
    # is prone to resize artifacts in non-fullscreen prompts.
    assert plain_message == f"{'─' * (width - 1)}\n  ❯ "
    assert "input" not in plain_message


# ── Session mode / erase_when_done behavior ───────────────────────────────────


def test_apply_mode_syncs_erase_when_done_with_current_mode() -> None:
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._session = cast(
        Any,
        SimpleNamespace(
            app=SimpleNamespace(erase_when_done=False, full_screen=True),
            default_buffer=SimpleNamespace(completer=None),
        ),
    )
    prompt_session._agent_mode_completer = cast(Any, object())
    prompt_session._shell_mode_completer = cast(Any, object())
    prompt_session._mode = PromptMode.AGENT

    prompt_session._apply_mode()

    assert prompt_session._session.default_buffer.completer is prompt_session._agent_mode_completer
    assert prompt_session._session.app.erase_when_done is True
    assert prompt_session._session.app.full_screen is True

    prompt_session._mode = PromptMode.SHELL
    prompt_session._apply_mode()

    assert prompt_session._session.default_buffer.completer is prompt_session._shell_mode_completer
    assert prompt_session._session.app.erase_when_done is False
    assert prompt_session._session.app.full_screen is True


def test_attach_running_prompt_enables_erase_when_done_and_detach_restores_state() -> None:
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._mode = PromptMode.SHELL
    prompt_session._running_prompt_delegate = None
    prompt_session._running_prompt_previous_mode = None
    prompt_session._turn_starting = False
    app = SimpleNamespace(
        erase_when_done=False,
        full_screen=True,
        renderer=SimpleNamespace(full_screen=False),
    )
    prompt_session._session = cast(
        Any,
        SimpleNamespace(app=app, default_buffer=SimpleNamespace(completer=None)),
    )
    prompt_session._agent_mode_completer = cast(Any, object())
    prompt_session._shell_mode_completer = cast(Any, object())

    delegate = _DummyRunningPrompt()
    invalidations: list[tuple[PromptMode, bool, object | None]] = []
    initial_screen_modes = (app.full_screen, app.renderer.full_screen)

    def fake_invalidate() -> None:
        invalidations.append(
            (
                prompt_session._mode,
                prompt_session._session.app.erase_when_done,
                prompt_session._running_prompt_delegate,
            )
        )

    prompt_session.invalidate = fake_invalidate

    prompt_session.attach_running_prompt(delegate)

    assert prompt_session._mode == PromptMode.AGENT
    assert prompt_session._running_prompt_delegate is delegate
    assert prompt_session._session.app.erase_when_done is True
    assert (app.full_screen, app.renderer.full_screen) == initial_screen_modes

    prompt_session.detach_running_prompt(delegate)

    assert prompt_session._mode == PromptMode.SHELL
    assert prompt_session._running_prompt_delegate is None
    assert prompt_session._session.app.erase_when_done is False
    assert (app.full_screen, app.renderer.full_screen) == initial_screen_modes
    assert invalidations == [
        (PromptMode.AGENT, True, delegate),
        (PromptMode.SHELL, False, None),
    ]


# ── Prompt async contract ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("running_prompt", [_DummyRunningPrompt(), None])
async def test_prompt_once_uses_prompt_delegate_placeholder_contract(running_prompt: Any) -> None:
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._running_prompt_delegate = running_prompt
    prompt_session._tip_rotation_index = 0

    captured: list[object | None] = []

    class _DummySession:
        async def prompt_async(self, **kwargs: Any) -> str:
            captured.append(kwargs.get("placeholder"))
            return "hello"

    prompt_session._session = cast(Any, _DummySession())
    prompt_session._build_user_input = lambda command: UserInput(
        mode=PromptMode.AGENT,
        command=command,
        resolved_command=command,
        content=[],
    )

    result = await prompt_session._prompt_once(append_history=False)

    assert result.command == "hello"
    assert captured == [None]


@pytest.mark.asyncio
async def test_prompt_next_skips_history_for_running_submission() -> None:
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._running_prompt_delegate = _DummyRunningPrompt()
    prompt_session._tip_rotation_index = 0
    prompt_session._append_history_entry = lambda text: (_ for _ in ()).throw(
        AssertionError("running submissions must not append history")
    )
    prompt_session._build_user_input = lambda command: UserInput(
        mode=PromptMode.AGENT,
        command=command,
        resolved_command=command,
        content=[],
    )

    class _DummySession:
        async def prompt_async(self, **kwargs: Any) -> str:
            return "follow-up"

    prompt_session._session = cast(Any, _DummySession())

    result = await prompt_session.prompt_next()

    assert result.command == "follow-up"
    assert prompt_session.last_submission_was_running is True


@pytest.mark.asyncio
async def test_prompt_next_does_not_mark_submission_as_running_when_delegate_releases_prompt() -> (
    None
):
    prompt_session = object.__new__(CustomPromptSession)

    class _FinishedRunningPrompt(_DummyRunningPrompt):
        def running_prompt_accepts_submission(self) -> bool:
            return False

    prompt_session._running_prompt_delegate = _FinishedRunningPrompt()
    prompt_session._tip_rotation_index = 0
    prompt_session._append_history_entry = lambda _text: None  # type: ignore[assignment]
    prompt_session._build_user_input = lambda command: UserInput(
        mode=PromptMode.AGENT,
        command=command,
        resolved_command=command,
        content=[],
    )

    class _DummySession:
        async def prompt_async(self, **kwargs):
            return "follow-up"

    prompt_session._session = cast(Any, _DummySession())

    result = await prompt_session.prompt_next()

    assert result.command == "follow-up"
    assert prompt_session.last_submission_was_running is False


def test_append_update_notice_appends_as_last_footer_row():
    # Called last, after the status lines: the notice becomes the final footer
    # row (below the status/clock line), fully clear of the input box. It prepends
    # its own newline and adds no trailing one, so it never leaves a blank bottom row.
    fragments: list[tuple[str, str]] = [("status", "◇ model"), ("", " ctx")]
    fake = SimpleNamespace(_update_notice_provider=lambda: "↑ Update available — v9.9.9 · /update")
    CustomPromptSession._append_update_notice(cast(Any, fake), fragments, 80)
    # Existing footer fragments are untouched; the notice is appended at the end.
    assert fragments[0] == ("status", "◇ model")
    assert fragments[1] == ("", " ctx")
    assert fragments[2] == ("", "\n")  # leading newline starts a fresh row
    assert "Update available" in fragments[3][1]
    assert "v9.9.9" in fragments[3][1]
    assert "bold" in fragments[3][0]
    assert len(fragments) == 4  # no trailing newline → no blank row at the bottom


def test_append_update_notice_noop_when_no_update():
    fragments: list[tuple[str, str]] = [("sep", "────")]
    fake = SimpleNamespace(_update_notice_provider=lambda: None)
    CustomPromptSession._append_update_notice(cast(Any, fake), fragments, 80)
    assert fragments == [("sep", "────")]


def test_append_update_notice_noop_when_no_provider():
    fragments: list[tuple[str, str]] = [("sep", "x")]
    fake = SimpleNamespace(_update_notice_provider=None)
    CustomPromptSession._append_update_notice(cast(Any, fake), fragments, 80)
    assert fragments == [("sep", "x")]
