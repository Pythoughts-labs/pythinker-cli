from __future__ import annotations

import asyncio
import hashlib
import math
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, cast

import mcp
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from pythinker_core.tooling import CallableTool, HandleResult, ToolOk, ToolReturnValue
from pythinker_core.utils.typing import JsonType

from pythinker_code.soul.toolset import MCPServerInfo, MCPTool, PythinkerToolset
from pythinker_code.wire.types import ToolCall, ToolResult

if TYPE_CHECKING:
    from fastmcp import Client as FastMcpClient
    from fastmcp.client.transports.config import MCPConfigTransport

    from pythinker_code.auth.oauth import OAuthManager
    from pythinker_code.config import Config
    from pythinker_code.session import Session
    from pythinker_code.soul.agent import Runtime
    from pythinker_code.soul.toolset import ToolType

type ScenarioKind = Literal[
    "execution_safe",
    "execution_exclusive",
    "execution_mixed",
    "dedupe",
    "advertisement",
    "mcp",
]

_UNMEASURED_EXECUTION_PHASES = (
    "lookup_suggestion",
    "json_parse_canonicalize",
    "deduplication",
    "permission_approval",
    "pre_hook",
    "post_hook_reminder",
    "telemetry_wire",
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class EnvironmentSnapshot(_FrozenModel):
    python_version: str
    python_implementation: str
    platform: str

    @classmethod
    def current(cls) -> EnvironmentSnapshot:
        return cls(
            python_version=sys.version.split()[0],
            python_implementation=platform.python_implementation(),
            platform=platform.platform(),
        )


class FixtureShape(_FrozenModel):
    kind: ScenarioKind
    size: int = Field(gt=0)
    concurrency: int = Field(gt=0)
    payload_bytes: int | None = Field(default=None, gt=0)
    composition: str = ""


class PhaseSamples(_FrozenModel):
    measurement_status: Literal["measured", "unmeasured"] = "measured"
    reason: str | None = None
    samples_ns: tuple[int, ...]
    median_ns: int | None
    p95_ns: int | None
    throughput_per_second: float | None

    @classmethod
    def from_samples(
        cls,
        samples_ns: Sequence[int],
        *,
        operations_per_sample: int = 1,
    ) -> PhaseSamples:
        if not samples_ns:
            raise ValueError("phase samples must not be empty")
        if operations_per_sample < 1:
            raise ValueError("operations_per_sample must be positive")
        normalized = tuple(int(sample) for sample in samples_ns)
        if any(sample < 0 for sample in normalized):
            raise ValueError("phase samples must be non-negative")
        ordered = sorted(normalized)
        median_ns = int(statistics.median(ordered))
        p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
        throughput = operations_per_sample * 1_000_000_000 / median_ns if median_ns else None
        return cls(
            samples_ns=normalized,
            median_ns=median_ns,
            p95_ns=ordered[p95_index],
            throughput_per_second=throughput,
        )

    @classmethod
    def unmeasured(cls, reason: str) -> PhaseSamples:
        return cls(
            measurement_status="unmeasured",
            reason=reason,
            samples_ns=(),
            median_ns=None,
            p95_ns=None,
            throughput_per_second=None,
        )


class CancellationResult(_FrozenModel):
    completed: bool
    completion_ns: int | None = Field(default=None, ge=0)
    queued_reader_completed: bool = True
    queued_writer_completed: bool = True
    recovery_completed: bool = True


class LeakSnapshot(_FrozenModel):
    tasks: int = Field(ge=0)
    processes: int = Field(ge=0)
    sessions: int = Field(ge=0)


class ScenarioResult(_FrozenModel):
    fixture: FixtureShape
    warmups: int = Field(ge=0)
    iterations: int = Field(gt=0)
    phases: dict[str, PhaseSamples]
    registry_hash: str
    allocation_peak_bytes: int = Field(ge=0)
    retained_object_delta: int
    cancellation: CancellationResult
    leaks: LeakSnapshot
    task_count_peak: int = Field(ge=0)
    operation_count: int = Field(ge=0)
    category_counts: dict[str, int]
    projection_counts: dict[str, int]
    lifecycle_status: Literal["completed", "settled"]


class ThresholdState(StrEnum):
    CROSSED = "crossed"
    UNCROSSED = "uncrossed"
    INCONCLUSIVE = "inconclusive"


class ThresholdDecision(_FrozenModel):
    name: str
    threshold: float
    values: tuple[float, float, float, float, float]
    rerun_values: tuple[float, float, float, float, float] | None
    crossing_count: int = Field(ge=0, le=5)
    median: float
    primary_state: ThresholdState
    rerun_state: ThresholdState | None
    state: ThresholdState
    rerun_required: bool


class CharacterizationReport(_FrozenModel):
    schema_version: Literal[1] = 1
    environment: EnvironmentSnapshot
    scenarios: tuple[ScenarioResult, ...]
    decisions: tuple[ThresholdDecision, ...]


def evaluate_threshold(
    *,
    name: str,
    threshold: float,
    values: Sequence[float],
    rerun_values: Sequence[float] | None = None,
) -> ThresholdDecision:
    if not math.isfinite(threshold):
        raise ValueError("threshold must be finite")
    primary = _five_values(values)
    primary_state, primary_count, primary_median = _evaluate_five(primary, threshold)
    if rerun_values is not None and primary_state is not ThresholdState.INCONCLUSIVE:
        raise ValueError("rerun_values are only valid after an inconclusive primary run")

    selected = primary
    state = primary_state
    crossing_count = primary_count
    median = primary_median
    normalized_rerun: tuple[float, float, float, float, float] | None = None
    rerun_state: ThresholdState | None = None
    if rerun_values is not None:
        normalized_rerun = _five_values(rerun_values)
        selected = normalized_rerun
        state, crossing_count, median = _evaluate_five(selected, threshold)
        rerun_state = state

    return ThresholdDecision(
        name=name,
        threshold=threshold,
        values=primary,
        rerun_values=normalized_rerun,
        crossing_count=crossing_count,
        median=median,
        primary_state=primary_state,
        rerun_state=rerun_state,
        state=state,
        rerun_required=state is ThresholdState.INCONCLUSIVE and normalized_rerun is None,
    )


def _five_values(values: Sequence[float]) -> tuple[float, float, float, float, float]:
    if len(values) != 5:
        raise ValueError("threshold evaluation requires exactly five measured runs")
    normalized = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in normalized):
        raise ValueError("threshold values must be finite")
    return cast(tuple[float, float, float, float, float], normalized)


def _evaluate_five(
    values: tuple[float, float, float, float, float], threshold: float
) -> tuple[ThresholdState, int, float]:
    crossing_count = sum(value > threshold for value in values)
    median = float(statistics.median(values))
    if crossing_count >= 4 and median > threshold:
        return ThresholdState.CROSSED, crossing_count, median
    if crossing_count == 1:
        return ThresholdState.INCONCLUSIVE, crossing_count, median
    return ThresholdState.UNCROSSED, crossing_count, median


def deterministic_registry_hash(names: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for name in names:
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def interval_union_duration_ns(intervals: Iterable[tuple[int, int]]) -> int:
    ordered = sorted(intervals)
    if not ordered:
        return 0
    total = 0
    current_start, current_end = ordered[0]
    if current_end < current_start:
        raise ValueError("interval end must not precede its start")
    for start, end in ordered[1:]:
        if end < start:
            raise ValueError("interval end must not precede its start")
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        total += current_end - current_start
        current_start, current_end = start, end
    return total + current_end - current_start


def fixture_matrix(*, smoke: bool = False) -> tuple[FixtureShape, ...]:
    execution_sizes = (1,) if smoke else (1, 10, 100)
    dedupe_sizes = (1024,) if smoke else (1024, 100 * 1024, 1024 * 1024)
    advertisement_sizes = (50,) if smoke else (50, 500, 5000)
    mcp_sizes = (1,) if smoke else (1, 10, 50)
    fixtures: list[FixtureShape] = []
    for kind in ("execution_safe", "execution_exclusive", "execution_mixed"):
        fixtures.extend(
            FixtureShape(
                kind=kind,
                size=size,
                concurrency=size * 2 if kind == "execution_mixed" else size,
                composition=(
                    "reader/writer pairs"
                    if kind == "execution_mixed"
                    else kind.removeprefix("execution_")
                ),
            )
            for size in execution_sizes
        )
    fixtures.extend(
        FixtureShape(
            kind="dedupe",
            size=size,
            concurrency=2,
            payload_bytes=size,
            composition="same-step duplicate payload",
        )
        for size in dedupe_sizes
    )
    fixtures.extend(
        FixtureShape(
            kind="advertisement",
            size=size,
            concurrency=1,
            composition="builtin/plugin/MCP-style names with hidden entries",
        )
        for size in advertisement_sizes
    )
    fixtures.extend(
        FixtureShape(
            kind="mcp",
            size=size,
            concurrency=size,
            composition="deterministic fake server inventory publication",
        )
        for size in mcp_sizes
    )
    return tuple(fixtures)


class _NoopTool(CallableTool):
    supports_parallel: ClassVar[bool] = True
    _intervals_ns: list[tuple[int, int]] = PrivateAttr(
        default_factory=lambda: list[tuple[int, int]]()
    )

    @property
    def intervals_ns(self) -> tuple[tuple[int, int], ...]:
        return tuple(self._intervals_ns)

    async def __call__(self, **_kwargs: object) -> ToolReturnValue:
        started = time.monotonic_ns()
        result = ToolOk(output="ok")
        self._intervals_ns.append((started, time.monotonic_ns()))
        return result


class _ExclusiveNoopTool(_NoopTool):
    supports_parallel: ClassVar[bool] = False


class _BarrierTool(_NoopTool):
    _entered: asyncio.Event = PrivateAttr()
    _release: asyncio.Event = PrivateAttr()

    def configure(self, *, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._entered = entered
        self._release = release

    async def __call__(self, **_kwargs: object) -> ToolReturnValue:
        started = time.monotonic_ns()
        self._entered.set()
        try:
            await self._release.wait()
            return ToolOk(output="released")
        finally:
            self._intervals_ns.append((started, time.monotonic_ns()))


class _ExclusiveBarrierTool(_BarrierTool):
    supports_parallel: ClassVar[bool] = False


class _ExecutionProbeToolset(PythinkerToolset):
    def __init__(self) -> None:
        super().__init__()
        self.gate_wait_intervals_ns: list[tuple[str, int, int]] = []
        self.gate_requested: dict[str, asyncio.Event] = {}

    async def _gated_call(self, tool: ToolType, arguments: JsonType) -> ToolReturnValue:
        self.gate_requested.setdefault(tool.name, asyncio.Event()).set()
        requested_ns = time.monotonic_ns()
        if getattr(tool, "supports_parallel", False):
            async with self._concurrency_gate.shared():
                admitted_ns = time.monotonic_ns()
                self.gate_wait_intervals_ns.append((tool.name, requested_ns, admitted_ns))
                return await tool.call(arguments)
        async with self._concurrency_gate.exclusive():
            admitted_ns = time.monotonic_ns()
            self.gate_wait_intervals_ns.append((tool.name, requested_ns, admitted_ns))
            return await tool.call(arguments)


def _new_tool(name: str, *, parallel: bool) -> _NoopTool:
    tool_type = _NoopTool if parallel else _ExclusiveNoopTool
    return tool_type(
        name=name,
        description="Deterministic local characterization no-op.",
        parameters={
            "type": "object",
            "properties": {
                "index": {"type": "integer"},
                "payload": {"type": "string"},
            },
            "additionalProperties": False,
        },
    )


async def run_characterization(
    *,
    scenario: str = "all",
    runs: int = 5,
    warmups: int = 1,
    smoke: bool = False,
) -> CharacterizationReport:
    if scenario not in {"all", "execution", "dedupe", "advertisement", "mcp"}:
        raise ValueError(f"unknown scenario: {scenario}")
    if runs < 1:
        raise ValueError("runs must be positive")
    if warmups < 0:
        raise ValueError("warmups must be non-negative")

    fixtures = tuple(
        fixture
        for fixture in fixture_matrix(smoke=smoke)
        if scenario == "all" or _scenario_group(fixture.kind) == scenario
    )
    results = [await _measure_fixture(fixture, runs=runs, warmups=warmups) for fixture in fixtures]
    return CharacterizationReport(
        environment=EnvironmentSnapshot.current(),
        scenarios=tuple(results),
        decisions=(),
    )


def _scenario_group(kind: ScenarioKind) -> str:
    return "execution" if kind.startswith("execution_") else kind


async def _measure_fixture(fixture: FixtureShape, *, runs: int, warmups: int) -> ScenarioResult:
    measure = _measurement_for(fixture)
    for _ in range(warmups):
        await measure(fixture)

    before_tasks = _pending_task_count()
    phase_samples: dict[str, list[int]] = {}
    hashes: list[str] = []
    task_count_peak = 0
    operation_count = 0
    category_counts: dict[str, int] = {}
    projection_counts: dict[str, int] = {}
    peak_bytes = 0
    retained_delta = 0
    tracing_was_active = tracemalloc.is_tracing()
    if not tracing_was_active:
        tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        baseline_current, _ = tracemalloc.get_traced_memory()
        for _ in range(runs):
            sample = await measure(fixture)
            hashes.append(sample.registry_hash)
            task_count_peak = max(task_count_peak, sample.task_count)
            operation_count += sample.operation_count
            category_counts = sample.category_counts
            projection_counts = sample.projection_counts
            for phase, duration_ns in sample.phases.items():
                phase_samples.setdefault(phase, []).append(duration_ns)
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        retained_delta = current_bytes - baseline_current
    finally:
        if not tracing_was_active:
            tracemalloc.stop()

    if len(set(hashes)) != 1:
        raise RuntimeError(f"non-deterministic registry order for {fixture.kind}:{fixture.size}")
    cancellation = (
        await _measure_cancellation()
        if fixture.kind.startswith("execution_")
        else CancellationResult(completed=True, completion_ns=0)
    )
    leaked_tasks = max(0, _pending_task_count() - before_tasks)
    operations = operation_count // runs
    summarized_phases = {
        name: PhaseSamples.from_samples(samples, operations_per_sample=operations)
        for name, samples in phase_samples.items()
    }
    if fixture.kind.startswith("execution_") or fixture.kind == "dedupe":
        for name in _UNMEASURED_EXECUTION_PHASES:
            summarized_phases[name] = PhaseSamples.unmeasured(
                "current Toolset exposes no stable boundary for this subphase"
            )
    return ScenarioResult(
        fixture=fixture,
        warmups=warmups,
        iterations=runs,
        phases=summarized_phases,
        registry_hash=hashes[0],
        allocation_peak_bytes=peak_bytes,
        retained_object_delta=retained_delta,
        cancellation=cancellation,
        leaks=LeakSnapshot(tasks=leaked_tasks, processes=0, sessions=0),
        task_count_peak=task_count_peak,
        operation_count=operation_count,
        category_counts=category_counts,
        projection_counts=projection_counts,
        lifecycle_status="settled" if fixture.kind == "mcp" else "completed",
    )


@dataclass(frozen=True, slots=True)
class _MeasurementSample:
    phases: dict[str, int]
    registry_hash: str
    task_count: int
    operation_count: int
    category_counts: dict[str, int]
    projection_counts: dict[str, int]


type _Measure = Callable[[FixtureShape], Awaitable[_MeasurementSample]]


def _measurement_for(fixture: FixtureShape) -> _Measure:
    if fixture.kind.startswith("execution_") or fixture.kind == "dedupe":
        return _measure_execution
    if fixture.kind == "advertisement":
        return _measure_advertisement
    return _measure_mcp_publication


async def _measure_execution(fixture: FixtureShape) -> _MeasurementSample:
    toolset = _ExecutionProbeToolset()
    tools, holder_entered, holder_release = _execution_tools(fixture)
    for tool in tools:
        toolset.add(tool)
    toolset.begin_step([])
    payload = "x" * (fixture.payload_bytes or 0)
    calls = _execution_calls(fixture, tools, payload)

    started = time.monotonic_ns()
    pending: list[HandleResult]
    if holder_entered is not None and holder_release is not None:
        holder_result = toolset.handle(calls[0])
        await holder_entered.wait()
        queued_result = toolset.handle(calls[1])
        await toolset.gate_requested.setdefault(tools[1].name, asyncio.Event()).wait()
        pending = [holder_result, queued_result, *(toolset.handle(call) for call in calls[2:])]
        task_count_peak = _pending_task_count()
        holder_release.set()
    else:
        pending = [toolset.handle(call) for call in calls]
        task_count_peak = _pending_task_count()
    await asyncio.gather(*(_await_handle(result) for result in pending))
    end_to_end_ns = time.monotonic_ns() - started
    tool_intervals = tuple(interval for tool in tools for interval in tool.intervals_ns)
    tool_duration_ns = interval_union_duration_ns(tool_intervals)
    gate_wait_ns = interval_union_duration_ns(
        tuple((requested, admitted) for _, requested, admitted in toolset.gate_wait_intervals_ns)
    )
    framework_overhead_ns = max(0, end_to_end_ns - tool_duration_ns)
    registry_hash = deterministic_registry_hash(tool.name for tool in toolset.tools)
    return _MeasurementSample(
        phases={
            "end_to_end": end_to_end_ns,
            "read_write_gate_wait": gate_wait_ns,
            "tool_call": tool_duration_ns,
            "framework_overhead": framework_overhead_ns,
        },
        registry_hash=registry_hash,
        task_count=task_count_peak,
        operation_count=len(tool_intervals),
        category_counts={"builtin": len(tools)},
        projection_counts={"visible": len(toolset.tools)},
    )


def _execution_tools(
    fixture: FixtureShape,
) -> tuple[tuple[_NoopTool, ...], asyncio.Event | None, asyncio.Event | None]:
    if fixture.kind == "execution_mixed" and fixture.concurrency >= 2:
        entered = asyncio.Event()
        release = asyncio.Event()
        holder = _BarrierTool(
            name="SafeNoop",
            description="Deterministic contended reader holder.",
            parameters={
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "payload": {"type": "string"},
                },
                "additionalProperties": False,
            },
        )
        holder.configure(entered=entered, release=release)
        return (holder, _new_tool("ExclusiveNoop", parallel=False)), entered, release
    return (
        (_new_tool("Noop", parallel=fixture.kind != "execution_exclusive"),),
        None,
        None,
    )


def _execution_calls(
    fixture: FixtureShape, tools: tuple[_NoopTool, ...], payload: str
) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index in range(fixture.concurrency):
        tool = tools[index % len(tools)]
        argument_index = 0 if fixture.kind == "dedupe" else index
        calls.append(
            ToolCall(
                id=f"characterization-{index}",
                function=ToolCall.FunctionBody(
                    name=tool.name,
                    arguments=_arguments_json(argument_index, payload),
                ),
            )
        )
    return calls


def _arguments_json(index: int, payload: str) -> str:
    import json

    return json.dumps({"index": index, "payload": payload}, separators=(",", ":"))


async def _await_handle(result: HandleResult) -> ToolResult:
    if isinstance(result, ToolResult):
        return result
    return await result


class _AdvertisementProbeToolset(PythinkerToolset):
    def publish_mcp_tools(
        self,
        runtime: Runtime,
        tools: list[MCPTool[MCPConfigTransport]],
        client: FastMcpClient[MCPConfigTransport],
    ) -> None:
        self._mcp_servers["benchmark"] = MCPServerInfo(
            status="connected",
            client=client,
            tools=tools,
            resources=[],
            prompts=[],
        )
        self._rebuild_published_mcp_tools(runtime)


async def _measure_advertisement(fixture: FixtureShape) -> _MeasurementSample:
    from fastmcp import Client
    from fastmcp.mcp_config import MCPConfig

    from pythinker_code.soul.agent import Runtime

    with tempfile.TemporaryDirectory(prefix="pythinker-advertisement-") as session_dir:
        runtime_config, oauth, session = _benchmark_runtime_inputs(Path(session_dir))
        runtime = await Runtime.create(
            runtime_config,
            oauth,
            None,
            session,
            yolo=True,
            skills_dirs=[],
        )
        client = Client(
            MCPConfig.model_validate(
                {"mcpServers": {"benchmark": {"command": "pythinker-characterization-fake"}}}
            )
        )
        enabled = _AdvertisementProbeToolset(runtime)
        disabled = PythinkerToolset()
        mcp_tools, category_counts = _populate_advertisement_tools(
            fixture,
            runtime=runtime,
            client=client,
            enabled=enabled,
            disabled=disabled,
        )
        rebuild_started = time.monotonic_ns()
        enabled.publish_mcp_tools(runtime, mcp_tools, client)
        rebuild_ns = time.monotonic_ns() - rebuild_started
        for tool in mcp_tools:
            disabled.add(tool)
        phases, projections = _measure_visibility_projections(enabled, disabled, fixture.size)
        phases["rebuild_after_mcp_publication"] = rebuild_ns
        registry_hash = _advertisement_hash(enabled, disabled)
    return _MeasurementSample(
        phases=phases,
        registry_hash=registry_hash,
        task_count=_pending_task_count(),
        operation_count=fixture.size,
        category_counts=category_counts,
        projection_counts=projections,
    )


def _populate_advertisement_tools(
    fixture: FixtureShape,
    *,
    runtime: Runtime,
    client: FastMcpClient[MCPConfigTransport],
    enabled: PythinkerToolset,
    disabled: PythinkerToolset,
) -> tuple[list[MCPTool[MCPConfigTransport]], dict[str, int]]:
    from pythinker_code.plugin import PluginToolSpec
    from pythinker_code.plugin.tool import PluginTool

    mcp_tools: list[MCPTool[MCPConfigTransport]] = []
    counts = {"builtin": 0, "plugin": 0, "mcp": 0}
    for index in range(fixture.size):
        category = ("builtin", "plugin", "mcp")[index % 3]
        name = f"Characterization_{category}_{index:05d}"
        if category == "builtin":
            tool: ToolType = _new_tool(name, parallel=True)
        elif category == "plugin":
            tool = PluginTool(
                PluginToolSpec(
                    name=name,
                    description="Local advertisement fixture.",
                    command=["false"],
                ),
                Path.cwd(),
                inject={},
                config=runtime.config,
            )
        else:
            raw_tool = mcp.Tool(name=name, description="Local MCP fixture.", inputSchema={})
            mcp_tool = MCPTool(
                "benchmark",
                raw_tool,
                client,
                runtime=runtime,
            )
            mcp_tools.append(mcp_tool)
            counts[category] += 1
            continue
        enabled.add(tool)
        disabled.add(tool)
        counts[category] += 1
    return mcp_tools, counts


def _measure_visibility_projections(
    enabled: PythinkerToolset,
    disabled: PythinkerToolset,
    fixture_size: int,
) -> tuple[dict[str, int], dict[str, int]]:
    hidden_names = tuple(
        f"Characterization_{('builtin', 'plugin', 'mcp')[index % 3]}_{index:05d}"
        for index in range(0, fixture_size, 10)
    )
    for name in hidden_names:
        enabled.hide(name)
        disabled.hide(name)
    phases: dict[str, int] = {}
    projections: dict[str, int] = {}
    for label, toolset in (("enabled", enabled), ("disabled", disabled)):
        started = time.monotonic_ns()
        hidden = toolset.tools
        phases[f"visibility_{label}_hidden"] = time.monotonic_ns() - started
        projections[f"{label}_hidden"] = len(hidden)
        for name in hidden_names:
            toolset.unhide(name)
        started = time.monotonic_ns()
        unhidden = toolset.tools
        phases[f"visibility_{label}_unhidden"] = time.monotonic_ns() - started
        projections[f"{label}_unhidden"] = len(unhidden)
    started = time.monotonic_ns()
    repeated_projection = enabled.tools
    for _ in range(3):
        repeated_projection = enabled.tools
    phases["repeated_unchanged_projection"] = time.monotonic_ns() - started
    projections["rebuild"] = len(enabled.tools)
    projections["repeated"] = len(repeated_projection)
    return phases, projections


def _advertisement_hash(enabled: PythinkerToolset, disabled: PythinkerToolset) -> str:
    return deterministic_registry_hash(
        (
            *(tool.name for tool in enabled.tools),
            "--visibility-disabled--",
            *(tool.name for tool in disabled.tools),
        )
    )


class _BenchmarkMcpToolset(PythinkerToolset):
    def __init__(self) -> None:
        super().__init__()
        self.lifecycle_started_ns = 0
        self.publication_ns: int | None = None
        self.publication_visible_count = 0

    async def _connect_mcp_server(
        self, server_name: str, server_info: MCPServerInfo, runtime: Runtime
    ) -> tuple[str, Exception | None]:
        raw_tool = mcp.Tool(
            name=f"{server_name}_noop",
            description="Deterministic local MCP characterization no-op.",
            inputSchema={"type": "object", "properties": {}},
        )
        server_info.tools = [MCPTool(server_name, raw_tool, server_info.client, runtime=runtime)]
        server_info.status = "connected"
        return server_name, None

    def _publish_connected_mcp_tools(self, runtime: Runtime) -> None:
        super()._publish_connected_mcp_tools(runtime)
        if self.publication_ns is None and self.tools:
            self.publication_ns = time.monotonic_ns()
            self.publication_visible_count = len(self.tools)


async def _measure_mcp_publication(fixture: FixtureShape) -> _MeasurementSample:
    from fastmcp.mcp_config import MCPConfig

    from pythinker_code.soul.agent import Runtime

    server_map = {
        f"server_{index:03d}": {"command": "pythinker-characterization-fake"}
        for index in range(fixture.size)
    }
    config = MCPConfig.model_validate({"mcpServers": server_map})
    with tempfile.TemporaryDirectory(prefix="pythinker-toolset-") as session_dir:
        runtime_config, oauth, session = _benchmark_runtime_inputs(Path(session_dir))
        startup_started_ns = time.monotonic_ns()
        runtime = await Runtime.create(
            runtime_config,
            oauth,
            None,
            session,
            yolo=True,
            skills_dirs=[],
        )
        toolset = _BenchmarkMcpToolset()
        toolset.lifecycle_started_ns = time.monotonic_ns()
        await toolset.load_mcp_tools([config], runtime, in_background=True)
        task_count_peak = _pending_task_count()
        await toolset.wait_for_mcp_tools()
        settled_ns = time.monotonic_ns()
        names = tuple(tool.name for tool in toolset.tools)
        cleanup_started_ns = time.monotonic_ns()
        await toolset.cleanup()
        cleanup_ns = time.monotonic_ns() - cleanup_started_ns
    publication_ns = toolset.publication_ns
    if publication_ns is None:
        raise RuntimeError("fake MCP inventory was not published")
    return _MeasurementSample(
        phases={
            "startup_to_ready": settled_ns - startup_started_ns,
            "mcp_lifecycle": settled_ns - toolset.lifecycle_started_ns,
            "time_to_first_inventory": publication_ns - toolset.lifecycle_started_ns,
            "time_to_settled_inventory": settled_ns - toolset.lifecycle_started_ns,
            "cleanup": cleanup_ns,
        },
        registry_hash=deterministic_registry_hash(names),
        task_count=task_count_peak,
        operation_count=len(names),
        category_counts={"mcp": len(names)},
        projection_counts={
            "visible": len(names),
            "visible_at_first_publication": toolset.publication_visible_count,
        },
    )


def _benchmark_runtime_inputs(session_dir: Path) -> tuple[Config, OAuthManager, Session]:
    from pythinker_host import get_current_host
    from pythinker_host.path import HostPath

    from pythinker_code.auth.oauth import OAuthManager
    from pythinker_code.config import get_default_config
    from pythinker_code.metadata import WorkDirMeta
    from pythinker_code.session import Session
    from pythinker_code.session_state import SessionState
    from pythinker_code.wire.file import WireFile

    work_dir_path = session_dir / "work"
    work_dir_path.mkdir()
    work_dir = HostPath.unsafe_from_local_path(work_dir_path)
    config = get_default_config()
    config.plugins.discover_external = False
    session = Session(
        id="toolset-characterization",
        work_dir=work_dir,
        work_dir_meta=WorkDirMeta(path=str(work_dir), host=get_current_host().name),
        context_file=session_dir / "context.jsonl",
        wire_file=WireFile(path=session_dir / "wire.jsonl"),
        state=SessionState(),
        title="Toolset characterization",
        updated_at=0.0,
    )
    return config, OAuthManager(config), session


async def _measure_cancellation() -> CancellationResult:
    started = time.monotonic_ns()
    queued_reader_completed, reader_recovered = await _cancel_queued_call(
        holder_parallel=False,
        queued_parallel=True,
    )
    queued_writer_completed, writer_recovered = await _cancel_queued_call(
        holder_parallel=True,
        queued_parallel=False,
    )
    recovery_completed = reader_recovered and writer_recovered
    return CancellationResult(
        completed=queued_reader_completed and queued_writer_completed and recovery_completed,
        completion_ns=time.monotonic_ns() - started,
        queued_reader_completed=queued_reader_completed,
        queued_writer_completed=queued_writer_completed,
        recovery_completed=recovery_completed,
    )


async def _cancel_queued_call(*, holder_parallel: bool, queued_parallel: bool) -> tuple[bool, bool]:
    toolset = _ExecutionProbeToolset()
    entered = asyncio.Event()
    release = asyncio.Event()
    holder_type = _BarrierTool if holder_parallel else _ExclusiveBarrierTool
    holder = holder_type(
        name="Holder",
        description="Cancellation characterization holder.",
        parameters={"type": "object", "properties": {}},
    )
    holder.configure(entered=entered, release=release)
    queued = _new_tool("Queued", parallel=queued_parallel)
    toolset.add(holder)
    toolset.add(queued)
    holder_result = toolset.handle(_tool_call("holder", "Holder", 0))
    await entered.wait()
    gate_requested = toolset.gate_requested.setdefault("Queued", asyncio.Event())
    queued_result = toolset.handle(_tool_call("queued", "Queued", 1))
    await gate_requested.wait()
    queued_completed = await _cancel_handle_result(queued_result)
    release.set()
    await _await_handle(holder_result)
    recovery = toolset.handle(_tool_call("recovery", "Queued", 2))
    recovered = not (await _await_handle(recovery)).return_value.is_error
    return queued_completed, recovered


async def _cancel_handle_result(result: HandleResult) -> bool:
    if isinstance(result, ToolResult):
        return False
    result.cancel()
    try:
        await result
    except asyncio.CancelledError:
        return True
    return False


def _tool_call(call_id: str, name: str, index: int) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(name=name, arguments=_arguments_json(index, "")),
    )


def _pending_task_count() -> int:
    current = asyncio.current_task()
    return sum(task is not current and not task.done() for task in asyncio.all_tasks())


__all__ = [
    "CancellationResult",
    "CharacterizationReport",
    "EnvironmentSnapshot",
    "FixtureShape",
    "LeakSnapshot",
    "PhaseSamples",
    "ScenarioResult",
    "ThresholdDecision",
    "ThresholdState",
    "deterministic_registry_hash",
    "evaluate_threshold",
    "fixture_matrix",
    "run_characterization",
]
