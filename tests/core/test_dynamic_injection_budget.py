from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from pythinker_code.config import Config, LLMModel
from pythinker_code.soul.agent import Runtime
from pythinker_code.soul.dynamic_injection import (
    ContextBudget,
    InjectionCandidate,
    collect_within_budget,
    injection_budget_from_runtime,
)


def test_collect_within_budget_orders_by_priority_and_caps():
    out = collect_within_budget(
        [
            InjectionCandidate(type="low", content="l" * 20, priority=1),
            InjectionCandidate(type="high", content="h" * 20, priority=10),
        ],
        budget_tokens=20,
    )
    assert [item.type for item in out] == ["high", "low"]
    assert sum(item.token_estimate or 0 for item in out) <= 20


def test_collect_within_budget_preserves_equal_priority_order_past_single_digits():
    candidates = [
        InjectionCandidate(type=f"candidate-{index}", content="x" * 4) for index in range(12)
    ]

    out = collect_within_budget(candidates, budget_tokens=20)

    assert [candidate.type for candidate in out] == [candidate.type for candidate in candidates]


def test_collect_within_budget_truncates_deterministically():
    out = collect_within_budget(
        [InjectionCandidate(type="x", content="alpha\nbeta\ngamma" * 100, priority=10)],
        budget_tokens=10,
    )
    assert len(out) == 1
    assert out[0].content.endswith("…")
    assert (out[0].token_estimate or 0) <= 10


def test_collect_within_budget_recomputes_untrusted_candidate_estimate():
    out = collect_within_budget(
        [
            InjectionCandidate(
                type="understated",
                content="x" * 40,
                priority=10,
                token_estimate=1,
            )
        ],
        budget_tokens=5,
    )

    assert len(out) == 1
    assert out[0].content.endswith("…")
    assert out[0].token_estimate == 5


def test_failed_first_truncation_attempt_prevents_lower_candidate_truncation():
    out = collect_within_budget(
        [
            InjectionCandidate(type="empty-prefix", content="\n" * 20, priority=20),
            InjectionCandidate(type="lower", content="lower candidate" * 10, priority=10),
        ],
        budget_tokens=1,
    )

    assert out == []


def test_context_budget_uses_ceiling_and_available_context():
    assert (
        ContextBudget(
            max_context_tokens=10_000,
            reserved_context_tokens=9_000,
            injection_ceiling_tokens=2_048,
        ).injection_budget_tokens
        == 1_000
    )


def test_injection_budget_from_runtime_uses_config_values():
    config = Config()
    config.memory.injection_ceiling_tokens = 512
    config.loop_control.reserved_context_size = 1000
    runtime = SimpleNamespace(
        config=config,
        llm=SimpleNamespace(model_config=LLMModel(provider="p", model="m", max_context_size=4000)),
    )
    assert injection_budget_from_runtime(cast(Runtime, runtime)).injection_budget_tokens == 512
