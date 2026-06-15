"""subagent-2: child->parent token/cost roll-up helpers."""

from __future__ import annotations

import pytest
from pythinker_core.chat_provider import TokenUsage
from pythinker_core.tooling import ToolReturnValue
from pythinker_core.utils.typing import JsonType

import pythinker_code.subagents.usage as usage_mod
from pythinker_code.subagents.runner import _fail_with_usage
from pythinker_code.subagents.usage import (
    EXTRA_COST_USD,
    EXTRA_INPUT_TOKENS,
    EXTRA_OUTPUT_TOKENS,
    accumulate_usage,
    estimate_cost_usd,
    format_usage_lines,
    summarize_batch,
    usage_extras,
)

_UNKNOWN_MODEL = "totally-unknown-model-xyz"


def _usage(input_other: int, output: int, cr: int = 0, cw: int = 0) -> TokenUsage:
    return TokenUsage(
        input_other=input_other, output=output, input_cache_read=cr, input_cache_creation=cw
    )


def test_accumulate_usage_sums_all_fields() -> None:
    a = _usage(10, 5, cr=2, cw=1)
    b = _usage(20, 7, cr=3, cw=4)
    total = accumulate_usage(a, b)
    assert total.input_other == 30
    assert total.output == 12
    assert total.input_cache_read == 5
    assert total.input_cache_creation == 5
    # .input is the sum of all input components.
    assert total.input == 30 + 5 + 5


def test_format_usage_lines_tokens_only_for_unpriced_model() -> None:
    lines = format_usage_lines("child", _usage(100, 40), _UNKNOWN_MODEL)
    assert lines == ["child_tokens: 100 in / 40 out"]


def test_format_usage_lines_includes_cost_when_priced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage_mod, "estimate_cost_usd", lambda usage, model: 0.1234)
    lines = format_usage_lines("child", _usage(100, 40), "some-model")
    assert lines[0] == "child_tokens: 100 in / 40 out"
    assert lines[1] == "child_cost_usd: 0.1234"


def test_usage_extras_carries_tokens_and_cost() -> None:
    extras = usage_extras(_usage(100, 40, cr=10), _UNKNOWN_MODEL)
    assert extras[EXTRA_INPUT_TOKENS] == 110  # 100 + 10 cache read
    assert extras[EXTRA_OUTPUT_TOKENS] == 40
    assert extras[EXTRA_COST_USD] == 0.0


def _result(extras: dict[str, JsonType] | None) -> ToolReturnValue:
    return ToolReturnValue(is_error=False, output="x", message="", display=[], extras=extras)


def test_summarize_batch_sums_children() -> None:
    results = [
        _result({EXTRA_INPUT_TOKENS: 100, EXTRA_OUTPUT_TOKENS: 40}),
        _result({EXTRA_INPUT_TOKENS: 50, EXTRA_OUTPUT_TOKENS: 20, EXTRA_COST_USD: 0.05}),
    ]
    lines = summarize_batch(results)
    assert lines[0] == "total_child_tokens: 150 in / 60 out"
    assert lines[1] == "total_child_cost_usd: 0.0500"


def test_summarize_batch_counts_each_child_once_not_per_resume_line() -> None:
    """Resume metadata in the child body must not inflate token roll-up totals."""
    resumed = _result({EXTRA_INPUT_TOKENS: 80, EXTRA_OUTPUT_TOKENS: 20})
    fresh = _result({EXTRA_INPUT_TOKENS: 20, EXTRA_OUTPUT_TOKENS: 10})
    lines = summarize_batch([resumed, fresh])
    assert lines[0] == "total_child_tokens: 100 in / 30 out"


def test_summarize_batch_empty_when_no_usage() -> None:
    results = [_result(None), _result({"unrelated": 1})]
    assert summarize_batch(results) == []


def test_estimate_cost_usd_nonzero_for_priced_model() -> None:
    # Real pricing integration: a known priced model must produce a positive cost,
    # guarding against a regression that silently zeroes child cost.
    cost = estimate_cost_usd(_usage(1_000_000, 1_000_000), "claude-3-haiku-20240307")
    assert cost > 0


class _FakeSoul:
    cumulative_usage = TokenUsage(input_other=100, output=40)
    model_name = _UNKNOWN_MODEL


def test_output_writer_usage_emits_child_spend_lines(tmp_path) -> None:
    """The background runner surfaces a child's spend through the output writer (its
    results are fetched later via TaskOutput, so usage rides in the written transcript,
    matching the foreground runner's `child_tokens:` envelope)."""
    from pythinker_code.subagents.output import SubagentOutputWriter

    p = tmp_path / "out.log"
    p.write_text("", encoding="utf-8")
    writer = SubagentOutputWriter(p)
    writer.usage(format_usage_lines("child", _usage(100, 40), _UNKNOWN_MODEL))
    content = p.read_text(encoding="utf-8")
    assert "child_tokens: 100 in / 40 out" in content


def test_fail_with_usage_reports_spend_on_error() -> None:
    err = _fail_with_usage(_FakeSoul(), "boom", "Boom")  # type: ignore[arg-type]
    assert err.is_error
    assert err.brief == "Boom"
    # The failed child's spend rides along in both the message and the extras.
    assert "child_tokens: 100 in / 40 out" in err.message
    assert err.extras is not None
    assert err.extras[EXTRA_INPUT_TOKENS] == 100
    assert err.extras[EXTRA_OUTPUT_TOKENS] == 40


# ---------------------------------------------------------------------------
# aggregate_findings — batch-level RISKS/BLOCKERS roll-up
# ---------------------------------------------------------------------------

from pythinker_code.subagents.usage import aggregate_findings  # noqa: E402

_CHILD_A = """status: completed

### SUMMARY
Implemented the parser.

### RISKS
- Parser assumes UTF-8 input.

### BLOCKERS
None
"""

_CHILD_B = """status: completed

### SUMMARY
Wired the CLI flag.

### RISKS
- Parser assumes UTF-8 input.
- Flag collides with legacy alias.

### BLOCKERS
- Needs the new config key merged first.
"""


def test_aggregate_findings_collects_and_dedupes_risks_and_blockers() -> None:
    lines = aggregate_findings([("child-a", _CHILD_A), ("child-b", _CHILD_B)])
    text = "\n".join(lines)
    assert text.count("Parser assumes UTF-8 input.") == 1
    assert "Flag collides with legacy alias." in text
    assert "Needs the new config key merged first." in text
    assert "child-b" in text  # attribution for the blocker


def test_aggregate_findings_tolerates_free_text_children() -> None:
    lines = aggregate_findings([("child-a", "I just did the thing, no sections here.")])
    assert lines == []


def test_aggregate_findings_skips_none_text_blockers() -> None:
    # _CHILD_A reports "### BLOCKERS\nNone" — the placeholder must not emit a
    # blockers block, while the real RISKS finding still rolls up.
    lines = aggregate_findings([("child-a", _CHILD_A)])
    text = "\n".join(lines)
    assert "batch_blockers" not in text
    assert "batch_risks:" in text
    assert "Parser assumes UTF-8 input. [child-a]" in text


def test_aggregate_findings_skips_none_observed_marker() -> None:
    """`None observed.` is the empty-section marker the built-in agent report
    contracts document; it must not roll up as a finding."""
    report = "### RISKS\nNone observed.\n\n### BLOCKERS\nnone observed\n"
    assert aggregate_findings([("child-a", report)]) == []


def test_aggregate_findings_empty_batch() -> None:
    assert aggregate_findings([]) == []


def test_aggregate_findings_attributes_repeating_child_once() -> None:
    # A child listing the same bullet twice must be attributed once, not
    # rendered as "[child-a, child-a]".
    output = """### RISKS
- Parser assumes UTF-8 input.
- Parser assumes UTF-8 input.
"""
    lines = aggregate_findings([("child-a", output)])
    text = "\n".join(lines)
    assert "Parser assumes UTF-8 input. [child-a]" in text
    assert "child-a, child-a" not in text


def test_aggregate_findings_survives_unclosed_code_fence() -> None:
    # A child that opens a code fence and never closes it must not swallow
    # the sections that follow the malformed block.
    malformed = """### SUMMARY
Did the thing.

```python
print("oops, never closed")

### RISKS
- Fence was never closed.
"""
    lines = aggregate_findings([("child-a", malformed)])
    assert any("Fence was never closed." in line for line in lines)


def test_aggregate_findings_preserves_leading_cli_flags() -> None:
    report = """### RISKS
- --force flag bypasses validation.
* *args handling is fragile.
"""
    lines = aggregate_findings([("child-a", report)])
    text = "\n".join(lines)
    assert "--force flag bypasses validation." in text
    assert "*args handling is fragile." in text


def test_aggregate_findings_keeps_non_bulleted_marker_lines_intact() -> None:
    # Lines that merely START with "-"/"*" are content, not bullets: a bare
    # "--force ..." finding must not lose its first dash.
    report = """### RISKS
--force flag bypasses validation.
*args handling is fragile.
"""
    lines = aggregate_findings([("child-a", report)])
    text = "\n".join(lines)
    assert "--force flag bypasses validation." in text
    assert "*args handling is fragile." in text


def test_extract_section_ignores_headers_inside_code_fences():
    output = (
        "### RISKS\n"
        "- real risk\n"
        "```bash\n"
        "# BLOCKERS\n"
        "- not a finding, just a shell comment\n"
        "```\n"
        "- second real risk\n"
        "### BLOCKERS\n"
        "- real blocker\n"
    )
    from pythinker_code.subagents.usage import aggregate_findings

    lines = aggregate_findings([("child", output)])
    joined = "\n".join(lines)
    assert "real risk" in joined
    assert "second real risk" in joined
    assert "real blocker" in joined
    assert "shell comment" not in joined
