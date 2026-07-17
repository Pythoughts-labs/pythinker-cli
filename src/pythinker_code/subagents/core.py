"""Shared core logic for preparing a subagent soul.

Both ``ForegroundSubagentRunner`` and ``BackgroundAgentRunner`` delegate
the repetitive build-restore-prompt pipeline to :func:`prepare_soul` so
that prompt enhancements (e.g. git context injection) only need to be
implemented once.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pythinker_core.message import Message, TextPart, ThinkPart
from pythinker_host.path import HostPath

from pythinker_code.notifications import is_notification_message
from pythinker_code.soul.context import Context
from pythinker_code.soul.message import is_system_reminder_message
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.subagents.builder import SubagentBuilder
from pythinker_code.subagents.git_context import collect_git_context
from pythinker_code.subagents.models import AgentLaunchSpec, AgentTypeDefinition
from pythinker_code.subagents.review_target import (
    REVIEWER_AGENT_TYPES,
    ResolvedReviewTarget,
    revalidate_review_target_head,
)
from pythinker_code.subagents.store import SubagentStore

# NOTE: these must match the registered type names in agents/default/agent.yaml
# (dashed), which _SUBAGENT_PROFILES also keys on — not the yaml file stems.
GIT_CONTEXT_AGENT_TYPES = frozenset({"explore"}) | REVIEWER_AGENT_TYPES
"""Read-oriented agent types whose first prompt gets a git-context prefix.

Exploration and review both orient on repo state (branch, dirty files,
merge base); injecting it up front saves the turns each run would spend
rediscovering its scope. Write-capable types derive state themselves as
part of their task."""

SUBAGENT_OUTPUT_LANGUAGE_INSTRUCTION = """\
<output-language>
Write natural-language output in the same language as the original user request or task
prompt, unless that request explicitly asks for another language. Do not switch to a
model/provider default language (for example Chinese from Qwen). Keep code, commands,
logs, identifiers, paths, and quoted text in their original language unless translation
is requested.
</output-language>
""".strip()

if TYPE_CHECKING:
    from pythinker_code.soul.agent import Runtime


@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentRunSpec:
    """Everything needed to prepare a soul, without lifecycle concerns."""

    agent_id: str
    type_def: AgentTypeDefinition
    launch_spec: AgentLaunchSpec
    prompt: str
    resumed: bool
    # Filtered parent transcript to seed a NEW child with (spawn-time context
    # fork). Ignored on resume; see filter_history_for_fork.
    fork_history: Sequence[Message] | None = None
    # Operational work-dir override (e.g. an isolation worktree); flows into
    # the child runtime via copy_for_subagent.
    work_dir_override: HostPath | None = None
    resolved_review_target: ResolvedReviewTarget | None = None


_CHECKPOINT_MARKER_RE = re.compile(r"^CHECKPOINT \d+$")


def filter_history_for_fork(history: Sequence[Message]) -> list[Message]:
    """Filter a parent transcript down to its conversational spine.

    Keeps user requests and assistant text; drops tool traffic (whose
    call/result pairing would dangle out of context), thinking parts,
    injected reminder/notification wrappers, and checkpoint markers — the
    child should inherit intent and conclusions, not raw activity.
    """
    forked: list[Message] = []
    for message in history:
        if message.role == "user":
            if is_notification_message(message) or is_system_reminder_message(message):
                continue
            text = message.extract_text(" ").strip()
            if not text or _CHECKPOINT_MARKER_RE.match(text):
                continue
            forked.append(Message(role="user", content=[TextPart(text=text)]))
        elif message.role == "assistant":
            text = " ".join(
                part.text
                for part in message.content
                if isinstance(part, TextPart) and not isinstance(part, ThinkPart)
            ).strip()
            if not text:
                continue
            forked.append(Message(role="assistant", content=[TextPart(text=text)]))
    return forked


async def seed_forked_history(
    context: Context, fork_history: Sequence[Message] | None, *, resumed: bool
) -> None:
    """Seed a NEW child's context with the forked parent transcript.

    Persisting through the child's own context file keeps resume working
    unchanged. No-op on resume or when the child already has history.
    """
    if not fork_history or resumed or context.history:
        return
    await context.append_message(list(fork_history))


def _prepend_output_language_instruction(prompt: str) -> str:
    if not prompt.strip():
        return SUBAGENT_OUTPUT_LANGUAGE_INSTRUCTION
    return f"{SUBAGENT_OUTPUT_LANGUAGE_INSTRUCTION}\n\n{prompt}"


def _compose_review_prompt(caller_prompt: str, target: ResolvedReviewTarget) -> str:
    """Keep caller instructions subordinate to the authoritative resolved target."""
    safe_caller_prompt = html.escape(caller_prompt, quote=False)
    return f"<review-task>\n{safe_caller_prompt}\n</review-task>\n\n{target.prompt}"


async def prepare_soul(
    spec: SubagentRunSpec,
    runtime: Runtime,
    builder: SubagentBuilder,
    store: SubagentStore,
    on_stage: Callable[[str], None] | None = None,
) -> tuple[PythinkerSoul, str]:
    """Build agent, restore context, handle system prompt, write prompt file.

    Returns ``(soul, final_prompt)`` ready for execution via
    :func:`run_with_summary_continuation`.
    """

    # 1. Build agent from type definition
    agent = await builder.build_builtin_instance(
        agent_id=spec.agent_id,
        type_def=spec.type_def,
        launch_spec=spec.launch_spec,
        work_dir_override=spec.work_dir_override,
    )
    if on_stage:
        on_stage("agent_built")

    # 2. Restore conversation context
    context = Context(store.context_path(spec.agent_id))
    await context.restore()
    await seed_forked_history(context, spec.fork_history, resumed=spec.resumed)
    if on_stage:
        on_stage("context_restored")

    # 3. System prompt: reuse persisted prompt on resume, persist on first run
    if context.system_prompt is not None:
        agent = replace(agent, system_prompt=context.system_prompt)
    else:
        await context.write_system_prompt(agent.system_prompt)
    if on_stage:
        on_stage("context_ready")

    # 4. Compose the authoritative prompt for this run.
    prompt = spec.prompt
    git_context_dir = spec.work_dir_override or runtime.builtin_args.PYTHINKER_WORK_DIR
    is_reviewer = spec.type_def.name in REVIEWER_AGENT_TYPES
    if spec.resumed and spec.resolved_review_target is not None:
        raise RuntimeError("A resumed subagent cannot receive a new resolved review target.")
    if not spec.resumed and is_reviewer and spec.resolved_review_target is None:
        raise RuntimeError("A fresh reviewer requires a resolved review target.")
    if not is_reviewer and spec.resolved_review_target is not None:
        raise RuntimeError("A non-reviewer cannot receive a resolved review target.")

    git_ctx = ""
    if spec.type_def.name in GIT_CONTEXT_AGENT_TYPES and not spec.resumed:
        git_ctx = await collect_git_context(
            git_context_dir,
            include_merge_base=not is_reviewer,
        )
    if spec.resolved_review_target is not None:
        prompt = _compose_review_prompt(prompt, spec.resolved_review_target)
    if git_ctx:
        prompt = f"{git_ctx}\n\n{prompt}"
    prompt = _prepend_output_language_instruction(prompt)
    if spec.resolved_review_target is not None:
        await revalidate_review_target_head(spec.resolved_review_target, git_context_dir)

    # 5. Write prompt snapshot (debugging aid)
    store.prompt_path(spec.agent_id).write_text(prompt, encoding="utf-8")

    # 6. Create soul
    soul = PythinkerSoul(agent, context=context)
    return soul, prompt
