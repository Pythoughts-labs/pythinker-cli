from __future__ import annotations

from dataclasses import dataclass

from pythinker_code.benchmark.suites import load_suite
from pythinker_code.benchmark.tasks import load_task


@dataclass(frozen=True, slots=True)
class BenchmarkEstimate:
    model_key: str
    target_kind: str
    target_name: str
    task_count: int
    repeat: int
    prompt_tokens: int
    output_tokens: int
    estimated_cost: str


def estimate_benchmark(
    *,
    model_key: str,
    task_id: str | None,
    suite_name: str | None,
    repeat: int,
) -> BenchmarkEstimate:
    if task_id is not None:
        tasks = [load_task(task_id)]
        target_kind = "Task"
        target_name = task_id
    else:
        suite = load_suite(suite_name or "pythinker-smoke")
        tasks = [load_task(tid) for tid in suite.tasks]
        target_kind = "Suite"
        target_name = suite.name
    prompt_chars = sum(len(task.prompt) for task in tasks)
    prompt_tokens = max(1, prompt_chars // 4) * repeat
    output_tokens = 800 * len(tasks) * repeat
    return BenchmarkEstimate(
        model_key=model_key,
        target_kind=target_kind,
        target_name=target_name,
        task_count=len(tasks),
        repeat=repeat,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        estimated_cost="unavailable for this model",
    )


def render_estimate(estimate: BenchmarkEstimate) -> str:
    return "\n".join(
        [
            "Pythinker Benchmark estimate",
            "",
            f"Model: {estimate.model_key}",
            f"{estimate.target_kind}: {estimate.target_name}",
            f"Tasks: {estimate.task_count}",
            f"Repeat: {estimate.repeat}",
            f"Estimated prompt tokens: {estimate.prompt_tokens:,}",
            f"Estimated output tokens: {estimate.output_tokens:,}",
            f"Estimated cost: {estimate.estimated_cost}",
            "",
            "No model calls were made.",
        ]
    )
