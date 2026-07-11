from __future__ import annotations

import hashlib
from collections.abc import Awaitable
from pathlib import Path
from typing import cast

import pytest
from pythinker_core.tooling.empty import EmptyToolset

from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.soul.request_assembly import (
    FragmentOutcome,
    FragmentPersistence,
    FragmentRequirement,
    FragmentStatus,
    RequestManifest,
    RequestStatus,
)
from pythinker_code.soul.slash import registry as soul_slash_registry
from pythinker_code.telemetry import metrics
from pythinker_code.wire.types import TextPart


def _make_soul(runtime: Runtime, tmp_path: Path) -> PythinkerSoul:
    agent = Agent(
        name="manifest test",
        system_prompt="Static system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    return PythinkerSoul(agent, context=Context(file_backend=tmp_path / "context.jsonl"))


async def _run_prompt_manifest(soul: PythinkerSoul) -> None:
    command = soul_slash_registry.find_command("prompt-manifest")
    assert command is not None
    pending = command.func(soul, "")
    if isinstance(pending, Awaitable):
        await pending


def _manifest(
    status: RequestStatus,
    *,
    reason_code: str | None = None,
    outcome_status: FragmentStatus = FragmentStatus.INCLUDED,
    source: str = "permissions_state",
    key: str = "permissions",
    outcome_reason: str | None = None,
) -> RequestManifest:
    return RequestManifest(
        status=status,
        reason_code=reason_code,
        outcomes=(
            FragmentOutcome(
                key=key,
                source=source,
                requirement=FragmentRequirement.REQUIRED,
                persistence=FragmentPersistence.REQUEST_ONLY,
                status=outcome_status,
                estimated_tokens=12,
                admitted_tokens=8,
                reason_code=outcome_reason,
            ),
        ),
        budget_tokens=128,
        budgeted_admitted_tokens=8,
        non_budgeted_estimated_tokens=4,
    )


def _opaque_identifier(identifier: str) -> str:
    identifier_bytes = identifier.encode(encoding="utf-8")
    return f"id:{hashlib.sha256(identifier_bytes).hexdigest()[:16]}"


@pytest.fixture
def sent_text(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []
    monkeypatch.setattr(
        "pythinker_code.soul.slash.wire_send",
        lambda part: captured.append(cast(TextPart, part).text),
    )
    return captured


@pytest.mark.asyncio
async def test_prompt_manifest_before_first_assembly_reports_no_data(
    runtime: Runtime,
    tmp_path: Path,
    sent_text: list[str],
) -> None:
    soul = _make_soul(runtime, tmp_path)

    await _run_prompt_manifest(soul)

    assert sent_text == ["No request has been assembled in this session."]


@pytest.mark.parametrize(
    ("status", "reason_code", "outcome_status", "outcome_reason"),
    [
        (RequestStatus.SUCCEEDED, None, FragmentStatus.INCLUDED, None),
        (
            RequestStatus.DEGRADED,
            "optional_source_degraded",
            FragmentStatus.DEGRADED,
            "source_unavailable",
        ),
        (
            RequestStatus.FAILED,
            "required_source_failed",
            FragmentStatus.FAILED,
            "permissions_state_unavailable",
        ),
    ],
)
@pytest.mark.asyncio
async def test_prompt_manifest_renders_safe_status_and_accounting(
    runtime: Runtime,
    tmp_path: Path,
    sent_text: list[str],
    status: RequestStatus,
    reason_code: str | None,
    outcome_status: FragmentStatus,
    outcome_reason: str | None,
) -> None:
    soul = _make_soul(runtime, tmp_path)
    soul.latest_request_manifest = _manifest(
        status,
        reason_code=reason_code,
        outcome_status=outcome_status,
        outcome_reason=outcome_reason,
    )

    await _run_prompt_manifest(soul)

    rendered = sent_text[0]
    assert status.value.upper() in rendered
    assert f"{_opaque_identifier('permissions')} " in rendered
    assert f"[{_opaque_identifier('permissions_state')}]" in rendered
    assert "permissions [permissions_state]" not in rendered
    assert "required" in rendered
    assert "request_only" in rendered
    assert outcome_status.value in rendered
    assert "estimated=12" in rendered
    assert "admitted=8" in rendered
    assert "limit=128" in rendered
    assert "budgeted_admitted=8" in rendered
    assert "non_budgeted_estimated=4" in rendered
    if reason_code is not None:
        assert reason_code in rendered
    if outcome_reason is not None:
        assert outcome_reason in rendered


@pytest.mark.asyncio
async def test_prompt_manifest_redacts_untrusted_identifiers(
    runtime: Runtime,
    tmp_path: Path,
    sent_text: list[str],
) -> None:
    soul = _make_soul(runtime, tmp_path)
    secret = "sk-proj-" + "a" * 24
    private_path = "/Users/alice/private/request.txt"
    soul.latest_request_manifest = _manifest(
        RequestStatus.FAILED,
        reason_code=f"traceback:{private_path}",
        source=private_path,
        key=secret,
        outcome_status=FragmentStatus.FAILED,
        outcome_reason=f"credential={secret}",
    )

    await _run_prompt_manifest(soul)

    rendered = sent_text[0]
    assert private_path not in rendered
    assert secret not in rendered
    assert "traceback" not in rendered
    assert "<redacted>" in rendered


@pytest.mark.asyncio
async def test_prompt_manifest_redacts_embedded_credentials_from_reason_codes(
    runtime: Runtime,
    tmp_path: Path,
    sent_text: list[str],
) -> None:
    embedded_secret = "prefix-sk-proj-" + "e" * 24 + "-suffix"
    soul = _make_soul(runtime, tmp_path)
    soul.latest_request_manifest = _manifest(
        RequestStatus.FAILED,
        reason_code=embedded_secret,
        outcome_status=FragmentStatus.FAILED,
        outcome_reason=embedded_secret,
    )

    await _run_prompt_manifest(soul)

    rendered = sent_text[0]
    assert embedded_secret not in rendered
    assert rendered.count("<redacted>") == 2


@pytest.mark.parametrize(
    ("key", "source"),
    [
        (
            "plugin-sk-proj-" + "a" * 24 + "-suffix",
            "prefix-ghp_" + "b" * 24 + "-suffix",
        ),
        ("user_alice_request_20260711", "provider_controlled_plugin_alpha"),
    ],
)
@pytest.mark.asyncio
async def test_prompt_manifest_uses_opaque_ids_for_provider_controlled_metadata(
    runtime: Runtime,
    tmp_path: Path,
    sent_text: list[str],
    key: str,
    source: str,
) -> None:
    soul = _make_soul(runtime, tmp_path)
    soul.latest_request_manifest = _manifest(
        RequestStatus.SUCCEEDED,
        key=key,
        source=source,
    )

    await _run_prompt_manifest(soul)

    rendered = sent_text[0]
    assert key not in rendered
    assert source not in rendered
    assert _opaque_identifier(key) in rendered
    assert _opaque_identifier(source) in rendered


def test_request_manifest_metrics_record_only_sanitized_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[tuple[float, dict[str, object]]] = []

    class RecordingHistogram:
        def record(self, amount: float, attributes: dict[str, object]) -> None:
            records.append((amount, attributes))

    monkeypatch.setattr(
        metrics,
        "request_assembly_duration_seconds",
        RecordingHistogram(),
        raising=False,
    )
    record_manifest = getattr(metrics, "record_request_assembly", None)
    assert record_manifest is not None
    embedded_secret_source = "plugin-sk-proj-" + "c" * 24 + "-suffix"
    user_derived_source = "user_alice_request_20260711"
    suffixed_secret_source = "prefix-ghp_" + "d" * 24 + "-suffix"
    manifest = RequestManifest(
        status=RequestStatus.DEGRADED,
        reason_code="optional_source_degraded",
        outcomes=(
            FragmentOutcome(
                key="permissions",
                source=embedded_secret_source,
                requirement=FragmentRequirement.REQUIRED,
                persistence=FragmentPersistence.REQUEST_ONLY,
                status=FragmentStatus.INCLUDED,
                estimated_tokens=12,
                admitted_tokens=12,
                reason_code=None,
            ),
            FragmentOutcome(
                key="plan",
                source=user_derived_source,
                requirement=FragmentRequirement.BEST_EFFORT,
                persistence=FragmentPersistence.HISTORY,
                status=FragmentStatus.TRUNCATED,
                estimated_tokens=20,
                admitted_tokens=5,
                reason_code="budget_truncated",
            ),
            FragmentOutcome(
                key="git",
                source=suffixed_secret_source,
                requirement=FragmentRequirement.BEST_EFFORT,
                persistence=FragmentPersistence.REQUEST_ONLY,
                status=FragmentStatus.OMITTED_BUDGET,
                estimated_tokens=18,
                admitted_tokens=0,
                reason_code="budget_exhausted",
            ),
        ),
        budget_tokens=32,
        budgeted_admitted_tokens=17,
        non_budgeted_estimated_tokens=7,
    )

    record_manifest(manifest, duration_seconds=0.25)

    assert records == [
        (
            0.25,
            {
                "source_ids": (
                    _opaque_identifier(embedded_secret_source),
                    _opaque_identifier(user_derived_source),
                    _opaque_identifier(suffixed_secret_source),
                ),
                "required_count": 1,
                "optional_count": 2,
                "included_count": 1,
                "omitted_count": 1,
                "truncated_count": 1,
                "degraded_count": 0,
                "failed_count": 0,
                "budget_limit": 32,
                "budgeted_admitted_tokens": 17,
                "non_budgeted_estimated_tokens": 7,
            },
        )
    ]


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_change_successful_assembly(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _make_soul(runtime, tmp_path)
    telemetry_attempted = False

    def fail_telemetry(*_args: object, **_kwargs: object) -> None:
        nonlocal telemetry_attempted
        telemetry_attempted = True
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(metrics, "record_request_assembly", fail_telemetry, raising=False)

    prepared = await soul._assemble_request("Inspect the repository")

    assert telemetry_attempted is True
    assert prepared.assembled.manifest.status is RequestStatus.SUCCEEDED
