"""SARIF 2.1.0 emitter."""

from __future__ import annotations

import json as _json
from typing import Any

from pythinker_review.store.models import Finding, RunMeta, Severity

_SEV_TO_LEVEL: dict[Severity, str] = {
    Severity.critical: "error",
    Severity.high: "error",
    Severity.medium: "warning",
    Severity.low: "note",
    Severity.info: "note",
}


def render_sarif(meta: RunMeta, findings: list[Finding]) -> str:
    rules_seen: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for finding in findings:
        rules_seen.setdefault(
            finding.rule_id,
            {
                "id": finding.rule_id,
                "shortDescription": {"text": finding.title[:60]},
                "fullDescription": {"text": finding.title},
                "defaultConfiguration": {"level": _SEV_TO_LEVEL[finding.severity]},
            },
        )
        results.append(
            {
                "ruleId": finding.rule_id,
                "level": _SEV_TO_LEVEL[finding.severity],
                "message": {"text": finding.rationale},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": finding.location.file},
                            "region": {
                                "startLine": finding.location.start_line,
                                "endLine": finding.location.end_line,
                            },
                        }
                    }
                ],
                "properties": {
                    "category": finding.category.value,
                    "severity": finding.severity.value,
                    "confidence": finding.confidence,
                    "pass": finding.pass_,
                    "run_id": meta.id,
                    "confidence_reason": finding.confidence_reason,
                    "exploitability": finding.exploitability,
                    "reproduction": finding.reproduction,
                    "test_analysis": finding.test_analysis,
                    "suggested_regression_test": finding.suggested_regression_test,
                    "minimum_fix_scope": finding.minimum_fix_scope,
                },
            }
        )
    doc: dict[str, Any] = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "pythinker-review",
                        "informationUri": "https://github.com/Pythoughts-labs/pythinker-cli",
                        "rules": list(rules_seen.values()),
                    }
                },
                "results": results,
                "invocations": [
                    {
                        "executionSuccessful": meta.status
                        in ("completed", "completed_with_warnings"),
                        "exitCodeDescription": meta.status,
                    }
                ],
            }
        ],
    }
    return _json.dumps(doc, indent=2)
