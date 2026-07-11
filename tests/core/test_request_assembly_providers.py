from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
import pythinker_core
from pythinker_core import StepResult
from pythinker_core.message import Message, TextPart
from pythinker_core.tooling.empty import EmptyToolset

import pythinker_code.soul.pythinkersoul as pythinkersoul_module
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider
from pythinker_code.soul.dynamic_injections.model_defense import (
    ModelDefenseFragment,
    ModelDefenseInjectionProvider,
)
from pythinker_code.soul.dynamic_injections.permissions_state import PermissionsInjectionProvider
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.soul.request_assembly import (
    FragmentRequirement,
    FragmentStatus,
    RequestStatus,
)


class _StaticProvider(DynamicInjectionProvider):
    def __init__(self, injection_type: str = "plugin_reminder") -> None:
        self.injection_type = injection_type

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        del history, soul
        return [DynamicInjection(type=self.injection_type, content="Plugin reminder")]


class _FailingProvider(DynamicInjectionProvider):
    async def get_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        del history, soul
        raise RuntimeError("provider unavailable")


def _soul(runtime: Runtime, context: Context) -> PythinkerSoul:
    return PythinkerSoul(
        Agent(
            name="request assembly",
            system_prompt="stable prompt",
            toolset=EmptyToolset(),
            runtime=runtime,
        ),
        context=context,
    )


def _successful_step() -> StepResult:
    return StepResult(
        id="assembled-step",
        message=Message(role="assistant", content=[TextPart(text="Done")]),
        usage=None,
        tool_calls=[],
        _tool_result_futures={},
    )


@pytest.mark.asyncio
async def test_model_defense_preparation_is_replayed_until_acknowledged(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    soul = _soul(runtime, Context(file_backend=tmp_path / "model-defense.jsonl"))
    provider = ModelDefenseInjectionProvider(
        (ModelDefenseFragment(name="mock", patterns=("mock",), content="Defense"),)
    )

    first = await provider.prepare_injections([], soul)
    second = await provider.prepare_injections([], soul)

    assert first == second
    assert len(first) == 1
    provider.acknowledge_injections((first[0].type,))
    assert await provider.prepare_injections([], soul) == []


@pytest.mark.asyncio
async def test_registered_providers_have_explicit_trusted_outcomes(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "registered.jsonl")
    await context.append_message(Message(role="user", content="Do the task"))
    soul = _soul(runtime, context)

    async def capture(*_args: object, **_kwargs: object) -> StepResult:
        return _successful_step()

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    await soul._step()

    manifest = soul.latest_request_manifest
    assert manifest is not None
    assert manifest.status is RequestStatus.SUCCEEDED
    requirements = {outcome.source: outcome.requirement for outcome in manifest.outcomes}
    assert requirements["agents_md"] is FragmentRequirement.REQUIRED
    assert requirements["permissions_state"] is FragmentRequirement.REQUIRED
    assert requirements["model_defense"] is FragmentRequirement.REQUIRED
    assert (
        next(outcome for outcome in manifest.outcomes if outcome.source == "model_defense").status
        is FragmentStatus.NOT_APPLICABLE
    )
    optional_sources = {
        type(provider).__name__
        for provider in soul._injection_providers
        if not isinstance(provider, (PermissionsInjectionProvider, ModelDefenseInjectionProvider))
    }
    assert optional_sources <= {
        outcome.source
        for outcome in manifest.outcomes
        if outcome.requirement is FragmentRequirement.BEST_EFFORT
    }


@pytest.mark.asyncio
async def test_optional_provider_exception_degrades_and_still_invokes_model(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(file_backend=tmp_path / "degraded.jsonl")
    await context.append_message(Message(role="user", content="Do the task"))
    soul = _soul(runtime, context)
    soul._injection_providers = [
        ModelDefenseInjectionProvider(),
        PermissionsInjectionProvider(),
        _FailingProvider(),
    ]
    model_calls = 0

    async def capture(*_args: object, **_kwargs: object) -> StepResult:
        nonlocal model_calls
        model_calls += 1
        return _successful_step()

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    await soul._step()

    assert model_calls == 1
    manifest = soul.latest_request_manifest
    assert manifest is not None
    assert manifest.status is RequestStatus.DEGRADED
    failure = next(outcome for outcome in manifest.outcomes if outcome.source == "FailingProvider")
    assert failure.status is FragmentStatus.DEGRADED
    assert failure.reason_code == "provider_failed"


@pytest.mark.asyncio
async def test_disabled_optional_bus_keeps_required_security_sources(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.config.memory.injection_bus = False
    context = Context(file_backend=tmp_path / "disabled.jsonl")
    await context.append_message(Message(role="user", content="Do the task"))
    soul = _soul(runtime, context)
    soul._injection_providers = [
        _StaticProvider(),
        ModelDefenseInjectionProvider(),
        PermissionsInjectionProvider(),
    ]
    captured_history: tuple[Message, ...] = ()

    async def capture(
        _provider: object,
        _system_prompt: str,
        _toolset: object,
        history: Sequence[Message],
        **_kwargs: object,
    ) -> StepResult:
        nonlocal captured_history
        captured_history = tuple(history)
        return _successful_step()

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    await soul._step()

    rendered = "\n".join(message.extract_text() for message in captured_history)
    assert "Permissions state:" in rendered
    assert "Plugin reminder" not in rendered
    manifest = soul.latest_request_manifest
    assert manifest is not None
    assert (
        next(outcome for outcome in manifest.outcomes if outcome.source == "StaticProvider").status
        is FragmentStatus.NOT_APPLICABLE
    )
