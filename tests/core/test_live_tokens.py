"""Unit tests for the session-wide live output-token accumulator."""

from __future__ import annotations

import pytest

from pythinker_code.soul.live_tokens import (
    add_total_output_tokens,
    get_total_output_tokens,
    get_turn_output_tokens,
    reset_for_tests,
    snapshot_output_tokens_for_turn,
)


@pytest.fixture(autouse=True)
def _isolate() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


def test_total_accumulates_across_sources() -> None:
    # Every in-process soul (main, subagent, background) funnels here.
    add_total_output_tokens(50)  # main agent step
    add_total_output_tokens(30)  # subagent step
    add_total_output_tokens(20)  # background step
    assert get_total_output_tokens() == 100


def test_turn_delta_excludes_pre_turn_tokens() -> None:
    add_total_output_tokens(100)  # produced before the turn began
    snapshot_output_tokens_for_turn()
    assert get_turn_output_tokens() == 0
    add_total_output_tokens(40)  # main + subagent work during the turn
    assert get_turn_output_tokens() == 40
    assert get_total_output_tokens() == 140


def test_turn_delta_never_negative_without_snapshot() -> None:
    # No snapshot taken: baseline is 0, delta tracks the total.
    add_total_output_tokens(25)
    assert get_turn_output_tokens() == 25


def test_non_positive_counts_are_ignored() -> None:
    add_total_output_tokens(10)
    add_total_output_tokens(0)
    add_total_output_tokens(-5)
    assert get_total_output_tokens() == 10
