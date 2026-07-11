# Toolset characterization and conditional deepening design

**Status:** Approved as phase 6 of the agent core deepening program

## Problem

`PythinkerToolset` is large because it hides registry, visibility, execution, concurrency, deduplication, approvals, hooks, telemetry, and MCP lifecycle behavior behind one interface. Size alone does not prove poor depth. Splitting before characterizing latency, contention, lifecycle failure, and change locality could create shallow private modules, duplicate state, or callback cycles without improving callers.

This phase first produces reproducible evidence. Private extraction is permitted only when a documented threshold is crossed and the resulting implementation deletes equivalent state from `PythinkerToolset`.

## Goals

- Measure non-tool overhead in `handle` without adding a hosted telemetry dependency.
- Characterize advertisement cost, read/write gate behavior, cancellation, and MCP lifecycle.
- Reproduce failures with deterministic barriers rather than sleeps.
- Define and apply explicit go/no-go thresholds.
- Preserve `PythinkerToolset` as the external interface.
- Treat a no-go decision as a successful completed phase.

## Non-goals

- Splitting by line count.
- Changing approval, hook, telemetry, deduplication, visibility, or MCP semantics during characterization.
- Publishing new public interfaces.
- Adding production sampling, remote telemetry, or runtime dependencies solely for benchmarks.
- Optimizing actual tool execution time.

## Characterization harness

The harness uses existing test and benchmark conventions, standard-library monotonic timing, deterministic fake tools, and controlled host/MCP adapters. Measured regions exclude fixture setup and report both raw samples and summary statistics.

Every benchmark records:

- Python and platform information.
- Fixture size and concurrency shape.
- Warm-up count and measured iteration count.
- Median and p95 latency.
- Throughput where meaningful.
- Allocation or retained-object proxy available through existing tooling.
- Cancellation completion and leaked-task count.

Benchmarks are directional engineering gates, not product telemetry. They do not claim universal hardware performance. Timing thresholds use five isolated measured runs after warm-up. A timing threshold is considered crossed only when at least four of five runs cross it and the median result also crosses it. A single outlier produces an inconclusive decision and one complete rerun, not an extraction.

## Execution pipeline measurements

`handle` is measured by phase:

1. Tool lookup and suggestion.
2. JSON parse and canonicalization.
3. Same-step and cross-step deduplication.
4. Permission resolution and approval preparation.
5. Pre-hook execution.
6. Read/write gate wait.
7. Actual tool call.
8. Post-hook and reminder processing.
9. Telemetry and wire completion.

Scenarios include:

- 1, 10, and 100 parallel-safe calls.
- 1, 10, and 100 exclusive calls.
- Mixed reader/writer ordering.
- Duplicate payloads of 1 KiB, 100 KiB, and 1 MiB.
- Fast no-op tools so framework overhead is measurable.
- Tool failure, cancellation, and pre-hook block.

The harness reports non-tool overhead separately from actual tool-call duration.

## Advertisement measurements

The `tools` projection is measured with 50, 500, and 5,000 registered tools under:

- Visibility policy enabled and disabled.
- Hidden and unhidden entries.
- Built-in, plugin, and MCP mixtures.
- Repeated reads without registry changes.
- Registry rebuild after MCP publication.

The output includes p50/p95 projection latency and a deterministic registry hash so caching or extraction cannot change advertised order or collision behavior.

## MCP lifecycle matrix

Scenarios use 1, 10, and 50 configured servers:

- Fast, slow, and hung connect.
- Optional method-not-found response.
- Transient inventory failure.
- Duplicate tool names.
- List-change storms.
- Refresh racing disconnect.
- Disconnect during inventory.
- Cancellation during background load.
- Cleanup racing load.
- Close timeout.

The MCP lifecycle interval begins when `connect_to_mcp_servers` starts its foreground connection work or creates `_mcp_loading_task`, and ends when `wait_for_mcp_tools` completes after final registry publication. The startup-to-ready denominator begins at `Runtime.create` entry and ends at that same settled-inventory point in the same fixture. Measurements include time to first usable inventory, time to settled inventory, task count, leaked task/process count, deterministic published registry hash, cleanup duration, and truthful lifecycle status.

## Deterministic fault tests

Tests coordinate with events and barriers, never arbitrary sleeps, for:

- Pre-hook block and exception.
- Tool cancellation before and after gate admission.
- Post-hook failure.
- Telemetry failure according to existing policy.
- Reader cancellation while queued.
- Writer cancellation while readers drain.
- Refresh racing disconnect.
- Cleanup racing background load.
- MCP publication failure after partial inventory.

Assertions cover explicit failure status, PreToolUse block preservation, no orphan tasks, no leaked sessions, no half-published registry, deterministic cleanup, and later-call recovery.

## Go/no-go thresholds

### Private execution pipeline

Extract a private `_ToolExecutionPipeline` only if either condition is true:

- Non-tool framework overhead exceeds 10 percent of p95 latency for short in-process tools.
- At least three independent recent changes repeatedly modify the same `handle` lifecycle region and the proposed module removes that shared state from `PythinkerToolset`.

Fault characterization must be green before extraction. `PythinkerToolset.handle` remains the compatibility facade.

### Private MCP lifecycle

Extract a private `_McpLifecycle` if any condition is true:

- The defined MCP lifecycle interval exceeds 20 percent of the defined startup-to-ready denominator with 10 configured servers under the repeatability rule.
- Total `cleanup()` time, measured from method entry to return after stop signals are issued, exceeds 6 seconds. This derives from the existing concurrent 5-second per-client close timeout plus 1 second of scheduling allowance and applies to the 1, 10, and 50-server fixtures.
- A task, process, session, or publication leak is reproduced.
- At least three modules require direct MCP lifecycle state.

The private interface may cover configure, defer, start, wait, status, refresh, reconnect, disconnect, close, and inventory publication. It must not own general tool visibility, approval, or execution.

### Private registry

Extract a private `_ToolRegistry` if either condition is true:

- Advertisement exceeds 5 ms p95 at the 500-tool fixture under the repeatability rule. The 5,000-tool fixture is a stress result and cannot trigger extraction by itself.
- Collision, rebuild, or visibility defects recur and one owner would delete duplicated state.

The private interface preserves add, find, hide, unhide, advertised projection, and deterministic MCP rebuild semantics.

### Read/write gate

Keep `_ReadWriteGate` private and colocated unless gate wait exceeds 25 percent of end-to-end p95 in a realistic mixed workload or a second real consumer appears. Optimization alone is not a module seam.

### Mandatory no-go

Do not extract when:

- No threshold is crossed.
- The change only reduces file length.
- Equivalent state remains in both old and new modules.
- The extraction creates a public interface.
- Callbacks introduce a cycle between registry, execution, and MCP lifecycle.
- Characterization or cancellation tests are not green.

## Extraction rules

When a threshold is crossed:

- Write a failing test or benchmark assertion that demonstrates the measured problem.
- Move one coherent state machine at a time.
- Keep `PythinkerToolset` as the caller-facing facade.
- Pass dependencies into the private module; do not create global registries or hidden singletons.
- Preserve exact tool ordering, collision, approval, hook, event, and error behavior.
- Delete the moved state and business logic from `PythinkerToolset` in the same change.
- Re-run characterization and report before/after results with the same fixture.

If the measured result does not improve the target or weakens locality, revert the extraction.

## Test design

Tests and benchmarks are added before any extraction.

- Current reader overlap and writer exclusion remain characterization baselines.
- Cancellation at every queue state releases permits and allows later calls.
- PreToolUse block is never discarded.
- Ordinary tool exceptions retain `ToolRuntimeError` behavior.
- `BaseException` cancellation propagates after cleanup.
- Tool advertisement remains deterministic under insertion and publication changes.
- MCP duplicate-server and duplicate-tool behavior remains compatible.
- Background loading, refresh, reconnect, disconnect, and cleanup leave no tasks or sessions.
- Benchmark output contains fixture metadata and all required phase timings.
- Threshold evaluation emits a deterministic go/no-go decision from benchmark results.

Focused Toolset and concurrency tests run with the characterization suite. Any shipped extraction then runs the full Pythinker Code static and test gates.

## Deliverables

The phase always produces:

1. Characterization fixtures and deterministic fault tests.
2. Benchmark runner and documented invocation.
3. Machine-readable result schema containing fixture metadata and phase timings.
4. Human-readable decision record listing crossed, uncrossed, and inconclusive thresholds, all five run results, the median, and whether the repeatability rule passed.
5. Either a no-go result with no production refactor, or one evidence-supported private extraction.

A no-go result closes the phase. It does not justify searching for a different split until new evidence appears.

## Deletion test

For an extracted private module, deleting it must force its state machine and invariants back into `PythinkerToolset`; a module that only forwards method calls fails the deletion test. The extraction must reduce state ownership and change locality, not merely move lines.

## Rollback

Characterization-only changes are removed if they are flaky, mutate production behavior, or cannot reproduce deterministically. An extraction is reverted if compatibility, failure truthfulness, cancellation, cleanup, or measured target results regress. No hidden flag selects between old and new implementations.
