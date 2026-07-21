"""Deep prompt rendering and session-resource modules."""

from pythinker_code.ui.shell.prompting.clipboard import ClipboardAdapter
from pythinker_code.ui.shell.prompting.footer import (
    FooterContent,
    FooterViewModel,
    background_task_summary,
    select_footer_content,
    truncate_footer_left,
    truncate_footer_right,
)
from pythinker_code.ui.shell.prompting.frame import (
    FrozenFragments,
    PromptFrame,
    PromptFrameCollector,
    freeze_fragments,
)
from pythinker_code.ui.shell.prompting.git_status import GitSnapshot, GitStatusIndex
from pythinker_code.ui.shell.prompting.history import (
    PromptHistoryError,
    PromptHistoryStatus,
    PromptHistoryStore,
)
from pythinker_code.ui.shell.prompting.renderer import (
    PromptSceneAllocation,
    PromptSceneBudget,
    allocate_prompt_scene_rows,
)
from pythinker_code.ui.shell.prompting.toasts import ToastManager, ToastSnapshot

__all__ = (
    "ClipboardAdapter",
    "FrozenFragments",
    "FooterContent",
    "FooterViewModel",
    "GitSnapshot",
    "GitStatusIndex",
    "PromptFrame",
    "PromptFrameCollector",
    "PromptHistoryError",
    "PromptHistoryStatus",
    "PromptHistoryStore",
    "PromptSceneAllocation",
    "PromptSceneBudget",
    "ToastManager",
    "ToastSnapshot",
    "allocate_prompt_scene_rows",
    "background_task_summary",
    "freeze_fragments",
    "select_footer_content",
    "truncate_footer_left",
    "truncate_footer_right",
)
