"""E2E eval schema: re-exports offline core + OTel metric reader helpers."""

from __future__ import annotations

from opentelemetry.sdk.metrics.export import (
    HistogramDataPoint,
    InMemoryMetricReader,
    NumberDataPoint,
)

from tests_ai.eval_schema import (
    EVAL_CASE_SCHEMA_VERSION,
    BudgetBreach,
    EfficiencyBudget,
    EvalCase,
    EvalVerdict,
    ObservedMetrics,
    score_eval_case,
)

__all__ = [
    "EVAL_CASE_SCHEMA_VERSION",
    "BudgetBreach",
    "EfficiencyBudget",
    "EvalCase",
    "EvalVerdict",
    "ObservedMetrics",
    "observed_from_metric_reader",
    "score_eval_case",
]


def _counter_total(reader: InMemoryMetricReader, name: str) -> int:
    data = reader.get_metrics_data()
    if data is None:
        return 0
    total = 0
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    if isinstance(point, NumberDataPoint):
                        total += int(point.value)
    return total


def _histogram_sum(reader: InMemoryMetricReader, name: str) -> int:
    data = reader.get_metrics_data()
    if data is None:
        return 0
    total = 0
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    if isinstance(point, HistogramDataPoint):
                        total += int(point.sum)
    return total


def observed_from_metric_reader(
    reader: InMemoryMetricReader, *, tools_used: tuple[str, ...] = ()
) -> ObservedMetrics:
    """Read the efficiency triple out of an in-process OTel metric reader."""
    return ObservedMetrics(
        tool_calls=_counter_total(reader, "pythinker.tool.calls_total"),
        input_tokens=_counter_total(reader, "pythinker.llm.input_tokens"),
        output_tokens=_counter_total(reader, "pythinker.llm.output_tokens"),
        tool_errors=_counter_total(reader, "pythinker.errors_total"),
        step_count=_histogram_sum(reader, "pythinker.turn.step_count"),
        tools_used=tools_used,
    )
