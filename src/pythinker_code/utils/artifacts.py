"""Single source of truth for artifacts exchanged between coding subagents.

This module owns the typed schema, exact prompt contract block, and fail-closed extraction
of coding artifacts. Verifiers receive ONLY the typed fields declared here, never prose or
logs from the producer.

Two pairs are defined:

* ``CodingArtifact`` / ``VerificationResult`` for the generic coder-to-verifier flow.
* ``VulnerabilityArtifact`` / ``AuditVerdict`` for the security-specific finder-to-audit
  flow.
"""

from __future__ import annotations

import json
import re
from dataclasses import MISSING, dataclass, field, fields
from typing import Any, TypeGuard, cast

CODING_ARTIFACT_TAG: str = "coding_artifact"

_CODING_ARTIFACT_OPEN_TAG = f"<{CODING_ARTIFACT_TAG}>"
_CODING_ARTIFACT_CLOSE_TAG = f"</{CODING_ARTIFACT_TAG}>"
_CODING_ARTIFACT_PATTERN = re.compile(
    rf"(?:^|\r?\n){re.escape(_CODING_ARTIFACT_OPEN_TAG)}\r?\n"
    rf"(?P<body>.*?)\r?\n{re.escape(_CODING_ARTIFACT_CLOSE_TAG)}"
    rf"[ \t]*(?:\r?\n[ \t]*)*\Z",
    re.DOTALL,
)
_LOOSE_CODING_ARTIFACT_PATTERN = re.compile(
    rf"{re.escape(_CODING_ARTIFACT_OPEN_TAG)}(.*?){re.escape(_CODING_ARTIFACT_CLOSE_TAG)}",
    re.DOTALL,
)
_CODING_ARTIFACT_EXAMPLE_VALUES: tuple[object, ...] = (
    ["path/to/file.py"],
    "make test",
    "...",
    ["..."],
)
_MAX_MALFORMED_REASON_LENGTH = 120


class _DuplicateJSONKeyError(ValueError):
    """Raised when an artifact JSON object repeats a member name."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


@dataclass(frozen=True)
class CodingArtifact:
    """Producer-side handoff from a coder subagent to a verifier subagent."""

    files_changed: list[str]
    test_command: str
    expected_behavior: str
    edge_cases_claimed: list[str] = field(default_factory=list[str])

    def to_json(self) -> str:
        return json.dumps(
            {
                "files_changed": self.files_changed,
                "test_command": self.test_command,
                "expected_behavior": self.expected_behavior,
                "edge_cases_claimed": self.edge_cases_claimed,
            },
            indent=2,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CodingArtifact:
        return cls(
            files_changed=d["files_changed"],
            test_command=d["test_command"],
            expected_behavior=d["expected_behavior"],
            edge_cases_claimed=d.get("edge_cases_claimed", []),
        )


@dataclass(frozen=True)
class ExtractedCodingArtifact:
    """A valid coding artifact and the stripped JSON body that produced it."""

    artifact: CodingArtifact
    raw_body: str


@dataclass(frozen=True)
class MissingCodingArtifact:
    """No complete coding-artifact tag pair was present."""


@dataclass(frozen=True)
class MalformedCodingArtifact:
    """A coding-artifact tag was present, but its body violated the contract."""

    raw_body: str
    reason: str


type CodingArtifactExtraction = (
    ExtractedCodingArtifact | MissingCodingArtifact | MalformedCodingArtifact
)


def coding_artifact_contract_block() -> str:
    """Render the exact coding-artifact prompt block from the dataclass schema."""
    schema_fields = fields(CodingArtifact)
    if len(schema_fields) != len(_CODING_ARTIFACT_EXAMPLE_VALUES):
        raise RuntimeError("CodingArtifact example count does not match schema")

    example = {
        artifact_field.name: example_value
        for artifact_field, example_value in zip(
            schema_fields, _CODING_ARTIFACT_EXAMPLE_VALUES, strict=True
        )
    }
    example_lines = ["{"]
    for index, (field_name, example_value) in enumerate(example.items()):
        suffix = "," if index < len(example) - 1 else ""
        example_lines.append(f"  {json.dumps(field_name)}: {json.dumps(example_value)}{suffix}")
    example_lines.append("}")
    rendered_example = "\n".join(example_lines)
    optional_field_names = [
        artifact_field.name
        for artifact_field in schema_fields
        if artifact_field.default is not MISSING or artifact_field.default_factory is not MISSING
    ]
    optional_keys = ", ".join(f"`{name}`" for name in optional_field_names)
    optional_key_phrase = "key is" if len(optional_field_names) == 1 else "keys are"
    optional_key_pronoun = "it" if len(optional_field_names) == 1 else "them"

    return (
        "Artifact contract: Before finishing, you MUST emit your result as a structured artifact.\n"
        f"Wrap it in <{CODING_ARTIFACT_TAG}> tags on its own line at the very end of your final "
        "message:\n\n"
        f"<{CODING_ARTIFACT_TAG}>\n"
        f"{rendered_example}\n"
        f"</{CODING_ARTIFACT_TAG}>\n\n"
        "Do not include reasoning, logs, or intermediate output inside the tags — only the JSON "
        "fields above.\n"
        "`test_command` is the exact verification command you actually ran, verbatim — never an "
        "aspirational one.\n"
        f"The {optional_keys} {optional_key_phrase} optional; omit {optional_key_pronoun} if you "
        "have no distinct edge cases to claim."
    )


def extract_coding_artifact(text: str) -> CodingArtifactExtraction:
    """Extract one final tagged coding artifact, failing closed on contract violations."""
    opening_tag_count = text.count(_CODING_ARTIFACT_OPEN_TAG)
    closing_tag_count = text.count(_CODING_ARTIFACT_CLOSE_TAG)
    if opening_tag_count == 0 and closing_tag_count == 0:
        return MissingCodingArtifact()

    loose_match = _LOOSE_CODING_ARTIFACT_PATTERN.search(text)
    loose_raw_body = loose_match.group(1).strip() if loose_match is not None else ""
    if opening_tag_count != 1 or closing_tag_count != 1:
        return _malformed_coding_artifact(
            loose_raw_body,
            "Expected exactly one complete coding_artifact block",
        )

    match = _CODING_ARTIFACT_PATTERN.search(text)
    if match is None:
        return _malformed_coding_artifact(
            loose_raw_body,
            "Artifact tags must be on their own lines at the end of the message",
        )

    raw_body = match.group("body").strip()
    try:
        parsed: object = json.loads(raw_body, object_pairs_hook=_reject_duplicate_json_keys)
    except _DuplicateJSONKeyError as exc:
        return _malformed_coding_artifact(raw_body, f"Duplicate JSON key: {exc.key}")
    except json.JSONDecodeError as exc:
        return _malformed_coding_artifact(raw_body, f"Invalid JSON: {exc.msg}")
    except (RecursionError, ValueError):
        return _malformed_coding_artifact(raw_body, "Invalid JSON: parser limit exceeded")

    if not isinstance(parsed, dict):
        return _malformed_coding_artifact(raw_body, "Artifact JSON must be an object")

    payload = cast(dict[str, object], parsed)
    schema_fields = fields(CodingArtifact)
    schema_field_names = {artifact_field.name for artifact_field in schema_fields}
    if any(field_name not in schema_field_names for field_name in payload):
        return _malformed_coding_artifact(raw_body, "Artifact JSON contains undeclared keys")

    required_field_names = [
        artifact_field.name
        for artifact_field in schema_fields
        if artifact_field.default is MISSING and artifact_field.default_factory is MISSING
    ]
    for field_name in required_field_names:
        if field_name not in payload:
            return _malformed_coding_artifact(raw_body, f"Missing required key: {field_name}")

    files_changed = payload["files_changed"]
    test_command = payload["test_command"]
    expected_behavior = payload["expected_behavior"]
    edge_cases_claimed = payload.get("edge_cases_claimed", [])
    if not _is_string_list(files_changed):
        return _malformed_coding_artifact(raw_body, "files_changed must be a list of strings")
    if not isinstance(test_command, str):
        return _malformed_coding_artifact(raw_body, "test_command must be a string")
    if not isinstance(expected_behavior, str):
        return _malformed_coding_artifact(raw_body, "expected_behavior must be a string")
    if not _is_string_list(edge_cases_claimed):
        return _malformed_coding_artifact(raw_body, "edge_cases_claimed must be a list of strings")

    return ExtractedCodingArtifact(
        artifact=CodingArtifact(
            files_changed=files_changed,
            test_command=test_command,
            expected_behavior=expected_behavior,
            edge_cases_claimed=edge_cases_claimed,
        ),
        raw_body=raw_body,
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateJSONKeyError(key)
        payload[key] = value
    return payload


def _is_string_list(value: object) -> TypeGuard[list[str]]:
    if not isinstance(value, list):
        return False
    items = cast(list[object], value)
    return all(isinstance(item, str) for item in items)


def _malformed_coding_artifact(raw_body: str, reason: str) -> MalformedCodingArtifact:
    return MalformedCodingArtifact(
        raw_body=raw_body,
        reason=reason[:_MAX_MALFORMED_REASON_LENGTH],
    )


@dataclass(frozen=True)
class VerificationResult:
    """Verifier-side response back to the coder."""

    passed: bool
    stdout_summary: str
    stderr_summary: str
    discovered_gaps: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class VulnerabilityArtifact:
    """Producer-side handoff from a vulnerability finder to the audit verifier."""

    target_file: str
    vulnerability_type: str
    reproduction_command: str
    expected_failure_output: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "target_file": self.target_file,
                "vulnerability_type": self.vulnerability_type,
                "reproduction_command": self.reproduction_command,
                "expected_failure_output": self.expected_failure_output,
            },
            indent=2,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VulnerabilityArtifact:
        return cls(
            target_file=d["target_file"],
            vulnerability_type=d["vulnerability_type"],
            reproduction_command=d["reproduction_command"],
            expected_failure_output=d["expected_failure_output"],
        )


@dataclass(frozen=True)
class AuditVerdict:
    """Audit-verifier verdict on a claimed vulnerability."""

    vulnerability_confirmed: bool
    execution_logs: str
    false_positive_reasoning: str = ""
