# Toolset characterization decision

## Decision

**NO-GO: retain `PythinkerToolset`; do not ship a private extraction.**

The primary run crossed the execution-framework threshold. The controlled private
`_ToolExecutionPipeline` attempt did not improve that target and was reverted. The mixed gate ratio also crossed, but
that fixture deliberately holds a reader while a writer waits, so the result describes real barrier
contention rather than framework work a second owner could remove. Per the one-module limit, only a
private `_ToolExecutionPipeline` is eligible for the controlled attempt. No MCP lifecycle or registry
threshold crossed, and the 5,000-tool stress fixture is not used as a trigger.

## Environment and invocation

- Successful measured command: `uv run python scripts/benchmark_toolset.py --scenario all --runs 5 --output docs/superpowers/reports/2026-07-10-toolset-characterization.json`
- Python: CPython 3.14.6
- Platform: macOS 26.5.2, arm64
- Warm-ups: 1 per fixture
- Measured runs: 5 per fixture
- Machine schema: version 2; full all/5 runs derive decisions during report generation
- Machine record: `docs/superpowers/reports/2026-07-10-toolset-characterization.json`
- Directionality: local engineering evidence only; these values are not universal product telemetry.

The first command attempt completed measurement but could not write because the new report directory
did not exist. The directory was created and the full command was run again; no sample from the
failed write was reused.

## Deterministic threshold decisions

All values below were derived from paired raw nanosecond samples in the machine record by the Task 12
`evaluate_threshold` function. A crossing requires at least four of five values above the threshold
and a crossing median. Exactly one crossing is inconclusive and requires one complete five-run rerun.
No primary decision had exactly one crossing, so no rerun was permitted or performed.

| Decision | Threshold | Five primary values | Median | Crossings | Primary | Rerun | Final |
| --- | ---: | --- | ---: | ---: | --- | --- | --- |
| Execution framework overhead, short safe size 1 (%) | 10 | 99.379636, 99.386855, 99.409597, 99.560675, 99.427590 | 99.409597 | 5/5 | crossed | not required | crossed |
| MCP lifecycle / startup-to-ready, 10 servers (%) | 20 | 3.494475, 3.724089, 3.483475, 3.232632, 4.668090 | 3.494475 | 0/5 | uncrossed | not required | uncrossed |
| MCP cleanup, 1 server (s) | 6 | 0.000192875, 0.000158375, 0.000167042, 0.000141875, 0.000135583 | 0.000158375 | 0/5 | uncrossed | not required | uncrossed |
| MCP cleanup, 10 servers (s) | 6 | 0.000337167, 0.000311209, 0.000303209, 0.000349417, 0.002235292 | 0.000337167 | 0/5 | uncrossed | not required | uncrossed |
| MCP cleanup, 50 servers (s) | 6 | 0.001486500, 0.001113583, 0.001131750, 0.001084292, 0.001252167 | 0.001131750 | 0/5 | uncrossed | not required | uncrossed |
| Registry within-run projection p95, 500 tools (ms) | 5 | 3.245000, 2.934625, 3.091542, 4.716500, 3.079834 | 3.091542 | 0/5 | uncrossed | not required | uncrossed |
| Mixed gate wait / end-to-end, 10 pairs (%) | 25 | 89.705807, 89.779066, 89.454402, 89.823940, 89.565488 | 89.705807 | 5/5 | crossed | not required | crossed |

Each registry value is the nearest-rank p95 of twenty individually timed, visibility-enabled,
unchanged 500-tool projections from that run. The machine report preserves all five groups of twenty
raw samples. The decision excludes both single-sample hidden/unhidden timings and the 5,000-tool
stress result.

## Safety and fault matrix

- Every scenario reported zero leaked tasks, processes, and sessions.
- Every execution scenario reported completed queued-reader cancellation, queued-writer cancellation,
  and later-call recovery.
- Registry hashes were stable within every five-run fixture; the harness rejects a run if hashes vary.
- Deterministic event/barrier coverage exercises PreToolUse block preservation under telemetry failure,
  hook failure policy, post-hook isolation, queued and admitted cancellation, permit recovery, optional
  MCP method absence versus transient failure, list-change storms, refresh racing disconnect, hung
  connect, duplicate server and tool names, background-load cleanup, close failure/timeout, partial
  connection, and exception-atomic publication rollback.
- A fault test reproduced a real half-publication defect in `_rebuild_published_mcp_tools`; the scoped
  fix restores both public registries before re-raising the publication error.

## Non-timing locality and consumer evidence

- `git log --since=2026-04-01 -- src/pythinker_code/soul/toolset.py` shows repeated lifecycle edits.
  In particular, `2904de00` changed deduplication inside `handle`, `5e505087` added gate execution and
  approval/event behavior, `63faa855` changed skip-event policy, and `e93c0c17` changed execution
  telemetry. This satisfies the independent recent-change locality trigger for one controlled
  execution-pipeline attempt.
- Targeted private-state search found no second production owner of `_concurrency_gate` or
  `_current_step_tasks`; only the characterization probe subclasses the Toolset for measurement.
- MCP consumers use the Toolset facade (`mcp_status_snapshot`, `wait_for_mcp_tools`, refresh,
  disconnect, reconnect, and `mcp_servers`). No production module directly owns `_mcp_loading_task`.
  The public MCP state has multiple consumers, but direct lifecycle state remains local to Toolset,
  so consumer count does not trigger `_McpLifecycle` extraction.

## Controlled extraction result

The attempt moved the reader/writer gate and its execution call ownership into the only permitted
private `_ToolExecutionPipeline`, removed the equivalent gate state/logic from `PythinkerToolset`,
and kept `PythinkerToolset.handle` as the facade. The focused fault matrix remained green (68 tests).

Against the original primary median of 99.568352 percent, the identical short-safe size-1 extraction
fixture produced framework-overhead ratios of 99.519399,
99.606995, 99.589209, 99.645530, and 99.624502 percent; median 99.606995 percent. The attempt was
slightly worse, remained far above the 10 percent threshold,
and moved only gate admission rather than the broader frequently changed handle lifecycle. It
therefore failed both the measured-target improvement rule and the locality/depth test.

The post-review schema-v2 rerun emitted decisions directly from the benchmark command and again
crossed the execution threshold, with a 99.409597 percent median. This does not change the failed
extraction comparison or authorize a second attempt.

The private module, compatibility projection, and probe adaptation were reverted in full. The final
decision is **NO-GO**: retain `PythinkerToolset` and the colocated `_ReadWriteGate`. Keep the
characterization/fault tests, machine and human evidence, and exception-atomic publication fix. New
evidence is required before proposing another split.
