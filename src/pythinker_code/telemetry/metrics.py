"""Pre-bound OTel metric instruments for the agent loop.

Call sites stay declarative — they import the instruments and call ``.add()``
or ``.record()``. When telemetry is disabled, the instruments are still
present but back onto the no-op meter, so the cost is one attribute lookup
plus a no-op call.

Initialization happens automatically: ``otel.init()`` calls ``bind()`` once
the SDK MeterProvider is up. Until then, everything points at the global
no-op meter so unit tests and pre-init code paths stay safe.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import metrics as _metrics
from opentelemetry.metrics import Counter, Histogram, Meter

from pythinker_code.soul.request_assembly import (
    FragmentRequirement,
    FragmentStatus,
    RequestManifest,
)

# ---------------------------------------------------------------------------
# Module-level instrument handles
# ---------------------------------------------------------------------------
# Initialized lazily on first ``bind()``. Until then, the global no-op meter
# returns no-op instruments — accessing them is safe.

_meter: Meter = _metrics.get_meter("pythinker-code")

# --- Turn-level ---
turn_total: Counter = _meter.create_counter(
    "pythinker.turn.total",
    description="Number of agent turns completed (one user message → final response).",
    unit="1",
)
turn_duration_seconds: Histogram = _meter.create_histogram(
    "pythinker.turn.duration_seconds",
    description="End-to-end agent-turn duration.",
    unit="s",
)
turn_step_count: Histogram = _meter.create_histogram(
    "pythinker.turn.step_count",
    description="Number of inner steps (LLM calls + tool loops) per turn.",
    unit="1",
)
request_assembly_duration_seconds: Histogram = _meter.create_histogram(
    "pythinker.request_assembly.duration_seconds",
    description="Request assembly duration with sanitized admission aggregates.",
    unit="s",
)

# --- LLM-level ---
llm_calls_total: Counter = _meter.create_counter(
    "pythinker.llm.calls_total",
    description="Number of LLM API calls.",
    unit="1",
)
llm_duration_seconds: Histogram = _meter.create_histogram(
    "pythinker.llm.duration_seconds",
    description="LLM request duration.",
    unit="s",
)
llm_input_tokens: Counter = _meter.create_counter(
    "pythinker.llm.input_tokens",
    description="Input/prompt tokens consumed (diagnostic — pythinker is BYO-key).",
    unit="1",
)
llm_output_tokens: Counter = _meter.create_counter(
    "pythinker.llm.output_tokens",
    description="Output/completion tokens generated (diagnostic).",
    unit="1",
)
llm_cache_read_tokens: Counter = _meter.create_counter(
    "pythinker.llm.cache_read_tokens",
    description="Prompt-cache READ input tokens (cache hits — diagnostic for cache efficiency).",
    unit="1",
)
llm_cache_creation_tokens: Counter = _meter.create_counter(
    "pythinker.llm.cache_creation_tokens",
    description="Prompt-cache CREATION input tokens (cache writes — diagnostic).",
    unit="1",
)

# --- Tool-level ---
tool_calls_total: Counter = _meter.create_counter(
    "pythinker.tool.calls_total",
    description="Number of tool invocations (Read, Bash, Edit, MCP, …).",
    unit="1",
)
tool_duration_seconds: Histogram = _meter.create_histogram(
    "pythinker.tool.duration_seconds",
    description="Tool execution duration.",
    unit="s",
)

# --- Errors ---
errors_total: Counter = _meter.create_counter(
    "pythinker.errors_total",
    description="Errors observed by kind (api_error, tool_error, crash, …).",
    unit="1",
)


def bind(meter: Meter) -> None:
    """Re-bind every instrument to a real (SDK) meter once OTel is initialized.

    Called once from ``otel.init`` after the MeterProvider is up. Subsequent
    calls are idempotent — but we replace the module-level handles so existing
    references stay correct (Python rebinds names, not objects).
    """
    global _meter
    global turn_total, turn_duration_seconds, turn_step_count
    global request_assembly_duration_seconds
    global llm_calls_total, llm_duration_seconds, llm_input_tokens, llm_output_tokens
    global llm_cache_read_tokens, llm_cache_creation_tokens
    global tool_calls_total, tool_duration_seconds, errors_total

    _meter = meter
    turn_total = meter.create_counter(
        "pythinker.turn.total",
        description="Number of agent turns completed (one user message → final response).",
        unit="1",
    )
    turn_duration_seconds = meter.create_histogram(
        "pythinker.turn.duration_seconds",
        description="End-to-end agent-turn duration.",
        unit="s",
    )
    turn_step_count = meter.create_histogram(
        "pythinker.turn.step_count",
        description="Number of inner steps (LLM calls + tool loops) per turn.",
        unit="1",
    )
    request_assembly_duration_seconds = meter.create_histogram(
        "pythinker.request_assembly.duration_seconds",
        description="Request assembly duration with sanitized admission aggregates.",
        unit="s",
    )
    llm_calls_total = meter.create_counter(
        "pythinker.llm.calls_total",
        description="Number of LLM API calls.",
        unit="1",
    )
    llm_duration_seconds = meter.create_histogram(
        "pythinker.llm.duration_seconds",
        description="LLM request duration.",
        unit="s",
    )
    llm_input_tokens = meter.create_counter(
        "pythinker.llm.input_tokens",
        description="Input/prompt tokens consumed (diagnostic — pythinker is BYO-key).",
        unit="1",
    )
    llm_output_tokens = meter.create_counter(
        "pythinker.llm.output_tokens",
        description="Output/completion tokens generated (diagnostic).",
        unit="1",
    )
    llm_cache_read_tokens = meter.create_counter(
        "pythinker.llm.cache_read_tokens",
        description="Prompt-cache READ input tokens (cache hits, diagnostic).",
        unit="1",
    )
    llm_cache_creation_tokens = meter.create_counter(
        "pythinker.llm.cache_creation_tokens",
        description="Prompt-cache CREATION input tokens (cache writes — diagnostic).",
        unit="1",
    )
    tool_calls_total = meter.create_counter(
        "pythinker.tool.calls_total",
        description="Number of tool invocations (Read, Bash, Edit, MCP, …).",
        unit="1",
    )
    tool_duration_seconds = meter.create_histogram(
        "pythinker.tool.duration_seconds",
        description="Tool execution duration.",
        unit="s",
    )
    errors_total = meter.create_counter(
        "pythinker.errors_total",
        description="Errors observed by kind (api_error, tool_error, crash, …).",
        unit="1",
    )


# ---------------------------------------------------------------------------
# Recording helpers — used by the agent loop
# ---------------------------------------------------------------------------


def record_turn(*, duration_seconds: float, step_count: int, stop_reason: str) -> None:
    """Record a completed agent turn."""
    attrs: dict[str, Any] = {"stop_reason": stop_reason}
    turn_total.add(1, attrs)
    turn_duration_seconds.record(duration_seconds, attrs)
    turn_step_count.record(step_count, attrs)


def record_request_assembly(
    manifest: RequestManifest,
    *,
    duration_seconds: float,
) -> None:
    """Record content-free request admission aggregates."""
    outcomes = manifest.outcomes
    attrs: dict[str, Any] = {
        "source_ids": tuple(dict.fromkeys(outcome.source for outcome in outcomes)),
        "required_count": sum(
            outcome.requirement is FragmentRequirement.REQUIRED for outcome in outcomes
        ),
        "optional_count": sum(
            outcome.requirement is FragmentRequirement.BEST_EFFORT for outcome in outcomes
        ),
        "included_count": sum(outcome.status is FragmentStatus.INCLUDED for outcome in outcomes),
        "omitted_count": sum(
            outcome.status is FragmentStatus.OMITTED_BUDGET for outcome in outcomes
        ),
        "truncated_count": sum(outcome.status is FragmentStatus.TRUNCATED for outcome in outcomes),
        "degraded_count": sum(outcome.status is FragmentStatus.DEGRADED for outcome in outcomes),
        "failed_count": sum(outcome.status is FragmentStatus.FAILED for outcome in outcomes),
        "budget_limit": manifest.budget_tokens,
        "budgeted_admitted_tokens": manifest.budgeted_admitted_tokens,
        "non_budgeted_estimated_tokens": manifest.non_budgeted_estimated_tokens,
    }
    request_assembly_duration_seconds.record(duration_seconds, attrs)


# Model-name → family table. ``gen_ai.system`` only reflects the transport
# provider class, so every OpenAI-compatible endpoint (Alibaba DashScope,
# Moonshot, Zhipu, …) collapses into "openai" — the family attribute keeps the
# actual model lineage as a low-cardinality dashboard dimension. Order matters:
# first substring match wins (e.g. "codex" before "gpt").
_MODEL_FAMILY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("claude", "claude"),
    ("codex", "codex"),
    ("gpt", "gpt"),
    ("gemini", "gemini"),
    ("qwen", "qwen"),
    ("qwq", "qwen"),
    ("deepseek", "deepseek"),
    ("kimi", "kimi"),
    ("moonshot", "kimi"),
    ("minimax", "minimax"),
    ("abab", "minimax"),
    ("glm", "glm"),
    ("zhipu", "glm"),
    ("llama", "llama"),
    ("mistral", "mistral"),
    ("grok", "grok"),
)


def classify_model_family(model: str | None) -> str:
    """Classify a model name into a stable ``gen_ai.model.family`` value."""
    name = (model or "").lower()
    for needle, family in _MODEL_FAMILY_PATTERNS:
        if needle in name:
            return family
    return "other"


def record_llm_call(
    *,
    duration_seconds: float,
    system: str,
    model: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    success: bool = True,
) -> None:
    """Record one LLM API call."""
    attrs: dict[str, Any] = {
        "gen_ai.system": system,
        "gen_ai.request.model": model,
        "gen_ai.model.family": classify_model_family(model),
        "success": success,
    }
    llm_calls_total.add(1, attrs)
    llm_duration_seconds.record(duration_seconds, attrs)
    if input_tokens is not None and input_tokens > 0:
        llm_input_tokens.add(input_tokens, attrs)
    if output_tokens is not None and output_tokens > 0:
        llm_output_tokens.add(output_tokens, attrs)
    if cache_read_tokens is not None and cache_read_tokens > 0:
        llm_cache_read_tokens.add(cache_read_tokens, attrs)
    if cache_creation_tokens is not None and cache_creation_tokens > 0:
        llm_cache_creation_tokens.add(cache_creation_tokens, attrs)


def record_tool_call(
    *,
    tool_name: str,
    duration_seconds: float,
    success: bool,
    error_type: str | None = None,
) -> None:
    """Record one tool invocation."""
    attrs: dict[str, Any] = {"tool.name": tool_name, "success": success}
    if error_type:
        attrs["error_type"] = error_type
    tool_calls_total.add(1, attrs)
    tool_duration_seconds.record(duration_seconds, attrs)


def record_error(*, kind: str, error_type: str | None = None) -> None:
    """Record an error by kind (api_error, tool_error, crash, …)."""
    attrs: dict[str, Any] = {"kind": kind}
    if error_type:
        attrs["error_type"] = error_type
    errors_total.add(1, attrs)
