from __future__ import annotations

from types import SimpleNamespace
from typing import Literal

import pytest
from prompt_toolkit.formatted_text import FormattedText

from pythinker_code.ui.shell import prompt as shell_prompt
from pythinker_code.ui.shell.prompt import CustomPromptSession, PromptMode
from pythinker_code.ui.shell.prompting import (
    FrozenFragments,
    PromptFrame,
    PromptSceneBudget,
    allocate_prompt_scene_rows,
    freeze_fragments,
)

Scene = Literal[
    "body",
    "body_pinned",
    "tall_pinned",
    "modal",
    "shortcuts",
    "update",
    "status_body",
]


def _text(value: str) -> FrozenFragments:
    return freeze_fragments(FormattedText([("", value)]))


def _session_for_scene(
    scene: Scene,
    *,
    width: int,
    height: int,
    card_style: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> CustomPromptSession:
    session = object.__new__(CustomPromptSession)
    session._mode = PromptMode.AGENT
    session._model_name = None
    session._model_capabilities = set()
    session._thinking = False
    session._thinking_effort = "off"
    session._shortcut_help_open = scene == "shortcuts"
    session._update_notice_provider = (lambda: "Update ready") if scene == "update" else None

    body = "live row 1\nlive row 2"
    status = ""
    pinned = ""
    modal = scene == "modal"
    if scene == "body_pinned":
        pinned = "Working…"
    elif scene == "tall_pinned":
        pinned = "\n".join(f"pinned {index}" for index in range(20))
    elif scene == "modal":
        body = "\n".join(f"modal control {index}" for index in range(20))
        status = "\n".join(f"old status {index}" for index in range(10))
    elif scene == "status_body":
        status = "\n".join(f"old status {index}" for index in range(10))

    session._current_prompt_frame = PromptFrame(
        columns=width,
        terminal_rows=height,
        body_rows=PromptSceneBudget(
            terminal_rows=height,
            input_rows=0 if modal else 2,
        ).preamble_rows,
        agent_status=_text(status),
        interactive_body=_text(body),
        pinned_tail=_text(pinned),
        placeholder=(),
        input_card_hidden=False,
        input_chrome_hidden=False,
        modal_active=modal,
        running_prompt_active=True,
    )
    monkeypatch.setattr(shell_prompt, "is_card_style", lambda: card_style)
    return session


@pytest.mark.parametrize("height", range(1, 13))
@pytest.mark.parametrize("width", (20, 40, 80, 120))
@pytest.mark.parametrize(
    "scene",
    ("body", "body_pinned", "tall_pinned", "modal", "shortcuts", "update", "status_body"),
)
@pytest.mark.parametrize("card_style", (False, True), ids=("pythinker", "card"))
def test_prompt_scene_never_exceeds_terminal_height(
    height: int,
    width: int,
    scene: Scene,
    card_style: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session_for_scene(
        scene,
        width=width,
        height=height,
        card_style=card_style,
        monkeypatch=monkeypatch,
    )

    message = session._render_agent_prompt_message()
    message_rows = len(shell_prompt._formatted_text_display_rows(message, width)) if message else 0
    footer = FormattedText(
        [("", "\n".join(f"footer {index}" for index in range(4 if scene == "update" else 3)))]
    )
    monkeypatch.setattr(
        shell_prompt,
        "get_app_or_none",
        lambda: SimpleNamespace(
            output=SimpleNamespace(get_size=lambda: SimpleNamespace(columns=width, rows=height))
        ),
    )
    rendered_footer = session._fit_toolbar_to_terminal(footer, width)
    footer_rows = (
        len(shell_prompt._formatted_text_display_rows(rendered_footer, width))
        if rendered_footer
        else 0
    )

    assert message_rows + footer_rows <= height


def test_one_row_scene_keeps_modal_then_input_before_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modal = _session_for_scene(
        "modal", width=40, height=1, card_style=False, monkeypatch=monkeypatch
    )
    modal_text = "".join(fragment[1] for fragment in modal._render_agent_prompt_message())
    prompt = _session_for_scene(
        "body", width=40, height=1, card_style=False, monkeypatch=monkeypatch
    )
    prompt_text = "".join(fragment[1] for fragment in prompt._render_agent_prompt_message())

    assert "modal control 19" in modal_text
    assert "earlier output hidden" not in modal_text
    assert shell_prompt.PROMPT_SYMBOL_AGENT_INPUT in prompt_text
    assert modal._prompt_footer_row_budget == 0
    assert prompt._prompt_footer_row_budget == 0


def test_two_row_modal_uses_hint_then_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session_for_scene(
        "modal",
        width=40,
        height=2,
        card_style=False,
        monkeypatch=monkeypatch,
    )

    plain = "".join(fragment[1] for fragment in session._render_agent_prompt_message())

    assert "earlier output hidden" in plain
    assert "modal control 19" in plain


def test_rendered_overflow_keeps_pinned_before_live_and_old_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned = _session_for_scene(
        "body_pinned",
        width=40,
        height=6,
        card_style=False,
        monkeypatch=monkeypatch,
    )
    pinned_plain = "".join(fragment[1] for fragment in pinned._render_agent_prompt_message())
    status = _session_for_scene(
        "status_body",
        width=40,
        height=6,
        card_style=False,
        monkeypatch=monkeypatch,
    )
    status_plain = "".join(fragment[1] for fragment in status._render_agent_prompt_message())

    assert "Working…" in pinned_plain
    assert "live row" not in pinned_plain
    assert shell_prompt.PROMPT_SYMBOL_AGENT_INPUT in pinned_plain
    assert "live row 2" in status_plain
    assert "old status" not in status_plain


@pytest.mark.parametrize("card_style", (False, True), ids=("pythinker", "card"))
def test_pinned_separator_is_budgeted_before_live_body(
    card_style: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    without_body_height = 7
    with_body_height = without_body_height + 1
    without_body = _session_for_scene(
        "body_pinned",
        width=40,
        height=without_body_height,
        card_style=card_style,
        monkeypatch=monkeypatch,
    )
    with_body = _session_for_scene(
        "body_pinned",
        width=40,
        height=with_body_height,
        card_style=card_style,
        monkeypatch=monkeypatch,
    )

    without_body_plain = "".join(
        fragment[1] for fragment in without_body._render_agent_prompt_message()
    )
    with_body_rendered = with_body._render_agent_prompt_message()
    with_body_plain = "".join(fragment[1] for fragment in with_body_rendered)

    assert "Working…" in without_body_plain
    assert "live row" not in without_body_plain
    assert "live row 2\n\nWorking…" in with_body_plain
    assert len(shell_prompt._formatted_text_display_rows(with_body_rendered, 40)) == (
        5 if card_style else 4
    )


@pytest.mark.parametrize("card_style", (False, True), ids=("pythinker", "card"))
def test_tall_pinned_tail_clips_from_the_front(
    card_style: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session_for_scene(
        "tall_pinned",
        width=40,
        height=7,
        card_style=card_style,
        monkeypatch=monkeypatch,
    )

    plain = "".join(fragment[1] for fragment in session._render_agent_prompt_message())

    assert "pinned 19" in plain
    assert "pinned 18" in plain
    assert "pinned 17" not in plain
    assert "live row" not in plain


@pytest.mark.parametrize("card_style", (False, True), ids=("pythinker", "card"))
def test_footer_and_live_body_precede_shortcut_help(
    card_style: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden_height = 6 if not card_style else 7
    visible_height = hidden_height + 1
    hidden = _session_for_scene(
        "shortcuts",
        width=40,
        height=hidden_height,
        card_style=card_style,
        monkeypatch=monkeypatch,
    )
    visible = _session_for_scene(
        "shortcuts",
        width=40,
        height=visible_height,
        card_style=card_style,
        monkeypatch=monkeypatch,
    )

    hidden_plain = "".join(fragment[1] for fragment in hidden._render_agent_prompt_message())
    visible_plain = "".join(fragment[1] for fragment in visible._render_agent_prompt_message())

    assert "live row 1" in hidden_plain
    assert "close shortcuts" not in hidden_plain
    assert "live row 1" in visible_plain
    assert "╰" in visible_plain


@pytest.mark.parametrize("card_style", (False, True), ids=("pythinker", "card"))
def test_update_footer_precedes_live_body(
    card_style: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden_height = 5 if not card_style else 6
    visible_height = hidden_height + 1
    hidden = _session_for_scene(
        "update",
        width=40,
        height=hidden_height,
        card_style=card_style,
        monkeypatch=monkeypatch,
    )
    visible = _session_for_scene(
        "update",
        width=40,
        height=visible_height,
        card_style=card_style,
        monkeypatch=monkeypatch,
    )

    hidden_plain = "".join(fragment[1] for fragment in hidden._render_agent_prompt_message())
    visible_plain = "".join(fragment[1] for fragment in visible._render_agent_prompt_message())

    assert "live row" not in hidden_plain
    assert hidden._prompt_footer_row_budget == 4
    assert "live row 2" in visible_plain
    assert visible._prompt_footer_row_budget == 4


@pytest.mark.parametrize(
    ("height", "expected"),
    (
        (1, (1, 0, 0, 0, 0, 0, 0)),
        (2, (2, 0, 0, 0, 0, 0, 0)),
        (4, (2, 2, 0, 0, 0, 0, 0)),
        (5, (2, 3, 0, 0, 0, 0, 0)),
        (6, (2, 3, 1, 0, 0, 0, 0)),
        (7, (2, 3, 1, 1, 0, 0, 0)),
        (8, (2, 3, 1, 1, 1, 0, 0)),
        (9, (2, 3, 1, 1, 1, 1, 0)),
    ),
)
def test_scene_allocator_follows_overflow_priority(
    height: int,
    expected: tuple[int, int, int, int, int, int, int],
) -> None:
    allocation = allocate_prompt_scene_rows(
        PromptSceneBudget(terminal_rows=height),
        input_rows=2,
        footer_rows=3,
        pinned_rows=1,
        body_rows=1,
        status_rows=1,
        shortcut_rows=1,
    )

    assert (
        allocation.input_rows,
        allocation.footer_rows,
        allocation.pinned_rows,
        allocation.body_rows,
        allocation.status_rows,
        allocation.shortcut_rows,
        allocation.modal_rows,
    ) == expected


def test_modal_consumes_rows_before_every_other_surface() -> None:
    allocation = allocate_prompt_scene_rows(
        PromptSceneBudget(terminal_rows=5),
        modal_rows=10,
        input_rows=2,
        footer_rows=3,
        pinned_rows=1,
        body_rows=1,
        status_rows=1,
        shortcut_rows=1,
    )

    assert allocation.modal_rows == 5
    assert allocation.prompt_rows == 5
    assert allocation.footer_rows == 0


def test_scene_allocator_reserves_menu_and_safety_rows() -> None:
    allocation = allocate_prompt_scene_rows(
        PromptSceneBudget(terminal_rows=5, menu_rows=2, safety_rows=1),
        input_rows=2,
        footer_rows=3,
    )

    assert allocation.input_rows == 2
    assert allocation.footer_rows == 0
    assert allocation.prompt_rows + allocation.footer_rows + 2 + 1 == 5


def test_scene_allocator_counts_pinned_separator_inside_budget() -> None:
    allocation = allocate_prompt_scene_rows(
        PromptSceneBudget(terminal_rows=8),
        input_rows=2,
        footer_rows=3,
        pinned_rows=1,
        separator_rows=1,
        body_rows=2,
    )

    assert allocation.pinned_rows == 1
    assert allocation.separator_rows == 1
    assert allocation.body_rows == 1
    assert allocation.prompt_rows + allocation.footer_rows == 8


def test_update_notice_is_sampled_once_for_prompt_and_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def update_notice() -> str | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else "unexpected second sample"

    session = _session_for_scene(
        "body", width=40, height=8, card_style=False, monkeypatch=monkeypatch
    )
    session._update_notice_provider = update_notice
    session._render_agent_prompt_message()
    footer: list[tuple[str, str]] = []

    session._append_update_notice(footer, 40)

    assert calls == 1
    assert footer == []


def test_scene_budget_reserves_higher_priority_rows_before_pinned_tail() -> None:
    budget = PromptSceneBudget(
        terminal_rows=1,
        input_rows=0,
        footer_rows=1,
        menu_rows=0,
        safety_rows=0,
    )

    assert budget.preamble_rows == 0


@pytest.mark.parametrize("terminal_rows", range(-2, 13))
def test_scene_budget_is_zero_safe(terminal_rows: int) -> None:
    budget = PromptSceneBudget(
        terminal_rows=terminal_rows,
        input_rows=2,
        footer_rows=3,
        menu_rows=6,
        safety_rows=1,
    )

    assert budget.preamble_rows == max(0, terminal_rows - 12)
