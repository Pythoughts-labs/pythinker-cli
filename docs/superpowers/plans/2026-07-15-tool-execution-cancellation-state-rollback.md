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
- Produces: private `ToolExecutionEngine._abort_step() -> None`; successful batches remain finalized through `end_step()`, while cancelled or failed batches discard only their current-step dedup state.

- [ ] **Step 1: Add the cancellation rollback regression test**

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

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```bash
uv run pytest tests/core/test_tool_execution_cancellation.py::test_cancelled_batch_does_not_commit_cross_step_dedup_state -q
```

Expected: FAIL because the retry summary reports `dedup_triggered is True` and consecutive count 2, proving the cancelled call was committed.

- [ ] **Step 3: Add a private current-step abort operation**

Add beside `end_step()`:

```python
def _abort_step(self) -> None:
    if self._step_closed:
        return
    self._current_step_calls = []
    self._current_step_tasks = {}
    self._dedup_triggered = False
    self._step_closed = True
```

The method intentionally leaves `_seen_call_keys`, `_consecutive_key`, and `_consecutive_count`
unchanged because they represent prior successfully committed steps.

- [ ] **Step 4: Commit only after successful watcher settlement**

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
self._engine._abort_step()
raise
```

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
uv run pytest tests/core/test_tool_execution_cancellation.py::test_cancelled_batch_does_not_commit_cross_step_dedup_state -q
uv run pytest tests/core/test_tool_execution_engine.py tests/core/test_tool_execution_cancellation.py -q
```

Expected: the regression passes; all engine and cancellation tests pass with only the documented
third-party Loguru deprecation warning.

- [ ] **Step 6: Run package verification**

Run:

```bash
make check-pythinker-core
make test-pythinker-core
make check-pythinker-code
make test-pythinker-code
git diff --check
```

Expected: every command exits 0; Ruff, formatting, Pyright, and ty report no errors; all unit and
E2E tests pass.

- [ ] **Step 7: Commit the reviewed implementation**

```bash
git add src/pythinker_code/soul/tool_execution.py \
  tests/core/test_tool_execution_cancellation.py
git commit -m "fix(tools): roll back cancelled batch state"
```
