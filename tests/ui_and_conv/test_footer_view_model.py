"""Parity tests for the immutable prompt footer view model."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, NoReturn

import pytest
from prompt_toolkit.formatted_text import FormattedText

from pythinker_code.config import StatusLineConfig
from pythinker_code.ui.shell.prompt import CustomPromptSession, PromptMode, _display_width
from pythinker_code.ui.shell.prompting.footer import (
    FooterViewModel,
    background_task_summary,
    select_footer_content,
)
from pythinker_code.ui.shell.prompting.toasts import ToastSnapshot
from pythinker_code.ui.shell.statusline import StatusFlags, StatusLineContext
from pythinker_code.ui.theme import registry as theme_registry
from pythinker_code.ui.theme.capabilities import TerminalCapabilities


def _status(columns: int, *, ascii_only: bool = False) -> StatusLineContext:
    return StatusLineContext(
        columns=columns,
        working=True,
        frame=0,
        model_name="test-model",
        provider_label=None,
        effort=None,
        rate_in=None,
        rate_out=None,
        session_cost_usd=0.0,
        cost_budget_usd=None,
        context_tokens=0,
        max_context_tokens=0,
        elapsed_s=61.0,
        clock="12:34",
        cwd="~/project",
        git=None,
        diff_added=None,
        diff_removed=None,
        flags=StatusFlags(yolo=False, auto=False, plan=False),
        limits=None,
        ascii_only=ascii_only,
        style="plain" if ascii_only else "fancy",
        bar_width=8,
        background_bash=1,
    )


def _toast(message: str = "toast-only") -> ToastSnapshot:
    return ToastSnapshot(
        message=message,
        position="left",
        style="bold",
        topic=None,
        expires_at=float("inf"),
    )


def _model(
    columns: int,
    *,
    command: str = "",
    extensions: tuple[tuple[str, str], ...] = (),
    background: str = "",
    toast: ToastSnapshot | None = None,
    update_notice: str | None = "Update available",
    ascii_only: bool = False,
) -> FooterViewModel:
    status = _status(columns, ascii_only=ascii_only)
    status = replace(
        status,
        working=bool(background),
        background_bash=1 if background else 0,
    )
    return FooterViewModel(
        status=status,
        command_line=command,
        extension_statuses=extensions,
        background_summary=background,
        toast=toast,
        update_notice=update_notice,
    )


def _session() -> Any:
    session = object.__new__(CustomPromptSession)
    session._mode = PromptMode.AGENT
    session._model_name = "test-model"
    session._tips = []
    session._tip_rotation_index = 0
    session._last_tip_rotate_time = float("inf")
    session._prompt_footer_row_budget = 20
    session._statusline_cfg = StatusLineConfig(segments=["command"])

    def unexpected_provider() -> NoReturn:
        raise AssertionError("render adapter sampled a live provider")

    session._status_provider = unexpected_provider
    session._background_task_count_provider = unexpected_provider
    session._update_notice_provider = unexpected_provider
    return session


def _text(fragments: FormattedText) -> str:
    return "".join(fragment[1] for fragment in fragments)


@pytest.mark.parametrize("width", [20, 40, 80, 120])
@pytest.mark.parametrize(
    ("model_kwargs", "expected_kind"),
    [
        (
            {
                "command": "custom-command",
                "extensions": (("long-extension", "x" * 100),),
                "background": "background-only",
                "toast": _toast(),
            },
            "command",
        ),
        (
            {
                "extensions": (("long-extension", "x" * 100),),
                "background": "background-only",
                "toast": _toast(),
            },
            "extension",
        ),
        ({"background": background_task_summary(bash=1, agent=2), "toast": _toast()}, "background"),
        ({"toast": _toast()}, "toast"),
    ],
)
def test_legacy_and_card_adapters_share_left_policy_and_width_safety(
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    model_kwargs: dict[str, object],
    expected_kind: str,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    session = _session()
    model = _model(width, ascii_only=True, **model_kwargs)  # type: ignore[arg-type]

    selected = select_footer_content(model)
    assert selected is not None and selected.kind == expected_kind

    legacy = session._render_legacy_bottom_toolbar(model)
    card = session._render_card_bottom_toolbar(model)
    assert select_footer_content(model) == selected

    for rendered in (legacy, card):
        rows = _text(rendered).splitlines()
        # Production truncation reserves terminal columns by display width, so the
        # width guard must measure display width, not code-point count.
        assert all(_display_width(row) <= width for row in rows)
        assert "Update available"[: max(0, width - 1)] in rows[-1]
        assert "…" not in _text(rendered)

    # Lower-precedence policy fields cannot leak into either adapter's left row.
    if expected_kind == "command":
        assert "long-extension:" not in _text(legacy)
        assert "long-extension:" not in _text(card)
        assert "background-only" not in _text(card)
        assert "toast-only" not in _text(card)
    elif expected_kind == "extension":
        assert "background-only" not in _text(card)
        assert "toast-only" not in _text(card)
    elif expected_kind == "background":
        assert "toast-only" not in _text(legacy)
        assert "toast-only" not in _text(card)


def test_footer_rows_respect_display_width_with_wide_and_combining_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wide/combining glyphs must be width-accounted so no rendered row overflows.

    ``len()`` would mis-measure both directions — a CJK glyph occupies two columns
    but counts as one code point, while a combining mark occupies zero columns but
    counts as one — so only a display-width guard catches terminal overflow here.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    session = _session()
    wide_command = "全角指令" + "é" * 3
    for width in (40, 80, 120):
        model = _model(width, command=wide_command, ascii_only=False)
        assert select_footer_content(model) is not None
        legacy = session._render_legacy_bottom_toolbar(model)
        card = session._render_card_bottom_toolbar(model)
        for rendered in (legacy, card):
            rows = _text(rendered).splitlines()
            assert all(_display_width(row) <= width for row in rows)
            assert "Update available"[: max(0, width - 1)] in rows[-1]


def test_left_and_right_toasts_both_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a left toast must not drop a simultaneously active right toast."""
    monkeypatch.setenv("NO_COLOR", "1")
    session = _session()
    left = ToastSnapshot(
        message="left-toast",
        position="left",
        style="bold",
        topic=None,
        expires_at=float("inf"),
    )
    right = ToastSnapshot(
        message="right-toast",
        position="right",
        style="bold",
        topic=None,
        expires_at=float("inf"),
    )
    model = FooterViewModel(
        status=_status(120, ascii_only=True),
        command_line="",
        extension_statuses=(),
        background_summary="",
        toast=left,
        update_notice=None,
        toast_right=right,
    )

    rendered = _text(session._render_legacy_bottom_toolbar(model))
    assert "left-toast" in rendered
    assert "right-toast" in rendered


def test_footer_view_model_is_immutable_and_width_specific() -> None:
    model = _model(80, command="cached")
    narrower = replace(model, status=replace(model.status, columns=40))

    assert model.status.columns == 80
    assert narrower.status.columns == 40
    with pytest.raises(AttributeError):
        model.command_line = "changed"  # type: ignore[misc]


def test_footer_view_model_is_built_once_per_prompt_frame() -> None:
    session = _session()
    model = _model(80, command="cached")
    calls = 0

    def build(columns: int) -> FooterViewModel:
        nonlocal calls
        calls += 1
        assert columns == 80
        return model

    session._current_prompt_frame = object()
    session._current_footer_view_model = None
    session._build_footer_view_model = build

    assert session._footer_view_model_for_render(80) is model
    assert session._footer_view_model_for_render(80) is model
    assert calls == 1


def test_resolver_cache_includes_terminal_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    first_caps = TerminalCapabilities(
        color_enabled=True,
        truecolor=True,
        color_256=True,
        dumb=False,
    )
    second_caps = TerminalCapabilities(
        color_enabled=False,
        truecolor=False,
        color_256=False,
        dumb=True,
    )
    assert isinstance(hash(first_caps), int)
    assert first_caps != second_caps

    theme_registry._resolver_for.cache_clear()
    monkeypatch.setattr(theme_registry, "get_terminal_capabilities", lambda: first_caps)
    first = theme_registry.get_resolver("dark")
    assert theme_registry.get_resolver("dark") is first

    monkeypatch.setattr(theme_registry, "get_terminal_capabilities", lambda: second_caps)
    second = theme_registry.get_resolver("dark")
    assert second is not first
    theme_registry._resolver_for.cache_clear()
