"""Session-wide live output-token accumulator.

Ports the reference design (``bootstrap/state.ts``: ``getTotalOutputTokens`` /
``getTurnOutputTokens`` / ``snapshotOutputTokensForTurn``): a single global total
that every in-process soul — root, subagents, background — increments as it
completes LLM steps. The live spinner reads the per-turn delta so the "↓ N tokens"
readout keeps moving during streaming, tool calls, and subagent runs instead of
freezing on the per-step context-size snapshot.

ponytail: plain module-level ints — every soul shares one asyncio event loop, so
there is no cross-thread race to guard against.
"""

from __future__ import annotations

_total_output_tokens: int = 0
_output_tokens_at_turn_start: int = 0


def add_total_output_tokens(count: int) -> None:
    """Add a completed step's output tokens to the session total."""
    global _total_output_tokens
    if count > 0:
        _total_output_tokens += count


def get_total_output_tokens() -> int:
    """Session-cumulative output tokens across all in-process agents."""
    return _total_output_tokens


def snapshot_output_tokens_for_turn() -> None:
    """Mark the start of a root turn so the live readout shows this turn's delta."""
    global _output_tokens_at_turn_start
    _output_tokens_at_turn_start = _total_output_tokens


def get_turn_output_tokens() -> int:
    """Output tokens produced since the current root turn began."""
    return max(0, _total_output_tokens - _output_tokens_at_turn_start)


def reset_for_tests() -> None:
    """Reset module state (test isolation only)."""
    global _total_output_tokens, _output_tokens_at_turn_start
    _total_output_tokens = 0
    _output_tokens_at_turn_start = 0
