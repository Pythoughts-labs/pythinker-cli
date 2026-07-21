"""Immutable constructor data and callable providers for the prompt session.

``CustomPromptSession.__init__`` keeps its keyword signature and builds these
bundles internally; they group the constructor surface into constructor data
(``PromptConfig``) and dynamic callable providers (``PromptProviders``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.formatted_text import AnyFormattedText

from pythinker_code.config import StatusLineConfig
from pythinker_code.llm import ModelCapability
from pythinker_code.soul import StatusSnapshot
from pythinker_code.utils.slashcmd import SlashCommand


@dataclass(frozen=True, slots=True)
class BgTaskCounts:
    bash: int = 0
    agent: int = 0


@dataclass(frozen=True, slots=True)
class PromptConfig:
    """Immutable constructor data for one prompt session."""

    model_capabilities: set[ModelCapability]
    model_name: str | None
    thinking: bool
    thinking_effort: str | None
    agent_mode_slash_commands: Sequence[SlashCommand[Any]]
    shell_mode_slash_commands: Sequence[SlashCommand[Any]]
    history_enabled: bool
    statusline_config: StatusLineConfig | None
    sticky_input: bool


@dataclass(frozen=True, slots=True)
class PromptProviders:
    """Callable providers the prompt session samples while rendering."""

    status_provider: Callable[[], StatusSnapshot]
    status_block_provider: Callable[[int], AnyFormattedText | None] | None
    fast_refresh_provider: Callable[[], bool] | None
    background_task_count_provider: Callable[[], BgTaskCounts] | None
    update_notice_provider: Callable[[], str | None] | None
    editor_command_provider: Callable[[], str]
    turn_recaps_provider: Callable[[], bool]
    plan_mode_toggle_callback: Callable[[], Awaitable[bool]] | None
    thinking_effort_cycle_callback: Callable[[], Awaitable[str | None]] | None
