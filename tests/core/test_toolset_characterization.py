from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import pythinker_code.benchmark.toolset_characterization as characterization_mod
from pythinker_code.benchmark.toolset_characterization import (
    CancellationResult,
    CharacterizationReport,
    EnvironmentSnapshot,
    FixtureShape,
    LeakSnapshot,
    PhaseSamples,
    ScenarioKind,
    ScenarioResult,
    ThresholdState,
    _await_handle,
    _BarrierTool,
    _ExecutionProbeToolset,
    _measure_execution,
    _new_tool,
    _tool_call,
    build_threshold_decisions,
    deterministic_registry_hash,
    evaluate_threshold,
    fixture_matrix,
    interval_union_duration_ns,
    run_characterization,
)


def _decision_scenario(
    kind: ScenarioKind,
    size: int,
    phases: dict[str, tuple[int, ...]],
) -> ScenarioResult:
    return ScenarioResult(
        fixture=FixtureShape(kind=kind, size=size, concurrency=size),
        warmups=1,
        iterations=5,
        phases={name: PhaseSamples.from_samples(values) for name, values in phases.items()},
        registry_hash=deterministic_registry_hash((kind, str(size))),
        allocation_peak_bytes=0,
        retained_object_delta=0,
        cancellation=CancellationResult(completed=True, completion_ns=0),
        leaks=LeakSnapshot(tasks=0, processes=0, sessions=0),
        task_count_peak=0,
        operation_count=5,
        category_counts={},
        projection_counts={},
        lifecycle_status="settled" if kind == "mcp" else "completed",
    )


@pytest.fixture
def characterization_report() -> CharacterizationReport:
    return CharacterizationReport(
        environment=EnvironmentSnapshot.current(),
        scenarios=(
            ScenarioResult(
                fixture=FixtureShape(kind="execution_safe", size=1, concurrency=1),
                warmups=1,
                iterations=2,
                phases={
                    "framework_overhead": PhaseSamples.from_samples((10, 20)),
                    "tool_duration": PhaseSamples.from_samples((1, 1)),
                },
                registry_hash=deterministic_registry_hash(("Noop",)),
                allocation_peak_bytes=100,
                retained_object_delta=0,
                cancellation=CancellationResult(completed=True, completion_ns=10),
                leaks=LeakSnapshot(tasks=0, processes=0, sessions=0),
                task_count_peak=1,
                operation_count=1,
                category_counts={"builtin": 1},
                projection_counts={"visible": 1},
                lifecycle_status="completed",
            ),
        ),
        decisions=(),
    )


def test_threshold_crosses_only_with_four_of_five_and_crossing_median() -> None:
    decision = evaluate_threshold(
        name="registry_500_p95",
        threshold=5.0,
        values=(5.1, 5.2, 1.0, 5.3, 5.4),
    )

    assert decision.state is ThresholdState.CROSSED
    assert decision.crossing_count == 4
    assert decision.median == 5.2
    assert decision.rerun_required is False


def test_measured_short_safe_framework_overhead_records_crossed_threshold() -> None:
    decision = evaluate_threshold(
        name="execution_framework_overhead_percent_short_safe_size_1",
        threshold=10.0,
        values=(
            99.41801566579635,
            99.44035789159838,
            99.56835157490183,
            99.6366887606035,
            99.62501511204897,
        ),
    )

    assert decision.state is ThresholdState.CROSSED
    assert decision.median == 99.56835157490183


def test_decision_builder_derives_all_gates_from_raw_scenarios() -> None:
    scenarios = (
        _decision_scenario(
            "execution_safe",
            1,
            {
                "framework_overhead": (11, 12, 13, 14, 15),
                "end_to_end": (100, 100, 100, 100, 100),
            },
        ),
        _decision_scenario(
            "execution_mixed",
            10,
            {
                "read_write_gate_wait": (20, 20, 20, 20, 20),
                "end_to_end": (100, 100, 100, 100, 100),
            },
        ),
        _decision_scenario(
            "advertisement",
            500,
            {"registry_projection_p95": (4_000_000,) * 5},
        ),
        *(
            _decision_scenario(
                "mcp",
                size,
                {
                    "startup_to_ready": (100,) * 5,
                    "mcp_lifecycle": (10,) * 5,
                    "cleanup": (1_000_000_000,) * 5,
                },
            )
            for size in (1, 10, 50)
        ),
    )

    decisions = build_threshold_decisions(scenarios)

    assert [decision.name for decision in decisions] == [
        "execution_framework_overhead_percent_short_safe_size_1",
        "mcp_lifecycle_startup_percent_10_servers",
        "mcp_cleanup_seconds_1_servers",
        "mcp_cleanup_seconds_10_servers",
        "mcp_cleanup_seconds_50_servers",
        "registry_projection_p95_ms_500_tools",
        "mixed_gate_wait_end_to_end_percent_10_pairs",
    ]
    assert decisions[5].values == (4.0, 4.0, 4.0, 4.0, 4.0)
    assert decisions[5].state is ThresholdState.UNCROSSED


def test_phase_samples_preserve_raw_within_run_projection_samples() -> None:
    within_runs = (
        (1_000_000, 2_000_000, 3_000_000),
        (2_000_000, 3_000_000, 4_000_000),
    )

    phase = PhaseSamples.from_samples(
        (3_000_000, 4_000_000),
        within_run_samples_ns=within_runs,
    )

    assert phase.within_run_samples_ns == within_runs


async def test_full_five_run_all_builds_tracked_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenarios = (
        _decision_scenario(
            "execution_safe",
            1,
            {
                "framework_overhead": (11, 12, 13, 14, 15),
                "end_to_end": (100,) * 5,
            },
        ),
        _decision_scenario(
            "execution_mixed",
            10,
            {
                "read_write_gate_wait": (20,) * 5,
                "end_to_end": (100,) * 5,
            },
        ),
        _decision_scenario(
            "advertisement",
            500,
            {"registry_projection_p95": (4_000_000,) * 5},
        ),
        *(
            _decision_scenario(
                "mcp",
                size,
                {
                    "startup_to_ready": (100,) * 5,
                    "mcp_lifecycle": (10,) * 5,
                    "cleanup": (1_000_000_000,) * 5,
                },
            )
            for size in (1, 10, 50)
        ),
    )
    by_fixture = {
        (scenario.fixture.kind, scenario.fixture.size): scenario for scenario in scenarios
    }

    monkeypatch.setattr(
        characterization_mod,
        "fixture_matrix",
        lambda *, smoke=False: tuple(scenario.fixture for scenario in scenarios),
    )

    async def measure(fixture: FixtureShape, *, runs: int, warmups: int) -> ScenarioResult:
        assert runs == 5
        assert warmups == 1
        return by_fixture[(fixture.kind, fixture.size)]

    monkeypatch.setattr(characterization_mod, "_measure_fixture", measure)

    report = await run_characterization(scenario="all", runs=5)

    assert len(report.decisions) == 7
    assert report.decisions[5].name == "registry_projection_p95_ms_500_tools"


def test_threshold_rejects_nonfinite_threshold() -> None:
    with pytest.raises(ValueError, match="threshold must be finite"):
        evaluate_threshold(name="example", threshold=float("nan"), values=(1, 2, 3, 4, 5))


@pytest.mark.parametrize(
    "values",
    [
        (1.0, 2.0, 3.0, 4.0, 5.0),
        (1.0, 2.0, 6.0, 7.0, 8.0),
    ],
)
def test_threshold_is_uncrossed_without_repeatable_crossing(
    values: tuple[float, float, float, float, float],
) -> None:
    decision = evaluate_threshold(name="example", threshold=5.0, values=values)

    assert decision.state is ThresholdState.UNCROSSED
    assert decision.rerun_required is False


def test_single_crossing_outlier_requests_exactly_one_complete_rerun() -> None:
    first = evaluate_threshold(
        name="example",
        threshold=5.0,
        values=(1.0, 1.1, 1.2, 1.3, 50.0),
    )

    assert first.state is ThresholdState.INCONCLUSIVE
    assert first.rerun_required is True

    rerun = evaluate_threshold(
        name="example",
        threshold=5.0,
        values=(1.0, 1.1, 1.2, 1.3, 50.0),
        rerun_values=(1.0, 1.1, 1.2, 1.3, 75.0),
    )

    assert rerun.state is ThresholdState.INCONCLUSIVE
    assert rerun.primary_state is ThresholdState.INCONCLUSIVE
    assert rerun.rerun_state is ThresholdState.INCONCLUSIVE
    assert rerun.rerun_required is False
    assert rerun.rerun_values == (1.0, 1.1, 1.2, 1.3, 75.0)


def test_complete_rerun_replaces_an_inconclusive_primary_decision() -> None:
    decision = evaluate_threshold(
        name="example",
        threshold=5.0,
        values=(1.0, 1.1, 1.2, 1.3, 50.0),
        rerun_values=(6.0, 6.1, 6.2, 6.3, 1.0),
    )

    assert decision.state is ThresholdState.CROSSED
    assert decision.primary_state is ThresholdState.INCONCLUSIVE
    assert decision.rerun_state is ThresholdState.CROSSED
    assert decision.crossing_count == 4
    assert decision.median == 6.1
    assert decision.rerun_required is False


def test_evaluator_rejects_partial_primary_run() -> None:
    with pytest.raises(ValueError, match="exactly five"):
        evaluate_threshold(name="example", threshold=5.0, values=(1.0, 2.0))


def test_evaluator_rejects_rerun_without_inconclusive_primary() -> None:
    with pytest.raises(ValueError, match="only valid after an inconclusive"):
        evaluate_threshold(
            name="example",
            threshold=5.0,
            values=(6.0, 6.1, 6.2, 6.3, 6.4),
            rerun_values=(1.0, 1.1, 1.2, 1.3, 1.4),
        )


def test_fixture_matrix_covers_the_approved_directional_shapes() -> None:
    fixtures = fixture_matrix()

    assert {(item.kind, item.size) for item in fixtures} == {
        *(("execution_safe", size) for size in (1, 10, 100)),
        *(("execution_exclusive", size) for size in (1, 10, 100)),
        *(("execution_mixed", size) for size in (1, 10, 100)),
        *(("dedupe", size) for size in (1024, 100 * 1024, 1024 * 1024)),
        *(("advertisement", size) for size in (50, 500, 5000)),
        *(("mcp", size) for size in (1, 10, 50)),
    }
    assert {
        (item.kind, item.size): item.concurrency
        for item in fixtures
        if item.kind.startswith("execution_")
    } == {
        **{("execution_safe", size): size for size in (1, 10, 100)},
        **{("execution_exclusive", size): size for size in (1, 10, 100)},
        **{("execution_mixed", size): size * 2 for size in (1, 10, 100)},
    }


def test_registry_hash_is_order_sensitive_and_reproducible() -> None:
    first = deterministic_registry_hash(("Alpha", "Beta", "Gamma"))

    assert first == deterministic_registry_hash(("Alpha", "Beta", "Gamma"))
    assert first != deterministic_registry_hash(("Beta", "Alpha", "Gamma"))


def test_overlapping_tool_intervals_use_union_not_sum() -> None:
    assert interval_union_duration_ns(((10, 30), (20, 40), (50, 55))) == 35


def test_unmeasured_phase_is_explicit_not_zero() -> None:
    phase = PhaseSamples.unmeasured("no stable public phase boundary")

    assert phase.measurement_status == "unmeasured"
    assert phase.samples_ns == ()
    assert phase.median_ns is None
    assert phase.p95_ns is None
    assert phase.reason == "no stable public phase boundary"


def test_report_schema_contains_required_measurement_and_safety_fields() -> None:
    schema = CharacterizationReport.model_json_schema()
    scenario_schema = schema["$defs"]["ScenarioResult"]["properties"]
    phase_schema = schema["$defs"]["PhaseSamples"]["properties"]

    assert set(schema["properties"]) >= {"schema_version", "environment", "scenarios", "decisions"}
    assert set(scenario_schema) >= {
        "fixture",
        "warmups",
        "iterations",
        "phases",
        "registry_hash",
        "allocation_peak_bytes",
        "retained_object_delta",
        "cancellation",
        "leaks",
        "task_count_peak",
        "operation_count",
        "category_counts",
        "projection_counts",
        "lifecycle_status",
    }
    assert set(phase_schema) >= {
        "measurement_status",
        "reason",
        "samples_ns",
        "median_ns",
        "p95_ns",
        "throughput_per_second",
        "within_run_samples_ns",
    }


def test_report_json_is_machine_readable(characterization_report: CharacterizationReport) -> None:
    payload = json.loads(characterization_report.model_dump_json())

    assert payload["schema_version"] == 2
    assert payload["environment"]["python_version"]
    assert payload["scenarios"][0]["phases"]["framework_overhead"]["samples_ns"] == [10, 20]


async def test_execution_smoke_separates_tool_duration_from_framework_overhead() -> None:
    report = await run_characterization(scenario="execution", runs=1, smoke=True)

    assert [scenario.fixture.kind for scenario in report.scenarios] == [
        "execution_safe",
        "execution_exclusive",
        "execution_mixed",
    ]
    expected_phases = {
        "lookup_suggestion",
        "json_parse_canonicalize",
        "deduplication",
        "permission_approval",
        "pre_hook",
        "read_write_gate_wait",
        "tool_call",
        "post_hook_reminder",
        "telemetry_wire",
        "end_to_end",
        "framework_overhead",
    }
    for scenario in report.scenarios:
        assert set(scenario.phases) == expected_phases
        assert (
            scenario.phases["end_to_end"].samples_ns[0]
            >= scenario.phases["tool_call"].samples_ns[0]
        )
        assert scenario.phases["lookup_suggestion"].measurement_status == "unmeasured"
        expected_operations = (
            scenario.fixture.size * 2
            if scenario.fixture.kind == "execution_mixed"
            else scenario.fixture.size
        )
        assert scenario.operation_count == expected_operations
        assert scenario.cancellation.completed is True
        assert scenario.cancellation.queued_reader_completed is True
        assert scenario.cancellation.queued_writer_completed is True
        assert scenario.cancellation.recovery_completed is True
        assert scenario.leaks.tasks == 0

    mixed = report.scenarios[-1]
    assert mixed.fixture.size == 1
    assert mixed.fixture.concurrency == 2
    assert mixed.operation_count == 2
    assert mixed.phases["read_write_gate_wait"].samples_ns[0] > 0


async def test_gate_wait_measures_request_to_admission_before_tool_call() -> None:
    toolset = _ExecutionProbeToolset()
    entered = asyncio.Event()
    release = asyncio.Event()
    holder = _BarrierTool(
        name="Holder",
        description="Known reader holder.",
        parameters={"type": "object", "properties": {}},
    )
    holder.configure(entered=entered, release=release)
    queued = _new_tool("Queued", parallel=False)
    toolset.add(holder)
    toolset.add(queued)
    toolset.begin_step([])

    holder_result = toolset.handle(_tool_call("holder", "Holder", 0))
    await entered.wait()
    queued_result = toolset.handle(_tool_call("queued", "Queued", 1))
    await toolset.gate_requested.setdefault("Queued", asyncio.Event()).wait()

    assert queued.intervals_ns == ()
    released_ns = time.monotonic_ns()
    release.set()
    await asyncio.gather(_await_handle(holder_result), _await_handle(queued_result))

    _, requested_ns, admitted_ns = next(
        interval for interval in toolset.gate_wait_intervals_ns if interval[0] == "Queued"
    )
    tool_started_ns, tool_finished_ns = queued.intervals_ns[0]
    assert requested_ns < released_ns <= admitted_ns
    assert admitted_ns <= tool_started_ns < tool_finished_ns
    assert admitted_ns - requested_ns > 0


async def test_mixed_task_peak_samples_all_dispatched_operations() -> None:
    sample = await _measure_execution(FixtureShape(kind="execution_mixed", size=10, concurrency=20))

    assert sample.operation_count == 20
    assert sample.task_count >= 20


async def test_dedupe_smoke_executes_one_tool_for_two_identical_calls() -> None:
    report = await run_characterization(scenario="dedupe", runs=1, smoke=True)
    scenario = report.scenarios[0]

    assert scenario.fixture.payload_bytes == 1024
    assert scenario.fixture.concurrency == 2
    assert scenario.operation_count == 1
    assert scenario.phases["deduplication"].measurement_status == "unmeasured"


async def test_advertisement_smoke_measures_hidden_and_unhidden_projection() -> None:
    report = await run_characterization(scenario="advertisement", runs=2, smoke=True)
    scenario = report.scenarios[0]

    assert set(scenario.phases) == {
        "visibility_enabled_hidden",
        "visibility_enabled_unhidden",
        "visibility_disabled_hidden",
        "visibility_disabled_unhidden",
        "repeated_unchanged_projection",
        "registry_projection_p95",
        "rebuild_after_mcp_publication",
    }
    projection = scenario.phases["registry_projection_p95"]
    assert len(projection.within_run_samples_ns) == 2
    assert all(len(run) >= 5 for run in projection.within_run_samples_ns)
    assert all(scenario.category_counts[origin] > 0 for origin in ("builtin", "plugin", "mcp"))
    assert (
        scenario.projection_counts["enabled_hidden"]
        < scenario.projection_counts["enabled_unhidden"]
    )
    assert scenario.projection_counts["rebuild"] == scenario.fixture.size
    assert scenario.operation_count == scenario.fixture.size * 2


async def test_mcp_smoke_measures_current_background_lifecycle_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code.soul.agent import Runtime

    create_spy = AsyncMock(wraps=Runtime.create)
    monkeypatch.setattr(Runtime, "create", create_spy)
    report = await run_characterization(scenario="mcp", runs=1, warmups=0, smoke=True)
    scenario = report.scenarios[0]

    assert create_spy.await_count == 1
    assert set(scenario.phases) == {
        "startup_to_ready",
        "mcp_lifecycle",
        "time_to_first_inventory",
        "time_to_settled_inventory",
        "cleanup",
    }
    assert scenario.task_count_peak >= 1
    assert scenario.operation_count == scenario.fixture.size
    assert (
        scenario.phases["startup_to_ready"].samples_ns[0]
        >= scenario.phases["mcp_lifecycle"].samples_ns[0]
    )
    assert (
        scenario.phases["time_to_first_inventory"].samples_ns[0]
        <= scenario.phases["time_to_settled_inventory"].samples_ns[0]
    )
    assert scenario.projection_counts["visible"] == scenario.fixture.size
    assert scenario.projection_counts["visible_at_first_publication"] == scenario.fixture.size
    assert scenario.lifecycle_status == "settled"
    assert scenario.leaks == LeakSnapshot(tasks=0, processes=0, sessions=0)


def test_runner_help_documents_scenario_runs_and_output() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/benchmark_toolset.py", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert completed.returncode == 0
    assert "--scenario" in completed.stdout
    assert "--runs" in completed.stdout
    assert "--output" in completed.stdout


def test_runner_smoke_writes_json_without_network_or_secrets(tmp_path: Path) -> None:
    output = tmp_path / "toolset.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/benchmark_toolset.py",
            "--scenario",
            "advertisement",
            "--runs",
            "1",
            "--smoke",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert [item["fixture"]["size"] for item in payload["scenarios"]] == [50]
    assert payload["decisions"] == []


def test_runner_rejects_nonpositive_run_count() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/benchmark_toolset.py", "--runs", "0"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert completed.returncode != 0
    assert "must be positive" in completed.stderr
