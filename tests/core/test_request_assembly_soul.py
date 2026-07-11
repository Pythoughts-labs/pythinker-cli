from __future__ import annotations

import asyncio
import inspect
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
from pythinker_code.soul.dynamic_injection import (
    DynamicInjection,
    DynamicInjectionProvider,
    PreparedInjection,
)
from pythinker_code.soul.dynamic_injections.model_defense import (
    ModelDefenseFragment,
    ModelDefenseInjectionProvider,
)
from pythinker_code.soul.dynamic_injections.permissions_state import PermissionsInjectionProvider
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.soul.request_assembly import RequestSourceError, RequestStatus
from pythinker_code.soul.request_lifecycle import RequestLifecycleError
from pythinker_code.soul.slash import clear as clear_context


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
    ) -> list[PreparedInjection]:
        del history, soul
        raise RuntimeError("permission state unavailable")


class _BarrierProvider(DynamicInjectionProvider):
    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        del history, soul
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return [DynamicInjection(type="barrier", content="Concurrent reminder")]


class _FailingAckProvider(_StaticProvider):
    def _on_injections_acknowledged(self, injections: Sequence[DynamicInjection]) -> None:
        del injections
        raise RuntimeError("ack failed")


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

    assert len(captured_history) == 3
    assert captured_history[0].role == "user"
    assert "Test agents content" in captured_history[0].extract_text()
    assert "Original question" in captured_history[0].extract_text()
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

    with pytest.raises(RequestLifecycleError, match="context_persistence_failed"):
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


@pytest.mark.asyncio
async def test_concurrent_main_and_btw_share_one_stateful_preparation(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "concurrent-prepare.jsonl")
    await context.append_message(Message(role="user", content="Main task"))
    soul = _soul(runtime, context)
    provider = _BarrierProvider()
    soul._injection_providers = [provider]

    async def capture(*_args: object, **kwargs: object) -> StepResult:
        if "on_message_part" in kwargs:
            on_message_part = cast(Callable[[TextPart], None], kwargs["on_message_part"])
            on_message_part(TextPart(text="Side answer"))
        return _step_result("Side answer")

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr("pythinker_code.soul.btw.pythinker_core.step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    main_task = asyncio.create_task(soul._step())
    await provider.entered.wait()
    btw_task = asyncio.create_task(execute_side_question(soul, "What changed?"))
    await asyncio.sleep(0)
    provider.release.set()
    await asyncio.gather(main_task, btw_task)

    assert provider.calls == 1


@pytest.mark.asyncio
async def test_cancelled_preparer_releases_lock_and_next_request_retries(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    context = Context(file_backend=tmp_path / "cancelled-prepare.jsonl")
    await context.append_message(Message(role="user", content="Main task"))
    soul = _soul(runtime, context)
    provider = _BarrierProvider()
    soul._injection_providers = [provider]

    first = asyncio.create_task(soul.assemble_side_request("one", "side"))
    await provider.entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    provider.release.set()
    assembled = await asyncio.wait_for(soul.assemble_side_request("two", "side"), timeout=1)

    assert provider.calls == 2
    assert "Concurrent reminder" in "\n".join(
        message.extract_text() for message in assembled.provider_history
    )


@pytest.mark.asyncio
async def test_revert_rearms_both_required_security_sources(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "security-generation.jsonl")
    await context.append_message(Message(role="user", content="Main task"))
    await context.checkpoint(add_user_message=False)
    soul = _soul(runtime, context)
    defense = ModelDefenseInjectionProvider(
        (ModelDefenseFragment(name="mock", patterns=("mock",), content="Defense"),)
    )
    permissions = PermissionsInjectionProvider()
    soul._injection_providers = [defense, permissions]
    captured: list[str] = []

    async def capture(
        _provider: object,
        _system_prompt: str,
        _toolset: object,
        history: Sequence[Message],
        **_kwargs: object,
    ) -> StepResult:
        captured.append("\n".join(message.extract_text() for message in history))
        return _step_result()

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    await soul._step()
    await soul._revert_context_to(0)
    await context.append_message(Message(role="user", content="Retry task"))
    await soul._step()

    assert all("Permissions state:" in item for item in captured)
    assert all("Defense" in item for item in captured)


@pytest.mark.asyncio
async def test_compaction_rebuild_rearms_both_required_security_sources(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "security-compaction.jsonl")
    await context.append_message(Message(role="user", content="Main task"))
    soul = _soul(runtime, context)
    soul._injection_providers = [
        ModelDefenseInjectionProvider(
            (ModelDefenseFragment(name="mock", patterns=("mock",), content="Defense"),)
        ),
        PermissionsInjectionProvider(),
    ]
    captured: list[str] = []

    async def capture(
        _provider: object,
        _system_prompt: str,
        _toolset: object,
        history: Sequence[Message],
        **_kwargs: object,
    ) -> StepResult:
        captured.append("\n".join(message.extract_text() for message in history))
        return _step_result()

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    await soul._step()
    await context.clear()
    await context.append_message(Message(role="user", content="Compacted task"))
    await soul.notify_history_rebuilt()
    await soul._step()

    assert len(captured) == 2
    assert all("Permissions state:" in item for item in captured)
    assert all("Defense" in item for item in captured)
    rebuilt = "\n".join(message.extract_text() for message in context.history)
    assert rebuilt.count("Permissions state:") == 1
    assert rebuilt.count("Defense") == 1


@pytest.mark.asyncio
async def test_cancellation_during_commit_finishes_commit_and_dedupes_retry(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "cancel-commit.jsonl")
    await context.append_message(Message(role="user", content="Main task"))
    soul = _soul(runtime, context)
    soul._injection_providers = [_StaticProvider()]
    entered = asyncio.Event()
    release = asyncio.Event()
    real_append = context.append_message

    async def blocked_append(message: Message | Sequence[Message]) -> None:
        entered.set()
        await release.wait()
        await real_append(message)

    async def capture(*_args: object, **_kwargs: object) -> StepResult:
        return _step_result()

    monkeypatch.setattr(context, "append_message", blocked_append)
    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    step = asyncio.create_task(soul._step())
    await entered.wait()
    step.cancel()
    await asyncio.sleep(0)
    step.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await step

    assert soul.latest_request_manifest is not None
    assert soul.latest_request_manifest.status is RequestStatus.FAILED
    assert soul.latest_request_manifest.reason_code == "context_persistence_cancelled"
    monkeypatch.setattr(context, "append_message", real_append)
    await soul._step()
    persisted = "\n".join(message.extract_text() for message in context.history)
    assert persisted.count("Stable plugin reminder") == 1


@pytest.mark.asyncio
async def test_clear_rearms_required_security_sources_for_next_turn(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "clear-generation.jsonl")
    await context.append_message(Message(role="user", content="Main task"))
    soul = _soul(runtime, context)
    soul._injection_providers = [
        PermissionsInjectionProvider(),
        ModelDefenseInjectionProvider(
            (ModelDefenseFragment(name="mock", patterns=("mock",), content="Defense"),)
        ),
    ]
    captured: list[str] = []

    async def capture(
        _provider: object,
        _system_prompt: str,
        _toolset: object,
        history: Sequence[Message],
        **_kwargs: object,
    ) -> StepResult:
        captured.append("\n".join(message.extract_text() for message in history))
        return _step_result()

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)
    monkeypatch.setattr("pythinker_code.soul.slash.wire_send", lambda _message: None)

    await soul._step()
    await clear_context(soul, "")  # type: ignore[reportGeneralTypeIssues]
    await context.append_message(Message(role="user", content="New task"))
    await soul._step()

    assert len(captured) == 2
    assert all("Permissions state:" in request for request in captured)
    assert all("Defense" in request for request in captured)
    rebuilt = "\n".join(message.extract_text() for message in context.history)
    assert rebuilt.count("Permissions state:") == 1
    assert rebuilt.count("Defense") == 1


@pytest.mark.asyncio
async def test_history_rebuild_discards_obsolete_committed_identity_generations(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "bounded-generations.jsonl")
    await context.append_message(Message(role="user", content="Main task"))
    soul = _soul(runtime, context)

    async def capture(*_args: object, **_kwargs: object) -> StepResult:
        return _step_result()

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    await soul._step()
    assert soul._request_lifecycle.committed_identity_count > 0
    for _ in range(20):
        soul._request_lifecycle.context_rebuilt()
        assert soul._request_lifecycle.committed_identity_count == 0


@pytest.mark.asyncio
async def test_ack_failure_replaces_manifest_and_retry_does_not_duplicate(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "ack-failure.jsonl")
    await context.append_message(Message(role="user", content="Main task"))
    soul = _soul(runtime, context)
    soul._injection_providers = [_FailingAckProvider()]

    async def capture(*_args: object, **_kwargs: object) -> StepResult:
        return _step_result()

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    with pytest.raises(RequestLifecycleError, match="provider_finalization_failed"):
        await soul._step()

    assert soul.latest_request_manifest is not None
    assert soul.latest_request_manifest.status is RequestStatus.FAILED
    assert soul.latest_request_manifest.reason_code == "provider_finalization_failed"
    await soul._step()
    persisted = "\n".join(message.extract_text() for message in context.history)
    assert persisted.count("Stable plugin reminder") == 1


@pytest.mark.asyncio
async def test_permission_posture_rearm_commits_new_identity_once(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "permission-rearm.jsonl")
    await context.append_message(Message(role="user", content="Main task"))
    soul = _soul(runtime, context)
    soul._injection_providers = [PermissionsInjectionProvider(), ModelDefenseInjectionProvider()]

    async def capture(*_args: object, **_kwargs: object) -> StepResult:
        return _step_result()

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    await soul._step()
    await soul._step()
    assert soul.latest_request_manifest is not None
    satisfied = next(
        outcome
        for outcome in soul.latest_request_manifest.outcomes
        if outcome.source.startswith("permissions_state")
    )
    assert satisfied.reason_code == "already_satisfied"
    initial_yolo = runtime.approval.is_yolo()
    runtime.approval.set_yolo(not initial_yolo)
    soul.rearm_injection("permissions_state")
    await soul._step()
    await soul._step()

    persisted = "\n".join(message.extract_text() for message in context.history)
    assert persisted.count("Permissions state:") == 2
    expected = "yolo off" if initial_yolo else "yolo on"
    assert persisted.count(expected) == 1


@pytest.mark.asyncio
async def test_cancellation_before_commit_keeps_history_and_provider_uncommitted(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    context = Context(file_backend=tmp_path / "cancel-before-commit.jsonl")
    await context.append_message(Message(role="user", content="Main task"))
    soul = _soul(runtime, context)
    provider = _BarrierProvider()
    soul._injection_providers = [provider]

    step = asyncio.create_task(soul._step())
    await provider.entered.wait()
    step.cancel()
    with pytest.raises(asyncio.CancelledError):
        await step

    assert len(context.history) == 1
    provider.release.set()
    assembled = await soul.assemble_side_request("retry", "side")
    assert "Concurrent reminder" in "\n".join(
        message.extract_text() for message in assembled.provider_history
    )


@pytest.mark.asyncio
async def test_cancellation_after_finalize_keeps_one_committed_reminder(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "cancel-after-finalize.jsonl")
    await context.append_message(Message(role="user", content="Main task"))
    soul = _soul(runtime, context)
    soul._injection_providers = [_StaticProvider()]
    provider_entered = asyncio.Event()
    provider_release = asyncio.Event()

    async def blocked_model(*_args: object, **_kwargs: object) -> StepResult:
        provider_entered.set()
        await provider_release.wait()
        return _step_result()

    monkeypatch.setattr(pythinker_core, "step", blocked_model)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    step = asyncio.create_task(soul._step())
    await provider_entered.wait()
    step.cancel()
    with pytest.raises(asyncio.CancelledError):
        await step

    persisted = "\n".join(message.extract_text() for message in context.history)
    assert persisted.count("Stable plugin reminder") == 1

    provider_release.set()

    async def capture(*_args: object, **_kwargs: object) -> StepResult:
        return _step_result()

    monkeypatch.setattr(pythinker_core, "step", capture)
    await soul._step()
    persisted = "\n".join(message.extract_text() for message in context.history)
    assert persisted.count("Stable plugin reminder") == 1


def test_request_lifecycle_owns_dynamic_dedupe_state() -> None:
    assert "_committed_injection_keys" not in inspect.getsource(PythinkerSoul)
    assert "_dynamic_request_sources" not in PythinkerSoul.__dict__
