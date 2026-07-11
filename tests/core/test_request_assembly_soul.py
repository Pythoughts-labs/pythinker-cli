from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

import pytest
import pythinker_core
from pythinker_core import StepResult
from pythinker_core.message import Message, TextPart
from pythinker_core.tooling.empty import EmptyToolset
from pythinker_host.path import HostPath

import pythinker_code.soul.pythinkersoul as pythinkersoul_module
from pythinker_code.skill import Skill, SkillCatalog
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.btw import execute_side_question
from pythinker_code.soul.context import Context
from pythinker_code.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider
from pythinker_code.soul.dynamic_injections.model_defense import ModelDefenseInjectionProvider
from pythinker_code.soul.dynamic_injections.permissions_state import PermissionsInjectionProvider
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.soul.request_assembly import RequestSourceError, RequestStatus


class _StaticProvider(DynamicInjectionProvider):
    async def get_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        del history, soul
        return [DynamicInjection(type="stable_plugin", content="Stable plugin reminder")]


class _FailingPermissionsProvider(PermissionsInjectionProvider):
    async def prepare_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        del history, soul
        raise RuntimeError("permission state unavailable")


def _soul(runtime: Runtime, context: Context) -> PythinkerSoul:
    return PythinkerSoul(
        Agent(
            name="request assembly",
            system_prompt="static provider prompt\n",
            toolset=EmptyToolset(),
            runtime=runtime,
        ),
        context=context,
    )


def _step_result(text: str = "Done") -> StepResult:
    return StepResult(
        id="assembled-step",
        message=Message(role="assistant", content=[TextPart(text=text)]),
        usage=None,
        tool_calls=[],
        _tool_result_futures={},
    )


@pytest.mark.asyncio
async def test_required_provider_failure_skips_model_and_records_failed_manifest(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "required-failure.jsonl")
    await context.append_message(Message(role="user", content="Do the task"))
    soul = _soul(runtime, context)
    soul._injection_providers = [
        ModelDefenseInjectionProvider(),
        _FailingPermissionsProvider(),
    ]
    model_calls = 0

    async def capture(*_args: object, **_kwargs: object) -> StepResult:
        nonlocal model_calls
        model_calls += 1
        return _step_result()

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    with pytest.raises(RequestSourceError, match="permissions_state_unavailable"):
        await soul._step()

    assert model_calls == 0
    assert soul.latest_request_manifest is not None
    assert soul.latest_request_manifest.status is RequestStatus.FAILED
    assert soul.latest_request_manifest.reason_code == "permissions_state_unavailable"


@pytest.mark.asyncio
async def test_history_handoff_is_byte_equivalent_and_request_only_sources_are_not_persisted(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = Skill(
        name="deploy-check",
        description="deployment validation",
        dir=HostPath.unsafe_from_local_path(tmp_path / "deploy-check"),
        skill_md_file=HostPath.unsafe_from_local_path(tmp_path / "deploy-check" / "SKILL.md"),
        scope="project",
    )
    runtime.skill_catalog = SkillCatalog({skill.name: skill}, ())
    runtime.skills = runtime.skill_catalog.exhaustive_mapping()
    context = Context(file_backend=tmp_path / "equivalent.jsonl")
    await context.append_message(
        [
            Message(role="user", content="Original question"),
            Message(role="assistant", content="Original answer"),
            Message(role="user", content="Validate this deployment"),
        ]
    )
    soul = _soul(runtime, context)
    soul._injection_providers = [_StaticProvider()]
    captured_history: list[Message] = []

    async def capture(
        _provider: object,
        _system_prompt: str,
        _toolset: object,
        history: Sequence[Message],
        **_kwargs: object,
    ) -> StepResult:
        captured_history.extend(history)
        return _step_result()

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    await soul._step()

    assert captured_history[0].role == "user"
    assert "Test agents content" in captured_history[0].extract_text()
    assert captured_history[1] == Message(role="assistant", content="Original answer")
    final_text = captured_history[-1].extract_text("\n")
    assert "Validate this deployment" in final_text
    assert "Stable plugin reminder" in final_text
    assert "Task-relevant skills:" in final_text
    persisted = "\n".join(message.extract_text() for message in context.history)
    assert persisted.count("Stable plugin reminder") == 1
    assert "Task-relevant skills:" not in persisted
    assert "Test agents content" not in persisted


@pytest.mark.asyncio
async def test_persistence_failure_skips_model_and_retries_same_identity(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_file = tmp_path / "persistence.jsonl"
    context = Context(file_backend=context_file)
    await context.append_message(Message(role="user", content="Do the task"))
    soul = _soul(runtime, context)
    soul._injection_providers = [_StaticProvider()]
    model_calls = 0

    async def capture(*_args: object, **_kwargs: object) -> StepResult:
        nonlocal model_calls
        model_calls += 1
        return _step_result()

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)
    append_message = context.append_message
    persistence_attempts = 0

    async def fail_once(message: Message | Sequence[Message]) -> None:
        nonlocal persistence_attempts
        persistence_attempts += 1
        if persistence_attempts == 1:
            raise PermissionError("context unavailable")
        await append_message(message)

    monkeypatch.setattr(context, "append_message", fail_once)

    with pytest.raises(PermissionError):
        await soul._step()

    assert model_calls == 0
    assert soul.latest_request_manifest is not None
    assert soul.latest_request_manifest.status is RequestStatus.FAILED
    assert soul.latest_request_manifest.reason_code == "context_persistence_failed"

    await soul._step()

    assert model_calls == 1
    persisted = "\n".join(message.extract_text() for message in context.history)
    assert persisted.count("Stable plugin reminder") == 1


@pytest.mark.asyncio
async def test_stable_provider_key_prevents_duplicate_history_on_later_steps(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "dedupe.jsonl")
    await context.append_message(Message(role="user", content="Do the task"))
    soul = _soul(runtime, context)
    soul._injection_providers = [_StaticProvider()]

    async def capture(*_args: object, **_kwargs: object) -> StepResult:
        return _step_result()

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    await soul._step()
    await soul._step()

    persisted = "\n".join(message.extract_text() for message in context.history)
    assert persisted.count("Stable plugin reminder") == 1


@pytest.mark.asyncio
async def test_model_failure_after_persistence_keeps_committed_reminder(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.config.loop_control.max_retries_per_step = 1
    context = Context(file_backend=tmp_path / "provider-failure.jsonl")
    await context.append_message(Message(role="user", content="Do the task"))
    soul = _soul(runtime, context)
    soul._injection_providers = [_StaticProvider()]

    async def fail(*_args: object, **_kwargs: object) -> StepResult:
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(pythinker_core, "step", fail)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    with pytest.raises(RuntimeError, match="model unavailable"):
        await soul._step()

    persisted = "\n".join(message.extract_text() for message in context.history)
    assert persisted.count("Stable plugin reminder") == 1


@pytest.mark.asyncio
async def test_default_btw_uses_request_assembler_without_persisting_side_question(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "btw.jsonl")
    await context.append_message(Message(role="user", content="Main task"))
    soul = _soul(runtime, context)
    soul._injection_providers = [_StaticProvider()]
    captured_history: list[Message] = []

    async def capture(
        _provider: object,
        _system_prompt: str,
        _toolset: object,
        history: Sequence[Message],
        **kwargs: object,
    ) -> StepResult:
        captured_history.extend(history)
        on_message_part = cast(Callable[[TextPart], None], kwargs["on_message_part"])
        on_message_part(TextPart(text="Side answer"))
        return _step_result("Side answer")

    monkeypatch.setattr("pythinker_code.soul.btw.pythinker_core.step", capture)

    response, error = await execute_side_question(soul, "What changed?")

    assert response == "Side answer"
    assert error is None
    rendered = "\n".join(message.extract_text() for message in captured_history)
    assert "Test agents content" in rendered
    assert "Stable plugin reminder" in rendered
    assert "What changed?" in rendered
    persisted = "\n".join(message.extract_text() for message in context.history)
    assert "Stable plugin reminder" not in persisted
    assert "What changed?" not in persisted
