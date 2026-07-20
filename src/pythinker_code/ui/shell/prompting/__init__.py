"""Immutable prompt-render frame capture."""

from pythinker_code.ui.shell.prompting.frame import (
    FrozenFragments,
    PromptFrame,
    PromptFrameCollector,
    freeze_fragments,
)
from pythinker_code.ui.shell.prompting.renderer import (
    PromptSceneAllocation,
    PromptSceneBudget,
    allocate_prompt_scene_rows,
)

__all__ = (
    "FrozenFragments",
    "PromptFrame",
    "PromptFrameCollector",
    "PromptSceneAllocation",
    "PromptSceneBudget",
    "allocate_prompt_scene_rows",
    "freeze_fragments",
)
