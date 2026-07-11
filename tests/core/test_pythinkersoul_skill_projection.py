from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import pythinker_core
from pythinker_core import StepResult
from pythinker_core.message import Message, TextPart
from pythinker_core.tooling.empty import EmptyToolset
from pythinker_host.path import HostPath

import pythinker_code.soul.pythinkersoul as pythinkersoul_module
from pythinker_code.skill import Skill
from pythinker_code.skill.catalog import (
    SkillCatalog,
    SkillProjectionOutcome,
    SkillProjectionStatus,
)
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.message import system_reminder
from pythinker_code.soul.pythinkersoul import PythinkerSoul


def _skill(tmp_path: Path, name: str, description: str) -> Skill:
    path = tmp_path / name / "SKILL.md"
    return Skill(
        name=name,
        description=description,
        dir=HostPath.unsafe_from_local_path(path.parent),
        skill_md_file=HostPath.unsafe_from_local_path(path),
        scope="user",
    )


async def _capture_one_step(
    runtime: Runtime,
    context: Context,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[tuple[Message, ...], PythinkerSoul, str]:
    runtime = dataclasses.replace(runtime, role="subagent", subagent_id="projection-test")
    soul = PythinkerSoul(
        Agent(
            name="projection",
            system_prompt="static",
            toolset=EmptyToolset(),
            runtime=runtime,
        ),
        context=context,
    )
    captured: list[tuple[Message, ...]] = []
    system_prompts: list[str] = []

    async def capture(_provider, prompt, _toolset, history, **_kwargs):
        system_prompts.append(prompt)
        captured.append(tuple(history))
        return StepResult(
            id="projection",
            message=Message(role="assistant", content=[TextPart(text="done")]),
            usage=None,
            tool_calls=[],
            _tool_result_futures={},
        )

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)
    await soul._step()
    return captured[0], soul, system_prompts[0]


@pytest.mark.asyncio
async def test_candidates_are_request_only_and_use_latest_real_user_task(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skills = {
        name: _skill(tmp_path, name, description)
        for name, description in (
            ("first-explicit", "explicit workflow"),
            ("second-explicit", "explicit workflow"),
            ("old-active", "active workflow"),
            ("new-active", "active workflow"),
            ("spreadsheet", "build workbook tables"),
        )
    }
    runtime.skill_catalog = SkillCatalog(skills, ())
    runtime.skills = dict(runtime.skill_catalog.exhaustive_mapping())
    runtime.session.state.active_skills = ["old-active", "new-active"]
    context = Context(file_backend=tmp_path / "history.jsonl")
    task = "Build workbook tables with $first-explicit then /skill:second-explicit"
    await context.append_message(
        [
            Message(role="user", content=[TextPart(text=task)]),
            Message(
                role="user",
                content=[system_reminder("Injected reminder that must not become the task")],
            ),
        ]
    )

    effective_history, soul, _ = await _capture_one_step(runtime, context, monkeypatch)

    rendered = effective_history[-1].extract_text("\n").split("Task-relevant skills:", 1)[1]
    assert rendered.index("`first-explicit`") < rendered.index("`second-explicit`")
    assert rendered.index("`second-explicit`") < rendered.index("`new-active`")
    assert rendered.index("`new-active`") < rendered.index("`old-active`")
    assert "`spreadsheet`" in rendered
    assert not any(
        "Task-relevant skills:" in message.extract_text("\n") for message in context.history
    )
    assert soul.latest_skill_projection_outcome is not None
    assert soul.latest_skill_projection_outcome.status is SkillProjectionStatus.READY


class _FailedProjectionCatalog(SkillCatalog):
    def prompt_view(self, query: str, *, max_characters: int, explicit_names=(), active_names=()):
        del query, max_characters, explicit_names, active_names
        return SkillProjectionOutcome(
            status=SkillProjectionStatus.FAILED,
            view=None,
            reason_code="safe_projection_failure",
        )


@pytest.mark.asyncio
async def test_projection_failure_is_recorded_without_unbounded_fallback(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skills = {
        f"skill-{index}": _skill(tmp_path, f"skill-{index}", "description") for index in range(100)
    }
    runtime.skill_catalog = _FailedProjectionCatalog(skills, ())
    runtime.skills = dict(runtime.skill_catalog.exhaustive_mapping())
    context = Context(file_backend=tmp_path / "history.jsonl")
    await context.append_message(Message(role="user", content=[TextPart(text="current task")]))

    effective_history, soul, _ = await _capture_one_step(runtime, context, monkeypatch)

    rendered = "\n".join(message.extract_text("\n") for message in effective_history)
    assert "skill-99" not in rendered
    assert soul.latest_skill_projection_outcome is not None
    assert soul.latest_skill_projection_outcome.status is SkillProjectionStatus.FAILED
    assert soul.latest_skill_projection_outcome.reason_code == "safe_projection_failure"


@pytest.mark.asyncio
async def test_plugin_qualified_mentions_resolve_in_left_to_right_priority_order(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skills = {
        name: _skill(tmp_path, name, "ü" * 2_000)
        for name in ("gh-fix-ci", "gh-address-comments", "implicit")
    }
    runtime.skill_catalog = SkillCatalog(skills, ())
    runtime.skills = runtime.skill_catalog.exhaustive_mapping()
    context = Context(file_backend=tmp_path / "qualified.jsonl")
    await context.append_message(
        Message(
            role="user",
            content=[
                TextPart(
                    text=(
                        "Use $github:gh-fix-ci then /skill:github:gh-address-comments "
                        "for implicit work"
                    )
                )
            ],
        )
    )

    history, _, _ = await _capture_one_step(runtime, context, monkeypatch)

    candidates = history[-1].extract_text("\n").split("Task-relevant skills:", 1)[1]
    assert candidates.index("`gh-fix-ci`") < candidates.index("`gh-address-comments`")
    assert candidates.index("`gh-address-comments`") < candidates.index("`implicit`")


@pytest.mark.parametrize("cap", [7_999, 8_000, 8_001])
@pytest.mark.asyncio
async def test_provider_visible_candidate_wrapper_respects_exact_cap_boundaries(
    cap: int,
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skills = {
        f"技能-{index:03d}-{'界' * 80}": _skill(
            tmp_path, f"技能-{index:03d}-{'界' * 80}", "説明🙂" * 300
        )
        for index in range(150)
    }
    runtime.skill_catalog = SkillCatalog(skills, ())
    runtime.skills = runtime.skill_catalog.exhaustive_mapping()
    monkeypatch.setattr(pythinkersoul_module, "SKILL_PROMPT_MAX_CHARACTERS", cap)
    context = Context(file_backend=tmp_path / f"cap-{cap}.jsonl")
    await context.append_message(Message(role="user", content=[TextPart(text="説明")]))

    history, _, _ = await _capture_one_step(runtime, context, monkeypatch)

    candidate_parts = [
        part.text
        for message in history
        for part in message.content
        if isinstance(part, TextPart) and "Task-relevant skills:" in part.text
    ]
    assert len(candidate_parts) == 1
    assert len(candidate_parts[0]) <= cap
    assert "omitted" in candidate_parts[0]
    omitted = int(candidate_parts[0].split("; ", 1)[1].split(" omitted", 1)[0])
    assert omitted >= 10


@pytest.mark.asyncio
async def test_different_tasks_keep_static_prompt_identical_but_change_candidates(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skills = {
        "spreadsheets": _skill(tmp_path, "spreadsheets", "workbook tables"),
        "review-pr": _skill(tmp_path, "review-pr", "review pull requests"),
    }
    runtime.skill_catalog = SkillCatalog(skills, ())
    runtime.skills = runtime.skill_catalog.exhaustive_mapping()
    first = Context(file_backend=tmp_path / "first.jsonl")
    second = Context(file_backend=tmp_path / "second.jsonl")
    await first.append_message(
        Message(role="user", content=[TextPart(text="Create workbook tables")])
    )
    await second.append_message(
        Message(role="user", content=[TextPart(text="Review this pull request")])
    )

    first_history, _, first_prompt = await _capture_one_step(runtime, first, monkeypatch)
    second_history, _, second_prompt = await _capture_one_step(runtime, second, monkeypatch)

    assert first_prompt == second_prompt == "static"
    assert first_history[-1] != second_history[-1]
