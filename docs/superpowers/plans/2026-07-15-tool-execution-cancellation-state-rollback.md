# Tool Execution Cancellation State Rollback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent cancelled or failed tool batches from polluting cross-step deduplication and consecutive-call state.

**Architecture:** Keep each batch's call fingerprints uncommitted until watcher settlement succeeds. On cancellation or failure, clear only the engine's current-step state while preserving previously committed fingerprints, completed-result snapshots, and cancellation supervision.

**Tech Stack:** Python 3.14, asyncio, Pythinker batch-tool contracts, pytest/pytest-asyncio, Ruff, Pyright, ty.

## Global Constraints

- Use `uv` or repository `make` targets for every Python command.
- Add no dependency, configuration key, public API, telemetry field, or persisted-data change.
- Preserve ordered results, callback suppression, completion snapshots, bounded cancellation, timeout poisoning, and original exception propagation.
- Keep the production change private to `ToolExecutionEngine` and `_ExecutionBatch`.
- Follow strict TDD: observe the regression test fail for the stale dedup state before editing production code.
- Do not weaken or remove existing cancellation, engine, core, or E2E coverage.

---

### Task 1: Roll Back Unsettled Batch State

**Files:**
- Modify: `src/pythinker_code/soul/tool_execution.py:395-400`
- Modify: `src/pythinker_code/soul/tool_execution.py:832-864`
- Test: `tests/core/test_tool_execution_cancellation.py`

**Interfaces:**
- Consumes: `ToolExecutionEngine.begin_step(...)`, `ToolExecutionEngine.end_step()`, `_ExecutionBatch._run()`, `ToolBatchContext.prior_call_fingerprints`, and `ToolBatchSummary`.
- Produces: internal `ToolExecutionEngine.abort_step() -> None` on the non-exported engine; successful batches remain finalized through `end_step()`, while cancelled or failed batches discard only their current-step dedup state.

- [x] **Step 1: Add the cancellation rollback regression test**

```python
async def test_cancelled_batch_does_not_commit_cross_step_dedup_state() -> None:
    stubborn = CancellationIgnoringTool()
    immediate = ImmediateTool()
    toolset = PythinkerToolset()
    toolset.add(stubborn)
    toolset.add(immediate)

    first = toolset.handle_batch(
        [_call("first", "Immediate")],
        ToolBatchContext(turn_id="turn", step_no=1),
    )
    await first.results()
    prior = first.summary.current_call_fingerprints

    cancelled = toolset.handle_batch(
        [_call("cancelled", "Stubborn")],
        ToolBatchContext(
            turn_id="turn",
            step_no=2,
            prior_call_fingerprints=prior,
        ),
    )
    await stubborn.started.wait()
    settlement = asyncio.create_task(cancelled.cancel_and_settle())
    await stubborn.cancel_seen.wait()
    stubborn.release.set()
    await settlement

    retry = toolset.handle_batch(
        [_call("retry", "Stubborn")],
        ToolBatchContext(
            turn_id="turn",
            step_no=2,
            prior_call_fingerprints=prior,
        ),
    )
    assert [result.tool_call_id for result in await retry.results()] == ["retry"]
    assert retry.summary.dedup_triggered is False
    assert retry.summary.consecutive_identical_call_count == 1
    assert stubborn.invocations == 2
```

- [x] **Step 2: Run the regression test and verify RED**

Run:

```bash
uv run pytest tests/core/test_tool_execution_cancellation.py::test_cancelled_batch_does_not_commit_cross_step_dedup_state -q
```

Expected: FAIL because the retry summary reports `dedup_triggered is True` and consecutive count 2, proving the cancelled call was committed.

- [x] **Step 3: Add a private current-step abort operation**

Add beside `end_step()`:

```python
def abort_step(self) -> None:
    """Discard uncommitted state for the current execution step."""
    if self._step_closed:
        return
    self._current_step_calls = []
    self._current_step_tasks = {}
    self._dedup_triggered = False
    self._step_closed = True
```

The method intentionally leaves `_seen_call_keys`, `_consecutive_key`, and `_consecutive_count`
unchanged because they represent prior successfully committed steps.

- [x] **Step 4: Commit only after successful watcher settlement**

Reshape `_ExecutionBatch._run()` so it awaits watcher results before finalizing:

```python
self._watcher_tasks = [
    asyncio.create_task(self._watch(future)) for future in self._source_futures
]
results = list(await asyncio.gather(*self._watcher_tasks))
self._engine.end_step()
self._summary = self._engine.summary
return results
```

In the `except BaseException` cleanup, retain the existing snapshot/cancel/gather sequence, then
replace fallback `end_step()` finalization with:

```python
self._engine.abort_step()
raise
```

- [x] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
uv run pytest tests/core/test_tool_execution_cancellation.py::test_cancelled_batch_does_not_commit_cross_step_dedup_state -q
uv run pytest tests/core/test_tool_execution_engine.py tests/core/test_tool_execution_cancellation.py -q
```

Expected: the regression passes; all engine and cancellation tests pass with only the documented
third-party Loguru deprecation warning.

- [x] **Step 6: Run package verification**

Run:

```bash
make check-pythinker-core
make test-pythinker-core
make check-pythinker-code
make test-pythinker-code
git diff --check
```

Observed: every command exited 0. The core check passed Ruff, formatting, and Pyright; its
repository-configured non-blocking `ty` invocation continued to report existing third-party typing
diagnostics. The CLI check passed Ruff, formatting, Pyright, and blocking `ty`. Core tests reported
431 passed; CLI tests reported 7,066 passed, 9 skipped, and 1 expected xfail; separate E2E tests
reported 65 passed and 4 skipped. `git diff --check` produced no output.

- [x] **Step 7: Commit the reviewed implementation**

```bash
git add src/pythinker_code/soul/tool_execution.py \
  tests/core/test_tool_execution_cancellation.py
git commit -m "fix(tools): roll back cancelled batch state"
```

---

### Task 2: Bound Owned Async Callback Cancellation

**Files:**
- Modify: `packages/pythinker-core/src/pythinker_core/__init__.py`
- Test: `packages/pythinker-core/tests/test_batch_toolset.py`
- Test: `tests/core/test_tool_execution_cancellation.py`

**Contract:** A caller cancellation gets one finite ownership deadline. Batch settlement receives
the remaining budget, then owned async result callbacks receive only the budget still available.
Cancellation-resistant callbacks remain tracked and failure-observed after timeout, but cannot
block the caller indefinitely or hide an earlier batch timeout.

- [x] Add a regression with an async result callback that catches `CancelledError` and waits for an
      explicit release. Verify the current `tool_results()` cancellation remains pending past the
      intended short bound.
- [x] Implement deadline-aware callback settlement with `asyncio.wait`, not `wait_for(gather(...))`,
      because cancelling a gather can itself wait forever for cancellation-resistant tasks.
- [x] Preserve the first batch/cancellation failure while still cancelling callbacks; add truthful
      timeout reporting for a callback-only timeout and retain pending callback futures until done.
- [x] Update the existing core timeout integration test to control the new owner deadline and run
      the focused core/cancellation suites.

### Task 3: Preserve Tool Lineage on Cancellation Timeout

**Files:**
- Modify: `src/pythinker_code/soul/pythinkersoul.py`
- Test: `tests/core/test_pythinkersoul_turn_balance.py` or a focused sibling module

**Contract:** Both `CancelledError` and `ToolCancellationTimeoutError` repair context before they
propagate. Completed results remain authoritative. Unfinished calls get an interrupted marker for
ordinary cancellation or an explicit completion-unknown/do-not-retry marker for timeout.

- [x] Add a real Soul + real `PythinkerToolset` regression where one tool completes and another
      ignores cancellation through a short deadline. Verify the current timeout path leaves the
      assistant tool calls unanswered.
- [x] Persist the completed snapshot and timeout-specific unknown markers under a shielded context
      write, then re-raise the original typed timeout.
- [x] Verify no duplicate context write, no `_last_tool_calls` commit for the unsettled batch, and
      unchanged ordinary-cancellation wording.

### Task 4: Integrate Late Engine Work into Runtime Cleanup

**Files:**
- Modify: `src/pythinker_code/soul/tool_execution.py`
- Modify: `src/pythinker_code/soul/toolset.py`
- Test: `tests/core/test_tool_execution_cancellation.py`

**Contract:** Toolset cleanup owns engine late-drain observers. It waits within a finite bound,
returns normally when late work drains, and raises `ToolCancellationTimeoutError` after closing MCP
resources when work survives the bound. It never detaches a still-running supervisor by cancelling
its drain observer.

- [x] Add regressions for cleanup waiting until a timed-out tool is released and for bounded,
      truthful failure while the tool remains cancellation-resistant.
- [x] Add an idempotent engine cleanup operation using bounded `asyncio.wait` over retained drain
      tasks and explicit timeout validation/reporting.
- [x] Wire engine cleanup into `PythinkerToolset.cleanup()` while guaranteeing MCP close attempts
      still run before any retained engine error propagates.
- [x] Preserve caller cancellation raised by engine cleanup until after MCP session/client teardown,
      then re-raise the original `CancelledError`.

### Task 5: Documentation, Full Gates, and Re-review

**Files:**
- Modify: `docs/en/customization/agent-architecture.md`
- Modify: this plan, its design, `tasks/todo.md`, and `tasks/lessons.md`

- [x] Update the detailed architecture flow to use `handle_batch()` and `ToolExecutionEngine` for
      Pythinker Soul, with per-call `handle()` labeled as the legacy non-batch fallback.
- [x] Run focused tests for all three Important findings, then the core and CLI full package gates
      and `git diff --check`.
- [x] Re-run task-scoped reviews and a fresh complete branch review. Push only with no open
      Critical/Important findings.
