"""Unit tests for the review findings parser (pure function, no Rich rendering).

Tests _parse_reviewer_findings and _aggregate_findings directly to verify that
severity counts are correct — something the renderer-level tests cannot check.
"""

from __future__ import annotations

from pythinker_code.ui.shell.tool_renderers.agent import (
    _aggregate_findings,
    _parse_reviewer_findings,
)

# ---------------------------------------------------------------------------
# _parse_reviewer_findings — exact count tests
# ---------------------------------------------------------------------------


def test_bracket_bullets_exact_counts():
    text = """\
## Findings
- [HIGH] Missing input validation
- [HIGH] SQL injection risk
- [MEDIUM] Weak error handling
"""
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is True
    assert counts == {"critical": 0, "high": 2, "medium": 1, "low": 0}


def test_bold_colon_bullets_exact_counts():
    text = """\
- **Critical**: Auth bypass via token reuse
- **High**: Unvalidated redirect
- **Low**: Missing cache-control header
"""
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is True
    assert counts == {"critical": 1, "high": 1, "medium": 0, "low": 1}


def test_plain_colon_bullets_exact_counts():
    text = """\
- Critical: session fixation
- Medium: insecure deserialization
"""
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is True
    assert counts == {"critical": 1, "high": 0, "medium": 1, "low": 0}


def test_bold_start_form_exact_counts():
    """Form 3: `**Severity**: description` at line start without a bullet."""
    text = """\
**Critical**: buffer overflow
**High**: format string bug
**High**: missing bounds check
"""
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is True
    assert counts == {"critical": 1, "high": 2, "medium": 0, "low": 0}


def test_severity_section_bullets_exact_counts():
    """Form 4: plain bullets inside a named-severity subsection."""
    text = """\
## Findings
### High Severity
- Token reuse vulnerability
- Missing TLS enforcement
### Low Severity
- Unused debug flag
"""
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is True
    assert counts == {"critical": 0, "high": 2, "medium": 0, "low": 1}


def test_markdown_table_rows_exact_counts():
    """Form 5: markdown table rows `| HIGH | description |`."""
    text = """\
| Severity | Finding |
|----------|---------|
| High | Missing CSRF token |
| Medium | Verbose error messages |
| High | SSRF via redirect |
"""
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is True
    assert counts == {"critical": 0, "high": 2, "medium": 1, "low": 0}


def test_empty_text_returns_unparsed():
    counts, was_parsed = _parse_reviewer_findings("")
    assert was_parsed is False
    assert counts == {"critical": 0, "high": 0, "medium": 0, "low": 0}


def test_prose_only_returns_unparsed_with_zero_counts():
    text = "The code looks fine with no major concerns. Good work overall."
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is False
    assert counts == {"critical": 0, "high": 0, "medium": 0, "low": 0}


def test_mid_sentence_severity_words_not_counted():
    """Severity words appearing mid-sentence must produce zero counts."""
    text = """\
This is not a high-risk change.
The overall risk is medium at most.
No critical vulnerabilities detected in this diff.
Low confidence that this will cause issues.
"""
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is False
    assert counts == {"critical": 0, "high": 0, "medium": 0, "low": 0}


def test_hyphenated_header_not_treated_as_severity_section():
    """'## High-level overview' must not set section_severity to 'high'.

    Without the `(?!-)` lookahead in _RE_SEVERITY_IN_HEADER, plain bullets
    following this header would be miscounted as 'high' findings.
    """
    text = """\
## High-level overview
- This is a general bullet
- Another general note
## Low-hanging fruit
- Easy win
"""
    counts, was_parsed = _parse_reviewer_findings(text)
    # No structured severity markers → unparsed
    assert was_parsed is False
    assert counts == {"critical": 0, "high": 0, "medium": 0, "low": 0}


def test_section_context_does_not_cross_into_next_non_severity_section():
    """Bullets after a non-severity header must not inherit prior section_severity."""
    text = """\
### High Severity
- Real finding
## Summary
- This is just a summary bullet
"""
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is True
    # Only the 'Real finding' bullet under '### High Severity' is counted
    assert counts == {"critical": 0, "high": 1, "medium": 0, "low": 0}


def test_mixed_forms_combined_counts():
    """All five forms appearing together sum correctly."""
    text = """\
## Findings
- [CRITICAL] Hardcoded secret
**High**: Buffer overflow
- **Medium**: Missing rate limit
### Low Severity
- Unused import
| High | XSS via innerHTML |
"""
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is True
    assert counts == {"critical": 1, "high": 2, "medium": 1, "low": 1}


# ---------------------------------------------------------------------------
# _aggregate_findings — exact reporters and counts
# ---------------------------------------------------------------------------


def _agent(
    name: str,
    subagent_type: str,
    result_text: str,
) -> dict[str, str]:
    return {
        "name": name,
        "subagent_type": subagent_type,
        "status": "completed",
        "result_text": result_text,
    }


def test_aggregate_single_reviewer_counts_and_reporters():
    agents = [
        _agent(
            "auth_review",
            "code-reviewer",
            "## Findings\n- [CRITICAL] Auth bypass\n- [HIGH] SSRF\n- [HIGH] Missing CSRF\n",
        )
    ]
    summary = _aggregate_findings(agents)
    assert summary.critical == 1
    assert summary.high == 2
    assert summary.medium == 0
    assert summary.low == 0
    assert summary.parsed_reports == 1
    assert summary.unparsed_reports == 0
    assert summary.total_reports == 1
    assert summary.reporters["critical"] == ["auth_review"]
    assert summary.reporters["high"] == ["auth_review"]


def test_aggregate_two_reviewers_reporters_per_severity():
    """Two reviewers — each reporter appears only in the rows where it has findings."""
    agents = [
        _agent("auth_review", "code-reviewer", "- [CRITICAL] Auth bypass\n"),
        _agent("api_review", "security-reviewer", "- [HIGH] SSRF\n- [HIGH] Missing CSRF\n"),
    ]
    summary = _aggregate_findings(agents)
    assert summary.critical == 1
    assert summary.high == 2
    assert summary.reporters["critical"] == ["auth_review"]
    assert summary.reporters["high"] == ["api_review"]
    assert summary.parsed_reports == 2
    assert summary.unparsed_reports == 0
    assert summary.total_reports == 2


def test_aggregate_unparsed_report_goes_to_unknown():
    agents = [
        _agent("structured", "code-reviewer", "- [MEDIUM] Missing validation\n"),
        _agent("prose_reviewer", "code-reviewer", "The code looks fine.\n"),
    ]
    summary = _aggregate_findings(agents)
    assert summary.medium == 1
    assert summary.parsed_reports == 1
    assert summary.unparsed_reports == 1
    assert summary.total_reports == 2
    assert "prose_reviewer" in summary.reporters["unknown"]
    assert "structured" not in summary.reporters["unknown"]


def test_aggregate_skips_non_reviewer_agents():
    """Implementer/coder agents must not feed the findings table."""
    agents = [
        _agent("impl", "implementer", "- [HIGH] I am not a review finding\n"),
        _agent("sec", "security-reviewer", "- [HIGH] Real finding\n"),
    ]
    summary = _aggregate_findings(agents)
    assert summary.high == 1
    assert summary.total_reports == 1
    assert summary.reporters["high"] == ["sec"]


def test_aggregate_empty_result_text_is_unparsed():
    agents = [_agent("empty_scan", "code-reviewer", "")]
    summary = _aggregate_findings(agents)
    assert summary.unparsed_reports == 1
    assert summary.parsed_reports == 0
    assert "empty_scan" in summary.reporters["unknown"]


# ---------------------------------------------------------------------------
# _parse_reviewer_findings — ```report JSON block (primary machine-readable format)
# ---------------------------------------------------------------------------


def test_report_block_counts_findings():
    text = (
        "Some analysis.\n"
        "```report\n"
        '{"findings": [\n'
        '  {"title": "Bug A", "severity": "high"},\n'
        '  {"title": "Bug B", "severity": "medium"},\n'
        '  {"title": "Bug C", "severity": "high"}\n'
        "]}\n"
        "```\n"
    )
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is True
    assert counts == {"critical": 0, "high": 2, "medium": 1, "low": 0}


def test_report_block_empty_findings_is_parsed():
    """{"findings": []} is a valid structured report — was_parsed must be True."""
    text = '```report\n{"findings": []}\n```\n'
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is True
    assert counts == {"critical": 0, "high": 0, "medium": 0, "low": 0}


def test_report_block_malformed_json_is_not_parsed():
    """A malformed ```report block must NOT report success — it is unparsed, so the
    caller treats it as an unparsed reviewer result rather than 'parsed, 0 findings'."""
    text = "```report\nnot valid json\n```\n"
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is False
    assert counts == {"critical": 0, "high": 0, "medium": 0, "low": 0}


def test_report_block_wrong_shape_is_not_parsed():
    """A ```report block holding a JSON array (not an object) is not a valid report."""
    text = '```report\n[{"severity": "high"}]\n```\n'
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is False
    assert counts["high"] == 0


def test_report_block_non_dict_findings_does_not_crash():
    """Untrusted payloads where 'findings' isn't a list of objects must not raise."""
    text = '```report\n{"findings": "high"}\n```\n'
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is True  # valid JSON object => structured, just no countable findings
    assert counts == {"critical": 0, "high": 0, "medium": 0, "low": 0}


def test_report_block_multiple_blocks_aggregate():
    block1 = '```report\n{"findings": [{"severity": "critical"}]}\n```\n'
    block2 = '```report\n{"findings": [{"severity": "low"}, {"severity": "low"}]}\n```\n'
    counts, was_parsed = _parse_reviewer_findings(block1 + block2)
    assert was_parsed is True
    assert counts == {"critical": 1, "high": 0, "medium": 0, "low": 2}


def test_report_block_with_markdown_markers_uses_json_only():
    """JSON block takes priority — markdown markers inside JSON must not be double-counted."""
    text = (
        '```report\n{"findings": [{"severity": "high", "description": "- [HIGH] inside JSON"}]}\n```\n'
        "- [HIGH] outside block\n"
    )
    # Only the JSON findings count; the markdown marker outside the block is not scanned
    # because the report block path returns early without running the markdown fallback.
    counts, was_parsed = _parse_reviewer_findings(text)
    assert was_parsed is True
    # JSON block found: 1 high from JSON. Markdown fallback is skipped entirely.
    assert counts["high"] == 1
