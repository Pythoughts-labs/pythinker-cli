# Tool Execution Cancellation State Rollback Design

## Goal

Keep cross-step duplicate detection and consecutive-call protection consistent with the tool
results that Pythinker actually accepts into conversation state. A cancelled or failed batch must
not make an uncompleted call look previously completed when the model retries it.

## Confirmed root cause

`_ExecutionBatch._run()` currently calls `ToolExecutionEngine.end_step()` immediately after
dispatch. `end_step()` commits the current call fingerprints into `_seen_call_keys` and advances
the consecutive-call streak before watcher settlement succeeds. If the batch is then cancelled,
`PythinkerSoul` deliberately retains the prior successful batch fingerprints, but the engine keeps
the cancelled call internally. The next retry therefore observes state that conversation history
does not contain.

## Design

Batch state is transactional at the existing execution-engine boundary:

- Dispatch records calls only in the engine's current, uncommitted step state.
- `_ExecutionBatch` waits for every watcher result before calling `end_step()` and publishing a
  finalized `ToolBatchSummary`.
- Cancellation or failure calls an internal engine abort operation after owned futures and watchers
  settle. Abort clears only current-step calls, task references, and the current dedup flag, then
  closes the step so the next batch is initialized from its authoritative `ToolBatchContext`.
- Abort does not modify previously committed fingerprints or the previously committed consecutive
  streak.
- Existing completion snapshots, callback suppression, bounded cancellation, timeout poisoning,
  and exception propagation remain unchanged.

This keeps the change inside the non-exported `ToolExecutionEngine` and `_ExecutionBatch`; it adds
no public API, configuration, dependency, or persisted-data change.

## Failure and edge behavior

- A normally settled batch commits once and continues to expose a finalized ordered summary.
- Cancellation before any result, between results, or while a callback is pending leaves completed
  result snapshots intact but rolls back the batch's dedup state.
- Dispatch or watcher failure rolls back the same state and re-raises the original exception.
- A timed-out cancellation remains poisoned until late work drains; once drained, the aborted call
  is not treated as completed.
- Repeated cancellation and zero-timeout behavior retain their existing contracts.

## Verification

A regression test first completes call A, starts and cancels blocking call B, then retries B while
the authoritative prior context still contains only A. Before the fix the retry is reported as a
cross-step duplicate with consecutive count 2. After the fix it is not a duplicate and its
consecutive count is 1.

Focused cancellation/engine tests run during TDD. The final gate is
`make check-pythinker-code && make test-pythinker-code`, plus the affected core package gates and
`git diff --check` before publication.

## Review-thread closeout

The sole unresolved GitHub Code Quality thread is a false positive: awaiting a cancelled task
inside `pytest.raises(asyncio.CancelledError)` is the observable assertion, not a no-effect
statement. After the code fix is pushed and the latest CodeRabbit review completes, reply with that
evidence and resolve the thread through GitHub GraphQL `resolveReviewThread`.

## Whole-branch review expansion

The final merge-base-to-head review found three additional cancellation-lifecycle gaps introduced
by this PR. They are part of this closeout rather than deferred follow-up work:

1. `StepResult` owns async result callbacks, but a callback that suppresses `CancelledError` can
   make callback settlement wait forever after the batch cancellation deadline expires.
2. `PythinkerSoul` persists completed tool-result lineage only for `CancelledError`. A typed
   `ToolCancellationTimeoutError` therefore skips the same context repair even though some calls
   may have completed and the rest have unknown completion state.
3. The execution engine retains poisoned batches and late-drain tasks, but toolset cleanup does not
   wait for or report those engine-owned tasks.

The robust closeout uses one bounded ownership deadline across a `StepResult` cancellation attempt.
The remaining budget is passed to batch and callback settlement. Callback futures remain owned and
failure-observed until they actually drain; if the deadline expires, cancellation surfaces a typed
timeout instead of hanging or pretending cleanup succeeded.

On either ordinary cancellation or cancellation timeout, Soul persists the assistant tool calls,
every known completed result, and one result for each unfinished call. Ordinary cancellation uses
the existing interrupted marker. Timeout uses an explicit unknown-completion marker that states the
operation may still be running and must not be retried automatically. The original cancellation or
typed timeout is re-raised only after the context write settles.

`ToolExecutionEngine` also exposes an internal async cleanup operation for retained late-drain
tasks. Cleanup waits only to the configured safety bound, reports surviving work with a typed
`ToolCancellationTimeoutError`, and never cancels the drain observer in a way that would detach the
underlying tool supervisor. `PythinkerToolset.cleanup()` performs this engine cleanup and still
closes all MCP resources before propagating a retained engine-cleanup error.

The public architecture page must describe `handle_batch()` and `ToolExecutionEngine` as the
Pythinker Soul path, while keeping per-call `handle()` documented only as the core compatibility
fallback for non-batch toolsets.
