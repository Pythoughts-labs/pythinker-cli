from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pythinker_core.message import Message, TextPart, ToolCall
from pythinker_core.tooling.empty import EmptyToolset
from pythinker_host.path import HostPath

from pythinker_code.hooks.runner import HookResult
from pythinker_code.skill import Skill
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.compaction_restore import (
    _display_path,
    build_compaction_restore_context,
    build_hook_context_message,
    compact_summary_text,
)
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul


def _tool_call(name: str, arguments: str) -> ToolCall:
    return ToolCall(
        id=f"{name}-1",
        function=ToolCall.FunctionBody(name=name, arguments=arguments),
    )


@pytest.mark.asyncio
async def test_restore_context_collects_recent_read_and_referenced_files(tmp_path: Path) -> None:
    history = [
        Message(role="user", content=[TextPart(text="Please review @docs/plan.md")]),
        Message(
            role="assistant",
            content=[TextPart(text="I'll inspect it.")],
            tool_calls=[
                _tool_call("ReadFile", '{"path":"src/app.py"}'),
                _tool_call("WriteFile", '{"path":"src/output.py","content":"x"}'),
            ],
        ),
    ]

    context = await build_compaction_restore_context(
        history,
        work_dir=HostPath.unsafe_from_local_path(tmp_path),
    )

    assert context.read_files == ("src/app.py",)
    assert context.referenced_files == ("docs/plan.md", "src/app.py", "src/output.py")
    assert context.messages
    restored_text = context.messages[0].extract_text("\n")
    assert "Referenced files:" in restored_text
    assert "Recently read files:" in restored_text
    assert "src/app.py" in restored_text
    assert "src/output.py" in restored_text


@pytest.mark.asyncio
async def test_restore_context_reinjects_active_skill_text(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "careful"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: careful\n---\n# Careful\nAlways verify.", encoding="utf-8")
    skill = Skill(
        name="careful",
        description="Be careful",
        dir=HostPath.unsafe_from_local_path(skill_dir),
        skill_md_file=HostPath.unsafe_from_local_path(skill_file),
        scope="project",
    )

    context = await build_compaction_restore_context(
        [],
        work_dir=HostPath.unsafe_from_local_path(tmp_path),
        active_skill_names=["careful"],
        skills_by_name={"careful": skill},
    )

    assert context.restored_skills == ("careful",)
    restored_text = context.messages[0].extract_text("\n")
    assert "Skill restored after compaction: careful" in restored_text
    assert "Always verify." in restored_text
    assert "Skills restored (careful)" in context.display_text()


def test_compact_summary_text_extracts_first_compacted_message() -> None:
    messages = [
        Message(
            role="user",
            content=[
                TextPart(text="Previous context has been compacted."),
                TextPart(text="Summary"),
            ],
        ),
        Message(role="assistant", content=[TextPart(text="Recent response")]),
    ]

    assert compact_summary_text(messages) == "Previous context has been compacted.\nSummary"


def test_build_hook_context_message_uses_additional_context_only() -> None:
    message = build_hook_context_message(["", "Reload these rules"])

    assert message is not None
    assert "Reload these rules" in message.extract_text("\n")
    assert "post-compaction hooks" in message.extract_text("\n")


@pytest.mark.asyncio
async def test_compact_context_restores_files_and_hook_context(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    context = Context(file_backend=tmp_path / "history.jsonl")
    soul = PythinkerSoul(agent, context=context)
    runtime.session.state.active_skills = []

    await context.append_message(
        Message(role="user", content=[TextPart(text="Read @docs/plan.md")])
    )
    await context.append_message(
        Message(
            role="assistant",
            content=[TextPart(text="Reading")],
            tool_calls=[_tool_call("ReadFile", '{"path":"src/app.py"}')],
        )
    )

    fake_result = MagicMock()
    fake_result.messages = [Message(role="user", content=[TextPart(text="compacted summary")])]
    fake_result.estimated_token_count = 10
    soul._run_with_connection_recovery = AsyncMock(return_value=fake_result)  # pyright: ignore[reportPrivateUsage]
    soul._checkpoint = AsyncMock()  # pyright: ignore[reportPrivateUsage]
    soul.notify_history_rebuilt = AsyncMock()
    soul._hook_engine.trigger = AsyncMock(  # pyright: ignore[reportPrivateUsage]
        side_effect=[
            [],
            [HookResult(additional_context="Hook restored context")],
            [HookResult(additional_context="SessionStart compact context")],
        ]
    )

    sent_texts: list[str] = []

    def _capture_wire(msg):
        if isinstance(msg, TextPart):
            sent_texts.append(msg.text)

    with patch("pythinker_code.soul.pythinkersoul.wire_send", _capture_wire):
        await soul.compact_context(custom_instruction="keep files")

    history_text = "\n".join(message.extract_text("\n") for message in context.history)
    assert "Referenced files:" in history_text
    assert "docs/plan.md" in history_text
    assert "src/app.py" in history_text
    assert "Hook restored context" in history_text
    assert "SessionStart compact context" in history_text
    assert any("Conversation compacted." in text for text in sent_texts)
    assert any("Referenced file docs/plan.md" in text for text in sent_texts)
    assert any("Read src/app.py" in text for text in sent_texts)

    pre_call = soul._hook_engine.trigger.await_args_list[0]  # pyright: ignore[reportPrivateUsage]
    assert pre_call.args[0] == "PreCompact"
    assert pre_call.kwargs["input_data"]["custom_instructions"] == "keep files"

    post_call = soul._hook_engine.trigger.await_args_list[1]  # pyright: ignore[reportPrivateUsage]
    assert post_call.args[0] == "PostCompact"
    assert post_call.kwargs["input_data"]["compact_summary"] == "compacted summary"

    session_start_call = soul._hook_engine.trigger.await_args_list[2]  # pyright: ignore[reportPrivateUsage]
    assert session_start_call.args[0] == "SessionStart"
    assert session_start_call.kwargs["matcher_value"] == "compact"


@pytest.mark.asyncio
async def test_compact_context_restores_history_when_rebuild_fails(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    context = Context(file_backend=tmp_path / "history-rebuild-fail.jsonl")
    soul = PythinkerSoul(agent, context=context)
    runtime.session.state.active_skills = []

    # Seed two messages so history_before_compaction is non-trivial
    msg_user = Message(role="user", content=[TextPart(text="Hello")])
    msg_assistant = Message(role="assistant", content=[TextPart(text="World")])
    await context.append_message(msg_user)
    await context.append_message(msg_assistant)

    before = list(context.history)

    fake_result = MagicMock()
    fake_result.messages = [Message(role="user", content=[TextPart(text="compacted-summary")])]
    fake_result.estimated_token_count = 5
    fake_result.usage = None
    soul._run_with_connection_recovery = AsyncMock(return_value=fake_result)  # pyright: ignore[reportPrivateUsage]
    soul._checkpoint = AsyncMock()  # pyright: ignore[reportPrivateUsage]
    soul._hook_engine.trigger = AsyncMock(return_value=[])  # pyright: ignore[reportPrivateUsage]

    # Wrap append_message: raise when seeing the compacted summary text so the
    # fault lands after clear() has already rotated the backing file.
    real_append = context.append_message

    async def flaky_append(message):
        msgs = [message] if isinstance(message, Message) else list(message)
        for m in msgs:
            if "compacted-summary" in m.extract_text(""):
                raise RuntimeError("disk full")
        return await real_append(message)

    context.append_message = flaky_append  # type: ignore[method-assign]

    with (
        patch("pythinker_code.soul.pythinkersoul.wire_send"),
        patch("pythinker_code.telemetry.track"),
        pytest.raises(RuntimeError, match="disk full"),
    ):
        await soul.compact_context()

    assert list(context.history) == before


@pytest.mark.asyncio
async def test_compact_context_emits_end_when_compaction_fails(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    context = Context(file_backend=tmp_path / "history-failure.jsonl")
    soul = PythinkerSoul(agent, context=context)
    runtime.session.state.active_skills = []

    await context.append_message(Message(role="user", content=[TextPart(text="compact me")]))

    soul._run_with_connection_recovery = AsyncMock(  # pyright: ignore[reportPrivateUsage]
        side_effect=RuntimeError("LLM 5xx")
    )
    soul._hook_engine.trigger = AsyncMock(return_value=[])  # pyright: ignore[reportPrivateUsage]

    sent: list[str] = []

    def _capture_wire(msg):
        sent.append(type(msg).__name__)

    with (
        patch("pythinker_code.soul.pythinkersoul.wire_send", _capture_wire),
        patch("pythinker_code.telemetry.track") as track,
        pytest.raises(RuntimeError, match="LLM 5xx"),
    ):
        await soul.compact_context()

    assert sent.count("CompactionBegin") == 1
    assert sent.count("CompactionEnd") == 1
    track.assert_called_once_with(
        "compaction_triggered",
        trigger_type="auto",
        before_tokens=context.token_count,
        success=False,
    )


def test_display_path_skips_out_of_workspace_absolute_paths(tmp_path: Path) -> None:
    work = HostPath.unsafe_from_local_path(tmp_path)

    # Out-of-workspace absolute paths must return None (security: no /etc/passwd resurface)
    assert _display_path("/etc/passwd", work_dir=work) is None
    assert _display_path(str(tmp_path.parent / "sibling.py"), work_dir=work) is None

    # In-workspace absolute: still relativized correctly
    assert _display_path(str(tmp_path / "src/app.py"), work_dir=work) == "src/app.py"

    # Relative path: returned unchanged (strip leading ./)
    assert _display_path("src/app.py", work_dir=work) == "src/app.py"


def test_display_path_keeps_additional_dir_files_absolute(tmp_path: Path) -> None:
    work = HostPath.unsafe_from_local_path(tmp_path / "ws")
    add = HostPath.unsafe_from_local_path(tmp_path / "lib")
    lib_file = str(tmp_path / "lib" / "util.py")

    # Files under an --add-dir root are legitimate workspace members and must
    # survive in restore reminders (absolute, to stay unambiguous).
    assert _display_path(lib_file, work_dir=work, additional_dirs=(add,)) == lib_file

    # Without the additional root the same path is still skipped.
    assert _display_path(lib_file, work_dir=work) is None

    # A sibling sharing the additional root's prefix is not contained.
    sneaky = str(tmp_path / "lib-extra" / "f.py")
    assert _display_path(sneaky, work_dir=work, additional_dirs=(add,)) is None

    # Out-of-workspace absolutes still never resurface.
    assert _display_path("/etc/passwd", work_dir=work, additional_dirs=(add,)) is None
