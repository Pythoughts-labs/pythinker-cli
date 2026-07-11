# Observable request assembly design

**Status:** Approved as phase 3 of the agent core deepening program

## Problem

A provider request is currently assembled across runtime prompt rendering, `PythinkerSoul`, AGENTS.md handling, dynamic injection providers, history normalization, tool advertisement, and provider adapters. Dynamic provider failures are logged and omitted, budget decisions are not inspectable, candidate metadata is discarded, and prompt inspection cannot show the same effective request that a provider receives.

The new module must own assembly decisions without changing the `pythinker_core.step` interface or persisting sensitive prompt content.

## Goals

- Produce one provider-ready request from explicit typed inputs.
- Preserve the current provider argument shape and compatibility ordering.
- Record source, authority, requirement, persistence, budget, and outcome for each fragment.
- Fail closed for required guidance and degrade explicitly for optional guidance.
- Preserve a stable static system-prompt prefix.
- Keep manifests in memory and sanitized.
- Expose the latest manifest through `/prompt-manifest`.

## Non-goals

- Moving provider authority mapping into the CLI package.
- Persisting full request bodies or manifests.
- Sending prompt contents, raw paths, credentials, or user text to telemetry.
- Making every dynamic provider required.
- Replacing `pythinker_core.step` or message normalization.
- Reordering existing compatible history before characterization tests prove intent.

## Module and interface

`pythinker_code.soul.request_assembly` becomes the deep module.

```python
class FragmentRequirement(StrEnum):
    REQUIRED = "required"
    BEST_EFFORT = "best_effort"

class FragmentPersistence(StrEnum):
    REQUEST_ONLY = "request_only"
    HISTORY = "history"

class FragmentStatus(StrEnum):
    INCLUDED = "included"
    NOT_APPLICABLE = "not_applicable"
    TRUNCATED = "truncated"
    OMITTED_BUDGET = "omitted_budget"
    DEGRADED = "degraded"
    FAILED = "failed"

class RequestStatus(StrEnum):
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"

@dataclass(frozen=True, slots=True)
class RequestFragment:
    key: str
    content: str
    source: str
    requirement: FragmentRequirement
    persistence: FragmentPersistence
    priority: int
    truncatable: bool

@dataclass(frozen=True, slots=True)
class FragmentOutcome:
    key: str
    source: str
    requirement: FragmentRequirement
    persistence: FragmentPersistence
    status: FragmentStatus
    estimated_tokens: int
    admitted_tokens: int
    reason_code: str | None

@dataclass(frozen=True, slots=True)
class RequestManifest:
    status: RequestStatus
    reason_code: str | None
    outcomes: tuple[FragmentOutcome, ...]
    budget_tokens: int
    budgeted_admitted_tokens: int
    non_budgeted_estimated_tokens: int

@dataclass(frozen=True, slots=True)
class RequestAssemblyInput:
    system_prompt: str
    persisted_history: tuple[Message, ...]
    current_task: str
    budget_tokens: int

@dataclass(frozen=True, slots=True)
class AssembledRequest:
    system_prompt: str
    provider_history: tuple[Message, ...]
    history_appends: tuple[Message, ...]
    manifest: RequestManifest

class RequestAssemblyError(RuntimeError):
    manifest: RequestManifest
    reason_code: str

class RequestAssembler:
    async def assemble(self, request: RequestAssemblyInput) -> AssembledRequest: ...
```

`RequestManifest` never contains fragment content, user text, model output, raw paths, or provider credentials. The assembler computes and validates token estimates from fragment content with the shared estimator; source adapters cannot supply or understate them. Per-fragment admitted counts include both budgeted and non-budgeted fragments. Aggregate fields separate budgeted admitted tokens from non-budgeted estimates, so authoritative AGENTS.md is visible without being charged against the optional budget. The signatures may be adapted during implementation planning to established types, but the information contract must not shrink.

## Source adapters

Existing `DynamicInjectionProvider` implementations remain source adapters during migration. A compatibility adapter converts each returned `DynamicInjection` into a best-effort history fragment with the provider's current ordering and priority.

Providers migrate individually to a typed result:

- `provided`: one or more fragments were produced.
- `not_applicable`: the provider completed successfully and no fragment applies.
- `failed`: the provider could not establish required state or encountered an actionable dependency error.

A provider returning no data is not treated as failure unless its interface declares that a result was required for the current request.

## Requiredness matrix

The approved initial policy is:

| Source | Requirement | Persistence | Budget behavior |
| --- | --- | --- | --- |
| AGENTS.md preamble | Required | Request-only projection of authoritative project instructions | Non-budgeted |
| Permission state | Required | History, matching current replay semantics | Reserved, never truncated |
| Applicable model defense | Required | History, matching current replay semantics | Reserved, never truncated |
| Task-relevant skill candidates | Best-effort | Request-only | Bounded by the skill catalogue cap |
| Plan, goal, active skills, agent list, orchestration, git, LSP, inline command, and auto-mode reminders | Best-effort | Preserve current persistence initially | Priority budget, truncation only when explicitly safe |

Model defense may return `not_applicable` when the current model profile does not require it. That is a successful outcome. Exceptions, invalid state, or unavailable required inputs produce failure.

Every registered provider not named in the table is admitted through the compatibility adapter as best-effort, with its source and outcome recorded. This normative catch-all prevents current or plugin providers from disappearing during migration.

Security classification cannot be changed by an untrusted provider payload. Requiredness is registered by trusted application composition, not supplied by fragment content.

## Assembly order

1. Accept the stable system prompt and a snapshot of persisted history.
2. Collect AGENTS.md through its existing authoritative trust wrapper.
3. Collect required providers and stop on required failure.
4. Ask `SkillCatalog` for task-relevant request-only candidates.
5. Collect optional providers and convert failures to degraded outcomes.
6. Reserve required budget before optional admission.
7. Admit optional fragments by explicit priority and deterministic source order.
8. Truncate only fragments whose trusted source marks them truncatable.
9. Apply persistence policy and existing history-normalization rules.
10. Return provider-ready history and the sanitized manifest.

Required fragments never compete with optional fragments. If required budget exceeds the available context, assembly raises `RequestBudgetError` before provider invocation. It does not truncate required guidance or silently evict it.

The static system prompt remains identical across tasks. Task-specific skill candidates and dynamic fragments are placed after the stable prefix in provider-visible history, protecting exact-prefix caching.

## History mutation and retries

Assembly itself is pure with respect to persisted `Context`. `history_appends` contains only newly admitted `HISTORY` messages. `provider_history` contains the input persisted history, those appends, and request-only fragments in final provider order. A request-only fragment can never appear in `history_appends`.

`PythinkerSoul` persists `history_appends` only after successful assembly and before provider invocation. Every persistent fragment has a stable key. Before append, the assembler checks the relevant history window for that key according to the provider's rearm policy. Retrying the same step does not append duplicate reminders.

If persistence fails, the provider is not called. The soul derives a new manifest by copying the successful assembly outcomes, setting overall status to `FAILED`, and setting reason code `context_persistence_failed`; it does not fabricate a fragment outcome. If the provider call fails after persistence, the committed reminder remains available for replay, matching current context behavior.

## Error contract

`RequestAssemblyError` is the base categorized failure. Every instance carries the sanitized failure `manifest` produced from all outcomes observed before failure plus a stable `reason_code`. Its manifest must have status `FAILED`, and `error.reason_code` must equal `error.manifest.reason_code`. `PythinkerSoul` stores that manifest before re-raising, so `/prompt-manifest` reports the failed attempt rather than stale success. Subclasses or reason codes distinguish:

- Required source unavailable.
- Required source invalid.
- Required content exceeds budget.
- History normalization failure.
- Persistence failure at the caller seam.
- Internal invariant violation.

Required failures are safe to surface with source identifiers and recovery guidance, never raw content. Optional failure yields `FragmentStatus.DEGRADED`, structured logging, and a visible manifest outcome.

Broad provider exceptions are converted only at the provider adapter seam. Unexpected programming errors retain a causal chain and are not converted to an empty successful result.

## Manifest observability

`PythinkerSoul` retains only the latest `RequestManifest` in memory. Successful assembly supplies it through `AssembledRequest`; failed assembly supplies it through `RequestAssemblyError`. The soul replaces the stored manifest on either path, so diagnostics do not show stale success.

`/prompt-manifest` renders:

- Overall `SUCCEEDED`, `DEGRADED`, or `FAILED` status and its safe overall reason code when present.
- Fragment key and trusted source identifier.
- Requirement and persistence class.
- Included, omitted, truncated, degraded, failed, or not-applicable status.
- Estimated and admitted token counts.
- Safe reason code.

It never renders fragment content, user content, raw file paths, secrets, or stack traces. Before the first assembly, it reports that no request has been assembled in the session.

The command uses normal slash-command output so Shell, print, ACP, and web consumers receive existing wire-safe text rather than a new protocol event. Public documentation and slash-command snapshots are updated.

## Telemetry

Telemetry may record only aggregate counts and stable source identifiers:

- Required, optional, included, omitted, truncated, degraded, and failed counts.
- Budget limit, budgeted admitted estimates, and non-budgeted estimated tokens as separate values.
- Assembly duration.

No content, raw paths, user input, tool arguments, model output, or provider credentials are recorded. Telemetry failure remains subject to existing project policy and cannot change required assembly success.

## Test design

Tests are written before implementation and must first fail for the intended missing behavior.

- Compatibility input produces byte-equivalent provider system prompt and history.
- Different tasks retain an identical static system prompt.
- Required source failure prevents persistence and provider invocation.
- Required not-applicable succeeds only for a source that supports that state.
- Required content is never truncated.
- Required budget exhaustion raises a categorized error.
- Optional provider failure creates a degraded outcome and permits provider invocation.
- Non-truncatable optional content is omitted rather than cut.
- Truncatable optional content records the admitted estimate.
- Equal-priority sources use stable explicit order rather than registration accidents.
- Stable keys prevent duplicate persistent reminders on retry.
- Manifest data contains no prompt snippets, user text, credentials, or raw paths.
- Adapter-supplied estimates cannot bypass assembler-computed budgeting.
- Budgeted and non-budgeted aggregate counts remain distinct.
- Caller persistence failure produces overall `FAILED` status without a fabricated fragment.
- `/prompt-manifest` handles no-data, success, degraded, and failure states.
- OpenAI and Anthropic provider snapshot tests preserve authority mapping with no real credentials.
- Disabled dynamic injection mode cannot bypass required security guidance.

Focused verification covers dynamic budgets, provider hooks, permissions, model defense, turn balancing, slash commands, provider snapshots, and wire behavior before the full Pythinker Code gate.

## Migration and deletion

1. Characterize current handoff bytes, ordering, persistence, and provider failures.
2. Add fragment, outcome, manifest, and categorized error types.
3. Return explicit outcomes from the existing budget selector while preserving its compatibility projection.
4. Adapt every current provider as best-effort without behavior change.
5. Extract request assembly and prove byte equivalence.
6. Register permissions and applicable model defense as required.
7. Add `SkillCatalog` candidates as request-only fragments.
8. Add sanitized telemetry and `/prompt-manifest`.
9. Migrate provider lifecycle and rearm metadata into typed fragments.
10. Delete `_collect_injections` and direct AGENTS.md/history orchestration from `_step`.

The deletion test passes when removing `RequestAssembler` would spread requiredness, ordering, persistence, budgeting, sanitization, retry identity, and degradation policy back across `PythinkerSoul` and provider implementations.

## Rollback

Revert the phase if provider handoff compatibility, required guidance, prefix stability, or security redaction tests fail. Do not preserve a hidden fail-open mode. Compatibility adapters project old providers into the one assembly implementation and do not create a second path.
