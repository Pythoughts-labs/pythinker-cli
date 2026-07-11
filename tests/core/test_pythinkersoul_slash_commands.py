from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pythinker_core.message import Message
from pythinker_core.tooling.empty import EmptyToolset
from pythinker_host.path import HostPath

import pythinker_code.soul.context as context_module
import pythinker_code.soul.pythinkersoul as pythinkersoul_module
import pythinker_code.soul.slash as slash_module
from pythinker_code.skill import Skill
from pythinker_code.skill.flow import Flow, FlowEdge, FlowNode
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.dynamic_injection import DynamicInjectionProvider
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.utils.slashcmd import SlashCommand


def _make_flow() -> Flow:
    nodes = {
        "BEGIN": FlowNode(id="BEGIN", label="Begin", kind="begin"),
        "END": FlowNode(id="END", label="End", kind="end"),
    }
    outgoing = {
        "BEGIN": [FlowEdge(src="BEGIN", dst="END", label=None)],
        "END": [],
    }
    return Flow(nodes=nodes, outgoing=outgoing, begin_id="BEGIN", end_id="END")


class _RearmProvider(DynamicInjectionProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def get_injections(self, history, soul):  # noqa: ANN001
        return []

    async def on_context_compacted(self) -> None:
        self.calls += 1


def test_flow_skill_registers_skill_and_flow_commands(runtime: Runtime, tmp_path: Path) -> None:
    flow = _make_flow()
    skill_dir = tmp_path / "flow-skill"
    skill_dir.mkdir()
    skill_dir_kp = HostPath.unsafe_from_local_path(skill_dir)
    flow_skill = Skill(
        name="flow-skill",
        description="Flow skill",
        type="flow",
        dir=skill_dir_kp,
        skill_md_file=skill_dir_kp / "SKILL.md",
        flow=flow,
        scope="user",
    )
    runtime.skills = {"flow-skill": flow_skill}

    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))

    command_names = {cmd.name for cmd in soul.available_slash_commands}
    assert "skill:flow-skill" in command_names
    assert "flow:flow-skill" in command_names


@pytest.mark.asyncio
async def test_skill_slash_run_does_not_auto_generate_session_title(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_dir.joinpath("SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: demo-skill",
                "description: Demo skill",
                "---",
                "",
                "Use this skill for tests.",
            ]
        ),
        encoding="utf-8",
    )
    runtime.skills = {
        "demo-skill": Skill(
            name="demo-skill",
            description="Demo skill",
            type="standard",
            dir=HostPath.unsafe_from_local_path(skill_dir),
            skill_md_file=HostPath.unsafe_from_local_path(skill_dir / "SKILL.md"),
            scope="user",
        )
    }

    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    soul._turn = AsyncMock(return_value=None)  # type: ignore[method-assign]
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _msg: None)

    await soul.run("/skill:demo-skill fix login")

    assert runtime.session.state.custom_title is None


@pytest.mark.asyncio
async def test_skill_slash_run_appends_local_specialization(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core_dir = tmp_path / "review-pr"
    core_dir.mkdir()
    core_dir.joinpath("SKILL.md").write_text("Core review workflow", encoding="utf-8")
    local_dir = tmp_path / "review-pr-local"
    local_dir.mkdir()
    local_dir.joinpath("SKILL.md").write_text("Local review rules", encoding="utf-8")
    runtime.skills = {
        "review-pr": Skill(
            name="review-pr",
            description="Review PR",
            type="standard",
            dir=HostPath.unsafe_from_local_path(core_dir),
            skill_md_file=HostPath.unsafe_from_local_path(core_dir / "SKILL.md"),
            scope="builtin",
        ),
        "review-pr-local": Skill(
            name="review-pr-local",
            description="Local review rules",
            type="standard",
            dir=HostPath.unsafe_from_local_path(local_dir),
            skill_md_file=HostPath.unsafe_from_local_path(local_dir / "SKILL.md"),
            scope="project",
        ),
    }

    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    turn = AsyncMock(return_value=None)
    monkeypatch.setattr(soul, "_turn", turn)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _msg: None)

    await soul.run("/skill:review-pr check diff")

    message = turn.call_args.args[0]
    text = message.extract_text()
    assert "Core review workflow" in text
    assert "# Local specialization: review-pr-local" in text
    assert "Local review rules" in text
    assert "User request:\ncheck diff" in text


@pytest.mark.asyncio
async def test_flow_slash_run_does_not_auto_generate_session_title(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    soul._slash_command_map["flow:demo-flow"] = SlashCommand(
        name="flow:demo-flow",
        description="Demo flow",
        func=lambda *_args, **_kwargs: None,
        aliases=[],
    )
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _msg: None)

    await soul.run("/flow:demo-flow")

    assert runtime.session.state.custom_title is None


@pytest.mark.asyncio
async def test_clear_slash_notifies_lifecycle_only_after_coherent_reset(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(
        name="Test Agent",
        system_prompt="Current system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    context = Context(file_backend=tmp_path / "history.jsonl")
    soul = PythinkerSoul(agent, context=context)
    await context.write_system_prompt("Current system prompt.")
    before_bytes = context.file_backend.read_bytes()
    notify = AsyncMock()
    soul.notify_history_rebuilt = notify  # type: ignore[method-assign]
    context.replace_history = AsyncMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]
    monkeypatch.setattr(slash_module, "wire_send", lambda _message: None)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)

    with pytest.raises(OSError, match="disk full"):
        await soul.run("/clear")

    assert context.file_backend.read_bytes() == before_bytes
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_visible_durability_error_rearms_before_propagating(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(
        name="Test Agent",
        system_prompt="Current system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    context = Context(file_backend=tmp_path / "history-durability.jsonl")
    soul = PythinkerSoul(agent, context=context)
    provider = _RearmProvider()
    soul._injection_providers = [provider]  # pyright: ignore[reportPrivateUsage]
    await context.append_message(Message(role="user", content="old"))
    lifecycle_generation = soul._request_lifecycle.history_generation  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _message: None)
    monkeypatch.setattr(slash_module, "wire_send", lambda _message: None)

    def fail_directory_sync(_path: Path) -> bool:
        raise OSError("fsync failed")

    monkeypatch.setattr(
        context_module,
        "_sync_parent_directory",
        fail_directory_sync,
    )

    with pytest.raises(
        context_module.ContextPersistenceError,
        match="power-loss durability is uncertain",
    ):
        await soul.run("/clear")

    assert context.system_prompt == "Current system prompt."
    assert context.history == []
    assert soul._request_lifecycle.history_generation == lifecycle_generation + 1  # pyright: ignore[reportPrivateUsage]
    assert provider.calls == 1
