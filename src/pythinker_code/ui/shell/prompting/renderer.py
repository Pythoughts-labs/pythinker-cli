"""Terminal-row budgeting for prompt scenes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSceneBudget:
    """Rows available to the dynamic preamble after higher-priority surfaces."""

    terminal_rows: int
    input_rows: int = 2
    footer_rows: int = 3
    menu_rows: int = 0
    safety_rows: int = 0

    @property
    def preamble_rows(self) -> int:
        reserved = (
            max(0, self.input_rows)
            + max(0, self.footer_rows)
            + max(0, self.menu_rows)
            + max(0, self.safety_rows)
        )
        return max(0, self.terminal_rows - reserved)


@dataclass(frozen=True)
class PromptSceneAllocation:
    """Row allowances in the renderer's deterministic overflow priority order."""

    modal_rows: int = 0
    input_rows: int = 0
    footer_rows: int = 0
    pinned_rows: int = 0
    separator_rows: int = 0
    body_rows: int = 0
    status_rows: int = 0
    shortcut_rows: int = 0

    @property
    def prompt_rows(self) -> int:
        return (
            self.modal_rows
            + self.input_rows
            + self.pinned_rows
            + self.separator_rows
            + self.body_rows
            + self.status_rows
            + self.shortcut_rows
        )


def allocate_prompt_scene_rows(
    budget: PromptSceneBudget,
    *,
    modal_rows: int = 0,
    input_rows: int = 0,
    footer_rows: int = 0,
    pinned_rows: int = 0,
    separator_rows: int = 0,
    body_rows: int = 0,
    status_rows: int = 0,
    shortcut_rows: int = 0,
) -> PromptSceneAllocation:
    """Allocate rows by surface priority without ever exceeding the terminal."""
    remaining = max(
        0,
        budget.terminal_rows - max(0, budget.menu_rows) - max(0, budget.safety_rows),
    )

    def take(requested: int) -> int:
        nonlocal remaining
        allocated = min(remaining, max(0, requested))
        remaining -= allocated
        return allocated

    return PromptSceneAllocation(
        modal_rows=take(modal_rows),
        input_rows=take(input_rows),
        footer_rows=take(footer_rows),
        pinned_rows=take(pinned_rows),
        separator_rows=take(separator_rows),
        body_rows=take(body_rows),
        status_rows=take(status_rows),
        shortcut_rows=take(shortcut_rows),
    )
