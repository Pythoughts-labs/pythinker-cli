"""Same-step tool-call concurrency policy.

When a provider emits parallel tool calls, mutating tools (WriteFile +
Shell from one assistant message) used to execute concurrently with no
race protection. Policy: parallel-safe tools overlap freely; everything
else (including unflagged plugin/MCP tools — safe default) runs
exclusively, keeping same-step mutation ordering deterministic.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pythinker_core.tooling import ToolReturnValue

from pythinker_code.hooks.engine import HookEngine
from pythinker_code.soul.toolset import PythinkerToolset
from pythinker_code.wire.types import ToolCall


class _RecordingTool:
    base = None

    def __init__(
        self, name: str, events: list[tuple[str, str]], *, parallel: bool, delay: float = 0.05
    ) -> None:
        self.name = name
        self._events = events
        self._delay = delay
        if parallel:
            self.supports_parallel = True

    async def call(self, arguments: object) -> ToolReturnValue:
        self._events.append(("enter", self.name))
        await asyncio.sleep(self._delay)
        self._events.append(("exit", self.name))
        return ToolReturnValue(is_error=False, output="ok", message="ok", display=[])


def _toolset(*tools: _RecordingTool, cwd: Path) -> PythinkerToolset:
    toolset = PythinkerToolset()
    toolset._hook_engine = HookEngine([], cwd=str(cwd))
    for tool in tools:
        toolset._tool_dict[tool.name] = tool  # type: ignore[assignment]
    return toolset


async def _dispatch(toolset: PythinkerToolset, *names: str) -> None:
    tasks = []
    for index, name in enumerate(names):
        result = toolset.handle(
            ToolCall(id=f"tc_{index}", function=ToolCall.FunctionBody(name=name, arguments="{}"))
        )
        assert isinstance(result, asyncio.Task)
        tasks.append(result)
    await asyncio.gather(*tasks)


class TestSameStepConcurrencyPolicy:
    async def test_mutating_tools_serialize_in_dispatch_order(self, tmp_path: Path) -> None:
        events: list[tuple[str, str]] = []
        toolset = _toolset(
            _RecordingTool("WriteA", events, parallel=False),
            _RecordingTool("WriteB", events, parallel=False),
            cwd=tmp_path,
        )

        await _dispatch(toolset, "WriteA", "WriteB")

        assert events == [
            ("enter", "WriteA"),
            ("exit", "WriteA"),
            ("enter", "WriteB"),
            ("exit", "WriteB"),
        ]

    async def test_parallel_safe_tools_overlap(self, tmp_path: Path) -> None:
        events: list[tuple[str, str]] = []
        toolset = _toolset(
            _RecordingTool("ReadA", events, parallel=True),
            _RecordingTool("ReadB", events, parallel=True),
            cwd=tmp_path,
        )

        await _dispatch(toolset, "ReadA", "ReadB")

        assert {events[0][0], events[1][0]} == {"enter"}, events

    async def test_reader_waits_for_earlier_writer(self, tmp_path: Path) -> None:
        events: list[tuple[str, str]] = []
        toolset = _toolset(
            _RecordingTool("Write", events, parallel=False),
            _RecordingTool("Read", events, parallel=True),
            cwd=tmp_path,
        )

        await _dispatch(toolset, "Write", "Read")

        assert events.index(("exit", "Write")) < events.index(("enter", "Read"))

    async def test_writer_waits_for_inflight_readers(self, tmp_path: Path) -> None:
        events: list[tuple[str, str]] = []
        toolset = _toolset(
            _RecordingTool("Read", events, parallel=True),
            _RecordingTool("Write", events, parallel=False),
            cwd=tmp_path,
        )

        await _dispatch(toolset, "Read", "Write")

        assert events.index(("exit", "Read")) < events.index(("enter", "Write"))

    async def test_unflagged_plugin_like_tool_runs_exclusively(self, tmp_path: Path) -> None:
        events: list[tuple[str, str]] = []
        toolset = _toolset(
            _RecordingTool("PluginA", events, parallel=False),
            _RecordingTool("PluginB", events, parallel=False),
            cwd=tmp_path,
        )

        await _dispatch(toolset, "PluginA", "PluginB")

        assert events == [
            ("enter", "PluginA"),
            ("exit", "PluginA"),
            ("enter", "PluginB"),
            ("exit", "PluginB"),
        ]


class TestParallelSafeFlags:
    def test_read_only_builtins_are_parallel_safe(self) -> None:
        from pythinker_code.tools.file.glob import Glob
        from pythinker_code.tools.file.grep_local import Grep, SmartSearch
        from pythinker_code.tools.file.read import ReadFile
        from pythinker_code.tools.mcp_resource import ListMcpResources, ReadMcpResource
        from pythinker_code.tools.think import Think

        for tool_cls in (
            Glob,
            Grep,
            SmartSearch,
            ReadFile,
            ListMcpResources,
            ReadMcpResource,
            Think,
        ):
            assert getattr(tool_cls, "supports_parallel", False), tool_cls.__name__

    def test_mutating_builtins_stay_exclusive(self) -> None:
        from pythinker_code.tools.file.replace import StrReplaceFile
        from pythinker_code.tools.file.write import WriteFile
        from pythinker_code.tools.shell import Shell

        for tool_cls in (WriteFile, StrReplaceFile, Shell):
            assert not getattr(tool_cls, "supports_parallel", False), tool_cls.__name__


async def test_read_gate_caps_concurrent_readers() -> None:
    """Parallel-safe tools overlap, but not without bound: the gate caps concurrent
    readers so a turn that fans out many parallel-safe tools (e.g. dozens of FetchURL)
    cannot open unbounded sockets/file handles at once."""
    from pythinker_code.soul.toolset import _ReadWriteGate

    gate = _ReadWriteGate(max_concurrent_readers=2)
    live = 0
    peak = 0
    in_body = asyncio.Semaphore(0)
    release = asyncio.Event()

    async def reader() -> None:
        nonlocal live, peak
        async with gate.shared():
            live += 1
            peak = max(peak, live)
            in_body.release()
            await release.wait()
            live -= 1

    tasks = [asyncio.create_task(reader()) for _ in range(5)]
    # Block until the gate is saturated at the cap (2 readers in their body).
    await in_body.acquire()
    await in_body.acquire()
    # Give any (incorrectly) unbounded extra readers a chance to slip in.
    await asyncio.sleep(0.05)
    assert peak == 2, f"reader concurrency exceeded the cap: peak={peak}"
    assert live == 2  # exactly the cap in-flight; the other 3 queued on the semaphore
    release.set()
    await asyncio.gather(*tasks)


async def test_read_gate_cap_does_not_block_writer_draining() -> None:
    """A reader queued on the cap has not yet entered the critical section, so a
    writer can still acquire exclusivity once in-flight readers drain — the cap must
    never deadlock the writer path."""
    from pythinker_code.soul.toolset import _ReadWriteGate

    gate = _ReadWriteGate(max_concurrent_readers=1)
    reader_release = asyncio.Event()
    reader_in_body = asyncio.Event()

    async def holding_reader() -> None:
        async with gate.shared():
            reader_in_body.set()
            await reader_release.wait()

    held = asyncio.create_task(holding_reader())
    await reader_in_body.wait()
    # A second reader is now queued on the cap (cannot enter the body).
    queued = asyncio.create_task(holding_reader())
    writer_ran = asyncio.Event()

    async def writer() -> None:
        async with gate.exclusive():
            writer_ran.set()

    writer_task = asyncio.create_task(writer())
    await asyncio.sleep(0.02)
    assert not writer_ran.is_set()  # blocked by the in-flight reader, as designed
    reader_release.set()  # drain the in-flight reader
    await asyncio.wait_for(writer_ran.wait(), timeout=1.0)  # writer proceeds, no deadlock
    queued.cancel()
    await asyncio.gather(held, writer_task, queued, return_exceptions=True)


class TestPluginToolDefault:
    async def test_plugin_tool_without_supports_parallel_runs_exclusively(
        self, tmp_path: Path
    ) -> None:
        """Unflagged plugin/MCP tools default to exclusive so same-step mutation ordering
        stays deterministic — mirrors blackbox partitionToolCalls isConcurrencySafe default."""
        events: list[tuple[str, str]] = []
        plugin = _RecordingTool("MyPlugin", events, parallel=False)
        toolset = _toolset(plugin, cwd=tmp_path)

        tasks = []
        for index in range(2):
            result = toolset.handle(
                ToolCall(
                    id=f"tc_{index}",
                    function=ToolCall.FunctionBody(
                        name="MyPlugin",
                        arguments=json.dumps({"n": index}),
                    ),
                )
            )
            assert isinstance(result, asyncio.Task)
            tasks.append(result)
        await asyncio.gather(*tasks)

        assert events == [
            ("enter", "MyPlugin"),
            ("exit", "MyPlugin"),
            ("enter", "MyPlugin"),
            ("exit", "MyPlugin"),
        ]
