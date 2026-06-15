"""Versioned eval-case schema + efficiency scoring (obs-eval-4, offline core).

Shared by ``tests_ai/eval_gate.py`` and ``tests_e2e/eval_schema.py`` so AI eval
harnesses do not cross-import between sibling test packages.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

EVAL_CASE_SCHEMA_VERSION = 1


class EfficiencyBudget(BaseModel):
    """Per-scenario ceilings; ``None`` means "do not gate on this metric"."""

    max_tool_calls: int | None = None
    max_total_tokens: int | None = None
    max_tool_errors: int | None = None
    max_steps: int | None = None


class EvalCase(BaseModel):
    """A versioned behavioral eval scenario."""

    schema_version: int = EVAL_CASE_SCHEMA_VERSION
    name: str
    query: str
    expected_tools: tuple[str, ...] = ()
    reference_outcome: str = ""
    budget: EfficiencyBudget = Field(default_factory=EfficiencyBudget)


class ObservedMetrics(BaseModel):
    """The efficiency triple observed for one scenario run."""

    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_errors: int = 0
    step_count: int = 0
    tools_used: tuple[str, ...] = ()

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class BudgetBreach(BaseModel):
    metric: str
    budget: int
    observed: int


class EvalVerdict(BaseModel):
    name: str
    passed: bool
    breaches: list[BudgetBreach] = Field(default_factory=list)
    missing_expected_tools: tuple[str, ...] = ()


def score_eval_case(case: EvalCase, observed: ObservedMetrics) -> EvalVerdict:
    """Score a scenario: within every set budget AND used every expected tool."""
    breaches: list[BudgetBreach] = []

    def _check(metric: str, budget: int | None, value: int) -> None:
        if budget is not None and value > budget:
            breaches.append(BudgetBreach(metric=metric, budget=budget, observed=value))

    _check("tool_calls", case.budget.max_tool_calls, observed.tool_calls)
    _check("total_tokens", case.budget.max_total_tokens, observed.total_tokens)
    _check("tool_errors", case.budget.max_tool_errors, observed.tool_errors)
    _check("step_count", case.budget.max_steps, observed.step_count)

    used = set(observed.tools_used)
    missing = tuple(tool for tool in case.expected_tools if tool not in used)
    return EvalVerdict(
        name=case.name,
        passed=not breaches and not missing,
        breaches=breaches,
        missing_expected_tools=missing,
    )
