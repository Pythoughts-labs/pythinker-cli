"""Capture dynamic prompt delegates once for a prompt_toolkit render frame."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from prompt_toolkit.formatted_text import (
    AnyFormattedText,
    FormattedText,
    OneStyleAndTextTuple,
    to_formatted_text,
)

from pythinker_code.ui.shell.prompting.renderer import PromptSceneBudget

FrozenFragments = tuple[OneStyleAndTextTuple, ...]


def freeze_fragments(value: AnyFormattedText | None) -> FrozenFragments:
    """Normalize formatted text into an immutable frame-owned tuple."""
    return tuple(to_formatted_text(value or FormattedText()))


def _has_text(fragments: FrozenFragments) -> bool:
    return any(text for _, text, *_ in fragments)


def _with_newline(fragments: FrozenFragments) -> FrozenFragments:
    if not fragments or fragments[-1][1].endswith("\n"):
        return fragments
    return (*fragments, ("", "\n"))


@dataclass(frozen=True, slots=True)
class PromptFrame:
    """All dynamic values used to render one prompt frame."""

    columns: int
    terminal_rows: int
    body_rows: int
    agent_status: FrozenFragments
    interactive_body: FrozenFragments
    pinned_tail: FrozenFragments
    placeholder: FrozenFragments
    input_card_hidden: bool
    input_chrome_hidden: bool
    modal_active: bool
    running_prompt_active: bool


class PromptFrameCollector:
    """Resolve delegates and sample their render-facing methods once per frame."""

    def __init__(
        self,
        *,
        resolve_modal: Callable[[], Any],
        resolve_running: Callable[[], Any],
        render_background_status: Callable[[int], AnyFormattedText],
        render_status_block: Callable[[int], AnyFormattedText],
        input_is_empty: Callable[[], bool],
        turn_is_starting: Callable[[], bool],
    ) -> None:
        self._resolve_modal = resolve_modal
        self._resolve_running = resolve_running
        self._render_background_status = render_background_status
        self._render_status_block = render_status_block
        self._input_is_empty = input_is_empty
        self._turn_is_starting = turn_is_starting

    def capture(self, *, columns: int, terminal_rows: int) -> PromptFrame:
        """Capture every render-facing delegate value exactly once."""
        modal = self._resolve_modal()
        running = self._resolve_running()
        body_rows = PromptSceneBudget(
            terminal_rows=terminal_rows,
            input_rows=0 if modal is not None else 2,
        ).preamble_rows

        pinned_method = cast(
            Callable[[int], AnyFormattedText] | None,
            getattr(running, "render_pinned_status_tail", None),
        )
        pinned = freeze_fragments(pinned_method(columns) if callable(pinned_method) else None)

        status_method = cast(
            Callable[[int], AnyFormattedText] | None,
            getattr(running, "render_agent_status", None),
        )
        rendered_status = freeze_fragments(
            status_method(columns) if callable(status_method) else None
        )
        if _has_text(rendered_status):
            if not _has_text(pinned):
                background = freeze_fragments(self._render_background_status(columns))
                if _has_text(background):
                    rendered_status = (*_with_newline(rendered_status), *background)
            agent_status = (*_with_newline(rendered_status), ("", "\n"))
        else:
            background: FrozenFragments = (
                ()
                if _has_text(pinned)
                else freeze_fragments(self._render_background_status(columns))
            )
            status_block = freeze_fragments(self._render_status_block(columns))
            agent_status = background
            if _has_text(status_block):
                agent_status = (*_with_newline(agent_status), *status_block)

        active = modal if modal is not None else running
        body_method = cast(
            Callable[[int], AnyFormattedText] | None,
            getattr(active, "render_running_prompt_body", None),
        )
        interactive_body = freeze_fragments(body_method(columns) if callable(body_method) else None)

        placeholder_method = cast(
            Callable[[], AnyFormattedText | None] | None,
            getattr(running, "running_prompt_placeholder", None),
        )
        placeholder = freeze_fragments(
            placeholder_method() if callable(placeholder_method) else None
        )
        hide_card_method = getattr(running, "running_prompt_hide_input_card", None)
        hide_card = bool(hide_card_method()) if callable(hide_card_method) else False
        hide_chrome_method = getattr(running, "running_prompt_hide_input_card_chrome", None)
        hide_chrome = bool(hide_chrome_method()) if callable(hide_chrome_method) else False
        modal_active = modal is not None
        input_card_hidden = (
            not modal_active and (self._turn_is_starting() or hide_card) and self._input_is_empty()
        )

        return PromptFrame(
            columns=columns,
            terminal_rows=terminal_rows,
            body_rows=body_rows,
            agent_status=agent_status,
            interactive_body=interactive_body,
            pinned_tail=pinned,
            placeholder=placeholder,
            input_card_hidden=input_card_hidden,
            input_chrome_hidden=hide_chrome,
            modal_active=modal_active,
            running_prompt_active=running is not None,
        )
