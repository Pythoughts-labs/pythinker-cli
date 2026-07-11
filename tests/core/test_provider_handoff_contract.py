from __future__ import annotations

import dataclasses
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
from pythinker_code.soul.pythinkersoul import PythinkerSoul

_AGENTS_REMINDER = (
    "<system-reminder>\n"
    "The merged `AGENTS.md` project instructions below are authoritative and already "
    "assembled: every file from the project root down to the working directory, deeper "
    "(more specific) files overriding shallower ones, each governing its own directory "
    "and everything beneath it. Treat them with the same authority as your system "
    "instructions.\n\n"
    "`````````\n"
    "Project rule.\n"
    "`````````\n"
    "</system-reminder>"
)


class _StaticInjectionProvider(DynamicInjectionProvider):
    def __init__(self, injection_type: str, content: str) -> None:
        self._injection = DynamicInjection(type=injection_type, content=content)

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        del history, soul
        return [self._injection]


@pytest.mark.asyncio
async def test_agent_step_has_one_characterized_provider_handoff(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builtin_args = dataclasses.replace(
        runtime.builtin_args,
        PYTHINKER_AGENTS_MD="Project rule.",
    )
    runtime = dataclasses.replace(
        runtime,
        builtin_args=builtin_args,
        role="subagent",
        subagent_id="contract-agent",
    )
    toolset = EmptyToolset()
    agent = Agent(
        name="Contract Agent",
        system_prompt="Static provider prompt.\n",
        toolset=toolset,
        runtime=runtime,
    )
    context = Context(file_backend=tmp_path / "history.jsonl")
    soul = PythinkerSoul(agent, context=context)
    await context.append_message(
        [
            Message(role="user", content=[TextPart(text="Original question")]),
            Message(role="assistant", content=[TextPart(text="Original answer")]),
            Message(role="user", content=[TextPart(text="Latest request")]),
        ]
    )

    soul.add_injection_provider(_StaticInjectionProvider("first", "First reminder"))
    soul.add_injection_provider(_StaticInjectionProvider("second", "Second reminder"))

    captured: list[tuple[object, str, object, tuple[Message, ...]]] = []

    async def capture(provider, system_prompt, provider_toolset, history, **_kwargs):
        captured.append((provider, system_prompt, provider_toolset, tuple(history)))
        return StepResult(
            id="characterized-step",
            message=Message(role="assistant", content=[TextPart(text="Done")]),
            usage=None,
            tool_calls=[],
            _tool_result_futures={},
        )

    monkeypatch.setattr(pythinker_core, "step", capture)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    await soul._step()

    assert len(captured) == 1
    provider, system_prompt, provider_toolset, effective_history = captured[0]
    assert runtime.llm is not None
    assert provider is runtime.llm.chat_provider
    assert system_prompt.encode("utf-8") == b"Static provider prompt.\n"
    assert provider_toolset is toolset
    assert effective_history == (
        Message(
            role="user",
            content=[
                TextPart(text=_AGENTS_REMINDER),
                TextPart(text="Original question"),
            ],
        ),
        Message(role="assistant", content=[TextPart(text="Original answer")]),
        Message(
            role="user",
            content=[
                TextPart(text="Latest request"),
                TextPart(
                    text=(
                        "<system-reminder>\nFirst reminder\n</system-reminder>\n"
                        "<system-reminder>\nSecond reminder\n</system-reminder>"
                    )
                ),
            ],
        ),
    )
    assert tuple(context.history) == (
        Message(role="user", content=[TextPart(text="Original question")]),
        Message(role="assistant", content=[TextPart(text="Original answer")]),
        Message(role="user", content=[TextPart(text="Latest request")]),
        Message(
            role="user",
            content=[
                TextPart(
                    text=(
                        "<system-reminder>\nFirst reminder\n</system-reminder>\n"
                        "<system-reminder>\nSecond reminder\n</system-reminder>"
                    )
                )
            ],
        ),
        Message(role="assistant", content=[TextPart(text="Done")]),
    )
