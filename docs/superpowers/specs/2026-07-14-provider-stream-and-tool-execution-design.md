# Provider stream correlation, compatibility profiles, and tool execution design

**Status:** Approved

**Date:** 2026-07-14

## Purpose

Pythinker must treat streamed tool calls as correlated protocol objects, not as one sequential stream of mergeable fragments. The current core accumulator holds one pending part. When two tool calls are streamed and their argument fragments interleave, a fragment can attach to the most recently observed call instead of the call identified by the provider's index. A deterministic reproduction leaves the first call empty and concatenates both argument objects onto the second call.

The same investigation found two adjacent architecture problems. Provider compatibility behavior is distributed across authentication modules, `llm.py`, provider adapters, and tool visibility. Tool execution is concentrated in `PythinkerToolset`, where registry and MCP lifecycle code share a class with parsing, policy, hooks, deduplication, scheduling, telemetry, and cancellation.

This design addresses the three problems in dependency order:

1. Correct indexed stream assembly in `pythinker-core`.
2. Concentrate transport compatibility and add clean, independent Z.AI Coding Plan and standard API routes.
3. Extract a batch-oriented tool execution engine without changing execution behavior.

The exact transcript behind the originally reported intermittent GLM-5.2 symptom is unavailable. The indexed-stream defect and existing Z.AI incompatibilities are proven independently; the design does not claim they are the only possible causes of that report.

## Goals

- Correlate tool-call fragments by provider-supplied identity while preserving model call order.
- Prevent any tool side effect until the entire assistant stream has terminated successfully and all calls are structurally complete.
- Fail explicitly on ambiguous, conflicting, orphaned, or truncated tool-call streams.
- Preserve compatibility for simple third-party providers that emit one unindexed call at a time.
- Resolve provider quirks through one typed compatibility profile attached to the active `LLM`.
- Keep provider-specific decisions out of `PythinkerSoul` and tool execution.
- Support Z.AI Coding Plan and standard API credentials simultaneously with explicit identities and no endpoint guessing.
- Preserve exact captured GLM reasoning when preserved thinking is enabled.
- Give tool execution one batch boundary while retaining `PythinkerToolset` as the compatibility facade.
- Keep the three phases independently reviewable and separately releasable.

## Non-goals

- Supporting the mainland `open.bigmodel.cn` service.
- Supporting an Anthropic-compatible Z.AI transport.
- Detecting a Z.AI key's plan type or retrying a request against another Z.AI endpoint.
- Migrating old `z-ai` provider entries, model aliases, login names, or environment variables. This integration has no users requiring compatibility, so the new contract is clean rather than transitional.
- Changing approval, hook, deduplication, concurrency, interruption, or tool-result semantics for conforming tools during the execution extraction. A tool that suppresses cancellation is explicitly hardened as an unsupported failure case.
- Starting tools speculatively before stream completion.
- Adding runtime dependencies, raw SSE logging, reasoning logging, or hosted telemetry.
- Claiming the original intermittent symptom is resolved without a redacted live confirmation.

## External provider contract

The implementation follows the current Z.AI global API contracts:

- Coding Plan OpenAI-compatible base URL: `https://api.z.ai/api/coding/paas/v4`.
- Standard API OpenAI-compatible base URL: `https://api.z.ai/api/paas/v4`.
- GLM-5.2 has a 1,000,000-token context window and a documented maximum output of 131,072 tokens.
- `tool_stream` enables streamed function-call deltas on supported GLM models.
- GLM-5.2 maps `low`, `medium`, and `high` reasoning effort to `high`; `xhigh` and `max` map to `max`; `none` and `minimal` skip thinking.
- Preserved thinking requires `clear_thinking: false` and exact, unmodified, correctly ordered replay of prior `reasoning_content`.

Primary references:

- <https://docs.z.ai/guides/llm/glm-5.2>
- <https://docs.z.ai/api-reference/llm/chat-completion>
- <https://docs.z.ai/guides/capabilities/thinking-mode>
- <https://docs.z.ai/devpack/latest-model>

These are transport contracts, not instructions to trust provider output. Tool names, arguments, stream structure, and model catalogs remain external untrusted input and are validated at their boundaries.

## Approaches considered

### Disable tool streaming

Turning off `tool_stream` or using only non-streaming responses would reduce exposure for one provider, but it would leave the core defect intact for every provider that streams parallel calls. It would also discard supported functionality rather than repair the contract. Rejected.

### Buffer independently inside each provider adapter

Every adapter could construct complete calls before yielding them. That localizes wire details but duplicates correlation, ordering, conflict detection, deterministic ID fallback, and malformed-stream policy across OpenAI Chat Completions, Responses, Anthropic-shaped, and first-party transports. Custom providers would still encounter the unsafe generic accumulator. Rejected.

### Shared stream assembler, typed compatibility profiles, and batch execution

Adapters preserve wire correlation metadata; one core assembler owns stream-level call state; compatibility profiles own request and replay quirks; one execution engine owns post-generation tool lifecycle. This creates three deep modules with distinct dependencies and failure contracts. Selected.

## Architecture

```text
saved provider + model configuration
                |
                v
ProviderCompatibilityResolver -----> immutable compatibility profile
                |                                  |
                v                                  v
         ChatProvider adapter <------ request/replay policy
                |
       normalized stream parts
                |
                v
        StreamMessageAssembler
        |                     |
 live text/thinking      complete ToolCalls
 callbacks                    |
                              v
                     ToolExecutionEngine
                     |                 |
              completion callbacks  ordered results
                     |                 |
                     +-------> context growth
```

The dependency direction is deliberate:

- Authentication owns credentials, endpoints, and model discovery.
- Compatibility resolution owns transport behavior for a known provider/model pair.
- Provider adapters translate wire events and message history.
- Stream assembly owns correlation and completion.
- Tool execution owns validated side effects after generation.
- `PythinkerSoul` coordinates request, persistence, interruption, and turn control without knowing provider quirks or tool-internal lifecycle.

## Phase 1: Correlated stream assembly

### Stream event contract

`ToolCall` remains the complete persisted/message-level object. Streaming call starts and `ToolCallPart` fragments carry non-persisted correlation metadata:

- provider call index when supplied;
- provider call ID when supplied;
- a complete function name on a call start;
- an explicitly identified function-name fragment on a fragment event;
- argument fragment when supplied.

Correlation metadata is excluded from provider history and session serialization. Non-streamed provider responses continue to yield complete `ToolCall` values.

Existing providers that emit a complete `ToolCall` start followed by `ToolCallPart` fragments remain supported. OpenAI-shaped adapters must copy `tool_calls[].index` and any available ID onto the normalized stream parts rather than discarding them.

If a provider omits a call ID, the assembler creates a deterministic message-local surrogate after assembly. It must not use an unseeded UUID. A provider ID that appears later replaces the provisional identity only when it is consistent with the same indexed call.

### `StreamMessageAssembler`

A new core module owns message construction. It keeps content assembly separate from an ordered map of in-progress tool calls.

For each call it records:

- correlation key;
- provider index or first-seen order;
- stable provider ID or deterministic surrogate;
- complete function name and any name fragments;
- accumulated arguments;
- whether a complete call start was observed.

Resolution rules are:

1. Use stream index when present.
2. Otherwise use provider call ID when present.
3. Otherwise use the only currently open legacy call.
4. If more than one call could accept an unindexed fragment, fail as ambiguous.

A fragment may arrive before its call start when it has an index or ID. It is buffered and must resolve before terminal validation. Repeated complete metadata is accepted only when identical. Name fragments append in arrival order for their correlated call. If a complete name and name fragments both appear, their assembled values must agree; otherwise assembly fails. Conflicting index/ID associations or complete function names fail explicitly.

An indexed stream returns calls in ascending provider-index order. An ID-only or legacy unindexed stream returns calls in first-seen order. A multi-call stream that mixes indexed and unindexed call starts fails as ambiguous instead of guessing an order.

### Completion and callback semantics

`on_message_part` continues receiving defensive copies of normalized text, thinking, and tool stream parts as they arrive. Text and thinking therefore remain live.

`on_tool_call` fires once per complete call, in model call order, only after successful terminal assembly. `pythinker_core.step` consequently starts no tool task while the provider stream is still open. This intentionally trades speculative overlap for correctness and retry safety.

The assembler records whether any `on_message_part` callback was invoked during the attempt. That observable-publication state is attached to a terminal protocol error so the retry classifier cannot replay already-published text, thinking, or tool deltas as though the first attempt never happened.

A successful terminal stream requires:

- no unresolved keyed fragment;
- no ambiguous legacy fragment;
- one non-empty function name per call;
- stable call identity;
- a finish reason that does not indicate truncation or transport failure.

An absent argument payload normalizes to `{}`. JSON validity remains an execution-boundary concern so malformed model-generated arguments produce the existing `ToolParseError` and can be shown to the model without retrying generation.

### Stream failures

A new typed `APIStreamProtocolError` distinguishes malformed provider streaming from empty output, HTTP failure, and invalid tool JSON. Its safe diagnostic fields are limited to response ID when available, correlation index/ID, and error category; it never includes arguments or reasoning.

- Ambiguous, conflicting, or orphaned stream state raises `APIStreamProtocolError`.
- A stream ending with `finish_reason="length"` and any tool-call state executes no tools and raises a typed truncation/protocol failure.
- A transport exception cancels assembly and executes no tools.
- The CLI may apply its existing bounded generation retry policy to a stream protocol failure only when no message-part callback published output for that attempt. After any observable publication, the failure is surfaced and is not retried, preventing duplicated or contradictory transcript output.
- Structurally complete invalid JSON is not a retryable stream error; it becomes `ToolParseError` during execution.

All partial state is released on success, error, or cancellation.

### Compatibility behavior for custom providers

The public stream union remains compatible. A custom provider may continue yielding:

- complete, non-streamed `ToolCall` objects;
- one unindexed `ToolCall` followed by unindexed fragments;
- fully indexed/interleaved calls.

Only a stream that becomes genuinely ambiguous is newly rejected. Silent cross-call corruption is not a supported compatibility behavior.

## Phase 2: Provider compatibility profiles and Z.AI routes

### Profile boundary

A new `provider_compatibility` module exposes an immutable profile and one resolver. The resolver receives the provider key, provider configuration, and model configuration. Resolution precedence is:

1. Exact managed provider key.
2. Exact normalized hostname/path and API family for user-defined compatible endpoints.
3. Model-specific behavior only inside an already recognized provider or endpoint family.
4. Conservative API-family defaults for unknown providers.

A model name alone never activates hosted-provider behavior. A local or third-party model named `glm-*` must not inherit Z.AI credentials, endpoints, thinking replay, or request fields.

The profile owns only transport compatibility:

- API family and adapter options;
- output-token parameter and model ceiling;
- tool-result conversion;
- deferred-tool-search capability;
- tool-stream request flags;
- reasoning field and effort mapping;
- thinking enable/disable body;
- reasoning replay requirements;
- provider-specific role support where currently required.

Authentication, token refresh, catalog retrieval, permissions, approvals, retries, and business policy remain outside the profile.

`create_llm` remains the transport factory. Its provider-type match instantiates the adapter, while the profile supplies the adapter options and generation overrides. Existing Kimi, DashScope, Qwen, Anthropic-proxy, OpenAI, and related behavior moves behind profiles with behavior-locking tests. Only the approved Z.AI behavior changes.

The resolved profile is stored on `LLM`. `supports_deferred_tool_search`, tool-result conversion, output capping, thinking-level availability, and replay serialization consult that profile rather than independently reclassifying the provider.

### Clean Z.AI identities

The integration defines two independent managed platforms:

| Route | Platform/model prefix | Provider key | Environment variable | Base URL |
| --- | --- | --- | --- | --- |
| Coding Plan | `z-ai-coding/*` | `managed:z-ai-coding` | `ZAI_CODING_API_KEY` | `https://api.z.ai/api/coding/paas/v4` |
| Standard API | `z-ai-api/*` | `managed:z-ai-api` | `ZAI_API_KEY` | `https://api.z.ai/api/paas/v4` |

There is no `z-ai/*` compatibility alias, legacy environment fallback, or persisted configuration migration. Interactive and CLI login, refresh, model selection, usage display, and logout use the explicit route identities. Logging into one route does not remove or rewrite the other. The most recently completed login may become the default model according to existing login behavior.

### Authentication and model discovery

Each route validates and discovers with its own OpenAI-compatible `/models` endpoint and bearer credential. Discovery state is never shared across routes.

- Missing or blank credentials fail before network access.
- `401` and `403` are authentication failures; the key and route are not saved.
- Timeout, transport failure, non-auth HTTP failure, or structurally unusable catalog data produces an explicit degraded-login information event and the route's curated built-in catalog.
- An empty but structurally valid catalog also uses the curated catalog and reports degradation.
- GLM-5.2 remains pinned per route when the corresponding endpoint omits it but the route contract supports it.
- Refresh updates and prunes only models owned by that route's provider key.
- Logout removes only the selected route and repairs the default model if necessary.
- No login probe sends a billed chat completion, and no failure retries the credential against the other route.

### GLM request behavior

For GLM-5.2 on either route:

- `reasoning_content` is captured as `ThinkPart` and replayed exactly when present.
- Enabled thinking sends `thinking: {"type": "enabled", "clear_thinking": false}`.
- Disabled or minimal thinking sends exactly `thinking: {"type": "disabled"}` and omits both `clear_thinking` and reasoning effort.
- `low`, `medium`, and `high` send `reasoning_effort: "high"`.
- `xhigh` and `max` send `reasoning_effort: "max"`.
- A new configuration with no explicit thinking preference initializes to `high`; an existing explicit preference is preserved.
- `tool_stream: true` is sent when tools are present.
- The maximum output setting is 131,072 tokens.
- Tool results use the profile's single-text representation.
- Deferred `ToolSearch` remains unavailable.

Other curated GLM models use their documented context/output limits and binary thinking support. `reasoning_effort` is sent only for a model that supports it, and `tool_stream` is sent only for supported GLM versions. Unknown discovered models do not inherit GLM-5.2-only controls merely from catalog presence.

Preserved thinking never synthesizes content. An assistant turn with no captured reasoning replays no invented placeholder. Captured reasoning is neither edited nor logged. Request assembly and history compaction may retain or remove complete history turns according to their existing contracts, but they must not truncate or rewrite a retained `ThinkPart`.

### Provider-neutral callers

`PythinkerSoul` continues to call `pythinker_core.step` with the active provider and toolset. It contains no Z.AI, GLM, endpoint, replay, or tool-result conversion branch. Tool visibility asks the active profile for the deferred-search capability. Provider adapters receive already-resolved compatibility policy rather than inspecting arbitrary model names in agent code.

## Phase 3: Batch-oriented tool execution

### Relationship to the prior Toolset characterization design

The earlier Toolset characterization design assumed the per-call `handle` interface would remain the execution seam and prohibited extraction based on file size alone. Phase 1 changes that premise: generation now produces a complete call batch before any dispatch, and this design explicitly approves a batch execution boundary.

This design supersedes only the prior conditional no-go for the private execution-pipeline extraction. The prior numerical thresholds, performance-trigger requirement, and benchmark-backed go/no-go decision record are waived for this extraction because the new complete-batch contract creates a functional seam rather than a line-count or performance optimization. Deterministic fault tests, behavioral characterization, and before/after regression measurements remain mandatory. The prior no-go rules for MCP lifecycle and registry extraction remain unchanged. Phase 3 does not extract MCP lifecycle or the registry.

### Ownership split

`PythinkerToolset` remains the caller-facing facade and owns:

- tool registration and lookup;
- hidden and advertised tool projection;
- runtime visibility policy;
- shared and external tool registration;
- MCP connection, inventory, refresh, publication, and cleanup.

A private `ToolExecutionEngine` owns:

- argument parsing and canonicalization;
- same-step and cross-step deduplication state;
- permission and approval enforcement;
- pre-use and post-use hook lifecycle;
- reader/writer scheduling;
- execution lifecycle events;
- tool spans, metrics, and existing telemetry calls;
- exception conversion;
- cancellation and batch settlement;
- repeated-call reminders and execution summary.

Moved state and logic are deleted from `PythinkerToolset`; the engine is not a forwarding-only file. The read/write gate moves with its sole execution owner. `PythinkerToolset.handle`, `begin_step`, `end_step`, and existing summary properties remain compatibility facades for tests and external callers, but `PythinkerSoul` uses the batch path.

### Batch contract

The optional batch protocol accepts:

- the complete ordered `ToolCall` sequence;
- turn ID;
- step number;
- prior normalized call fingerprints.

It returns a cancellable batch handle that exposes:

- completion callbacks as individual calls finish;
- ordered final `ToolResult` values;
- current normalized call fingerprints;
- whether deduplication triggered;
- the consecutive-identical-call count.

`pythinker_core.step` detects this optional protocol. Toolsets without it retain the existing `handle` dispatch path. The optional execution context and summary do not change provider history or public CLI configuration.

`PythinkerSoul` passes step context once and consumes the returned summary. It no longer coordinates `begin_step`/`end_step` or reads `PythinkerToolset`-specific execution state. It remains responsible for turn limits, context growth, interrupted-result persistence, and stuck-loop decisions because those are conversation concerns rather than tool invocation concerns.

### Execution pipeline

For one batch the engine:

1. Resolves each tool and parses its JSON arguments.
2. Canonicalizes arguments and records model call order.
3. Classifies same-step and cross-step duplicates.
4. Checks permission and approval policy before side effects.
5. Runs `PreToolUse`; a block result is authoritative.
6. Schedules the call through the existing reader/writer policy.
7. Emits execution-started state at the existing lifecycle point.
8. Invokes the tool with a defensive argument copy.
9. Records existing spans, metrics, logs, and tool-call events.
10. Schedules the existing managed post-use hook.
11. Applies any repeat reminder and resolves exactly one result for the call ID.

Same-step duplicate calls share the original underlying task and return the same value under their own IDs. They do not duplicate side effects. Final results follow model call order even when completion callbacks arrive in another order.

### Concurrency and cancellation

- Tools declaring `supports_parallel=True` remain bounded readers.
- Mutating, unclassified, plugin, and MCP tools remain exclusive by default.
- A queued writer continues blocking new readers and cannot starve.
- No tool invocation is retried automatically.
- Ordinary tool exceptions retain `ToolRuntimeError` conversion and actionable logging.
- Parse, missing-tool, policy, and hook-block outcomes retain their existing typed results.
- `CancelledError` and other control-flow `BaseException` values propagate after owned resources and spans are settled.
- Cancelling a batch cancels pending tasks, waits at most five seconds for settlement, and retains already-completed results for interruption persistence.
- A task still running after that bound raises `ToolCancellationTimeoutError`, marks the engine poisoned, and remains in an engine-owned late-task registry with exception-consuming completion callbacks. A poisoned engine rejects later batches until every late task settles; it never permits a potentially mutating orphan to overlap a new batch.
- Engine cleanup re-cancels and supervises the late-task registry and reports any unresolved count explicitly rather than claiming clean shutdown.
- Post-hook work remains owned by the existing hook engine lifecycle; extraction must not create unsupervised tasks.

Phase 3 is judged against the Phase 1/2 baseline. The only intentional execution-timing change occurred in Phase 1, when dispatch moved behind successful stream termination. The five-second poison path is an explicit safety hardening for tools that violate the supported cancellation contract; conforming tools retain existing behavior.

## Failure truthfulness

- A malformed or truncated tool stream never becomes a successful assistant/tool step.
- A retryable generation failure occurs before tool side effects, so bounded retry cannot duplicate a write; a failed attempt that already published stream output is not retried.
- Invalid model-generated JSON is returned as a parse failure, not tool success and not transport failure.
- Z.AI authentication failure never saves a key or silently tests another paid endpoint.
- Catalog fallback is labeled degraded and cannot be mistaken for live discovery.
- Missing preserved reasoning is never replaced with synthetic text presented as authentic model reasoning.
- Approval, policy, and `PreToolUse` uncertainty continues to fail closed.
- One tool failure does not reorder or erase other completed results.
- Cancellation leaves no unanswered persisted tool call: the soul retains real completed results and writes interruption results only for unfinished calls. An uncooperative task poisons execution and prevents another batch until its possible side effect has settled.

## Safe diagnostics

New diagnostics are local and structured. They may include:

- compatibility profile ID;
- endpoint class, never credential;
- response ID when already safe to log;
- tool-call index or provider call ID;
- protocol error category;
- aggregate call count and completion state.

They must not include API keys, authorization headers, tool arguments, tool output, reasoning content, full request/response bodies, or raw SSE chunks. This design adds no telemetry destination or event family.

## Test design

Tests are written before implementation changes and must fail for the intended reason.

### Phase 1 regression and contract tests

- Two indexed calls whose argument fragments alternate reconstruct independently.
- Three-call permutations preserve ascending provider-index order; ID-only calls preserve first-seen order.
- A fragment arriving before its indexed start resolves correctly.
- Stable repeated IDs and complete names are accepted; conflicts fail; correlated name fragments assemble in order.
- Missing provider ID receives a deterministic surrogate.
- One unindexed legacy call remains compatible.
- Two open calls plus an unindexed fragment fail as ambiguous.
- An unresolved orphan fails at terminal validation.
- A terminal `length` with tool-call state executes nothing.
- Transport error and cancellation release all assembly state and execute nothing.
- Text and thinking callbacks remain live while complete-call callbacks wait for terminal assembly.
- Complete-call callbacks fire exactly once and in model order.
- Non-streamed complete tool calls retain behavior.
- OpenAI Chat Completions and every adapter that emits partial tool calls retain correlation metadata.

The original deterministic reproduction is a required regression fixture.

### Phase 2 profile and route tests

- Coding and standard providers, keys, and same-named models coexist.
- Login for one route does not prune or replace the other.
- Each environment variable resolves only its named route.
- Blank credentials, `401`, and `403` do not mutate saved configuration.
- Discovery timeout, transport failure, malformed payload, and empty catalog report degraded fallback.
- Refresh and logout affect only the selected route.
- Request capture asserts exact Coding and standard base URLs and bearer authentication without exposing the key.
- GLM-5.2 off/minimal/high/max bodies match the approved mapping.
- Enabled requests include preserved-thinking and tool-stream controls.
- Disabled requests omit reasoning effort.
- History replay preserves exact reasoning bytes and ordering.
- A local `glm-5.2` on an unrelated endpoint does not resolve a Z.AI profile.
- ToolSearch visibility and tool-result serialization derive from the profile.
- The output-token ceiling and reasoning support are model-specific.
- Existing non-Z.AI compatibility fixtures remain byte- or behavior-equivalent as appropriate.
- Production code, docs, and tests contain no legacy `z-ai/*` route or fallback contract except historical release text if needed.

No test requires a real key. An optional manual smoke test may use separately configured credentials only when explicitly invoked; it redacts request headers and content.

### Phase 3 characterization and parity tests

Characterization precedes movement of production logic. Tests cover:

- not-found suggestions and malformed JSON;
- permission denial and approval closure;
- authoritative `PreToolUse` block;
- pre-hook and post-hook failure behavior;
- same-step task sharing and per-ID results;
- cross-step reminders and consecutive-repeat counts;
- reader overlap, reader bound, writer exclusion, and writer fairness;
- tool exception conversion and later-call recovery;
- cancellation before admission, during execution, and during batch settlement, including the five-second poisoned-engine path for a cancellation-suppressing tool;
- completion-order callbacks versus model-order results;
- partial completion followed by interruption;
- retry of the same model step without awaiting stale cancelled tasks;
- context rewind with cleared prior fingerprints;
- third-party per-call `handle` fallback;
- no leaked tasks, permits, spans, or hook work.

Existing Toolset and concurrency tests remain compatibility tests. Tests should assert behavior through the facade and the batch contract rather than private implementation line structure.

## Acceptance criteria

### Phase 1

- The deterministic interleaving reproduction returns each call with only its own arguments.
- No code path starts a tool before successful terminal stream assembly.
- Ambiguous or conflicting stream state produces a typed failure with no side effect.
- Existing simple and non-streamed providers remain compatible.

### Phase 2

- `z-ai-coding/glm-5.2` and `z-ai-api/glm-5.2` can coexist and select their own credentials/endpoints.
- GLM-5.2 request, replay, stream, and output behavior matches the approved profile.
- Provider-specific behavior is absent from `PythinkerSoul` and tool execution.
- Non-Z.AI compatibility behavior remains locked by tests.
- No migration or endpoint fallback path exists.

### Phase 3

- `PythinkerSoul` performs one batch handoff and consumes one execution summary rather than coordinating per-call Toolset lifecycle.
- Execution state has one owner and is deleted from `PythinkerToolset` except compatibility delegation.
- Approval, hooks, deduplication, concurrency, telemetry, errors, ordering, and cancellation match the characterized baseline.
- Removing the engine would force its state machine back into `PythinkerToolset`; it passes the deletion test.

## Delivery sequence

### PR 1: Stream correlation correctness

Scope: `pythinker-core` message/stream contracts, adapters, assembler, typed error, and tests. Add an `## Unreleased` changelog entry describing corrected parallel streamed tool calls.

Minimum gate:

```bash
make check-pythinker-core
make test-pythinker-core
```

Run the repository-required broader pre-PR gate before opening the PR.

### PR 2: Compatibility profiles and dual Z.AI routes

Scope: compatibility resolver, provider factory integration, clean auth identities, login/refresh/logout, model catalogs, docs, changelog, and focused request/history tests.

Minimum gates:

```bash
make check-pythinker-core
make test-pythinker-core
make check-pythinker-code
make test-pythinker-code
```

Core gates are required because profile-driven replay and adapter options may change `pythinker-core` contracts. Because this phase depends on terminal correlation before enabling tool streaming, PR 2 must not merge before PR 1.

### PR 3: Tool execution engine extraction

Scope: batch protocol, execution engine, Toolset delegation, soul integration, characterization/parity tests, and a changelog entry or an explicitly justified `no-changelog` classification if maintainers determine it is entirely invisible.

Minimum gates:

```bash
make check-pythinker-core
make test-pythinker-core
make check-pythinker-code
make test-pythinker-code
```

Each PR receives independent code review, the required CodeRabbit review when merging, and the complete repository pre-PR checks for every affected package. Focused passing tests are not a substitute for the full package gates.

## Rollback

- PR 1 can be reverted before PR 2. Once PR 2 enables Z.AI tool streaming, PR 2 must be reverted before reverting PR 1.
- PR 2 has no migration to reverse. Before release it is a code-only revert; after a user has configured either new route, rollback must remove the now-unsupported provider/model entries or move the default to another provider before reverting.
- PR 3 can be reverted independently because `PythinkerToolset` retains compatibility facades and provider/history contracts do not depend on the extracted implementation.
- No hidden feature flag selects old versus new behavior. A failed phase is reverted rather than kept as a parallel path.

## Residual risks

- The original user-observed GLM transcript is unavailable, so a separate model or prompt-level issue may remain after the deterministic defects are fixed.
- Delaying tool dispatch until stream completion removes speculative overlap. Correctness is mandatory; latency impact should be measured but cannot justify restoring unsafe dispatch.
- Sending the documented maximum output ceiling permits longer paid standard-API responses. It is a ceiling rather than a target, but usage behavior should be observed during the optional smoke test.
- Provider model catalogs and compatibility behavior can change. The route profiles therefore require focused contract tests and authoritative documentation review when updated.
- The execution extraction touches a load-bearing approval and cancellation path. Characterization, one-owner state, and separate delivery are mandatory mitigations.
