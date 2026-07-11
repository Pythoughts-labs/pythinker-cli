# Toolset characterization

The Toolset characterization harness is a directional, local engineering aid. It uses
deterministic no-op tools and fake MCP-style inventories to make Pythinker's framework costs
visible without network access, credentials, hosted telemetry, or new runtime dependencies. Its
numbers describe the machine and checkout that produced them; they are not universal product
performance claims.

Task 12 establishes the schema, evaluator, and measurement harness. It deliberately makes no
extraction decision. Task 13 runs the full fault matrix and real five-run measurements before any
private `_ToolExecutionPipeline`, `_McpLifecycle`, or `_ToolRegistry` extraction may be considered.

## Run it

From the repository root:

```bash
uv run python scripts/benchmark_toolset.py --scenario all --runs 5 \
  --output toolset-characterization.json
```

Use `--scenario execution`, `dedupe`, `advertisement`, or `mcp` to select one family. The default is
`all`. `--smoke` selects the smallest fixture in each chosen family and is intended only to validate
the runner and schema:

```bash
uv run python scripts/benchmark_toolset.py --scenario all --runs 1 --smoke
```

Without `--output`, the report is written as JSON to standard output. A non-positive run count, an
unknown scenario, or an unwritable output path exits nonzero.

## Fixtures and intervals

Fixture construction and warm-up are outside every named measured interval. All clocks use
`time.monotonic_ns()`.

- Execution runs parallel-safe and exclusive calls at concurrency 1, 10, and 100 through the public
  `PythinkerToolset` facade. For `execution_mixed`, fixture `size` is a reader/writer pair count:
  sizes 1, 10, and 100 therefore use concurrency and expected operation counts 2, 20, and 200.
  `end_to_end` starts immediately before `handle` dispatch and ends after all returned results
  settle. `tool_call` records absolute entry/exit intervals inside
  deterministic no-op tools; overlapping intervals are merged before their critical-path duration
  is subtracted from `end_to_end` for `framework_overhead`. Each call's read/write gate wait begins
  immediately before requesting the shared or exclusive context and ends immediately after
  admission, before `tool.call`. `read_write_gate_wait` is the union of those absolute wait
  intervals, so overlapping queued calls count once and remain directly comparable with
  `end_to_end` for the 25 percent gate. Every mixed scale uses an event-held reader from the first
  pair and queues its exclusive writer before releasing it; the remaining pairs are dispatched in
  call order behind that barrier, making contention deterministic without sleeps. The remaining
  named lifecycle subphases are present with `measurement_status: unmeasured`
  and an explanation because current Toolset exposes no stable boundary that would isolate them
  without changing production behavior.
- Dedupe dispatches two same-step calls with identical 1 KiB, 100 KiB, or 1 MiB payloads. It uses
  the same execution intervals and keeps payload construction outside the measured region.
- Advertisement projects 50, 500, and 5,000 real built-in, `PluginTool`, and `MCPTool` categories.
  It records visibility policy enabled/disabled with hidden/unhidden entries, three aggregate reads
  plus twenty individually timed repeated reads without a registry change, and a rebuild after
  deterministic MCP publication. Each measured run records all twenty raw projection samples and
  their nearest-rank p95; the registry repeatability decision uses the five within-run p95 values,
  never a single projection or the 5,000-tool stress result. Setup remains
  outside every named projection interval. Category and projection counts make the fixture behavior
  independently checkable; the registry hash length-prefixes both policy projections in order.
- MCP runs the current background `load_mcp_tools`/`wait_for_mcp_tools`/`cleanup` lifecycle for 1,
  10, and 50 configured servers, replacing network connection with a deterministic local inventory
  adapter. `mcp_lifecycle` starts immediately before background loading and ends when
  `wait_for_mcp_tools` settles after final registry publication. `time_to_first_inventory` ends when
  the inventory is visible through the public Toolset registry (the local all-at-once publication
  can make first and settled effectively the same boundary), and the report records the visible
  count at that exact publication boundary; `time_to_settled_inventory` ends after
  `wait_for_mcp_tools` returns; and `cleanup` covers `PythinkerToolset.cleanup()` entry through
  return. `startup_to_ready` begins immediately before the actual `Runtime.create` call and ends at
  that same settled-inventory point.

Concurrency and cancellation coordination use asyncio events and task completion, never timing
sleeps. The harness reports cancellation completion and post-run task leakage. Its fake adapters do
not start processes or sessions, so those leak counts remain explicit zeros. Cancellation probes
enter real queued reader and writer states through `PythinkerToolset.handle`, cancel them, then prove
that a later call recovers. Task 13 exercises the full failure matrix.

## Repeatability and reruns

Threshold decisions use exactly five isolated measured runs after warm-up. A threshold is crossed
only when at least four of five values exceed it and the median also exceeds it. No crossings means
uncrossed. Two or three crossings also remain uncrossed because the repeatability rule did not pass.
A single crossing is treated as an outlier and therefore inconclusive; it requests one complete
five-run rerun. The rerun replaces the inconclusive set for the decision, and a second rerun is never
requested.

The pure evaluator records both primary and rerun values, the selected crossing count and median,
the final `crossed`, `uncrossed`, or `inconclusive` state, and whether a rerun is still required.

## Extraction thresholds

Task 13 applies these approved gates with the repeatability rule:

- Execution pipeline: non-tool framework overhead exceeds 10 percent of p95 latency for short
  in-process tools, or three independent recent changes repeatedly touch the same lifecycle region
  and extraction deletes that shared state.
- MCP lifecycle: the defined lifecycle interval exceeds 20 percent of startup-to-ready with ten
  servers; cleanup exceeds six seconds; a task, process, session, or publication leak is reproduced;
  or three modules directly require MCP lifecycle state.
- Registry: advertisement exceeds 5 ms p95 at 500 tools, or recurring collision/rebuild/visibility
  defects would be eliminated by one owner. The 5,000-tool stress fixture cannot trigger extraction
  by itself.
- Read/write gate: gate wait exceeds 25 percent of end-to-end p95 in a realistic mixed workload, or
  a second real consumer appears.

No threshold crossing, file length alone, duplicate old/new state, a new public interface, a
callback cycle, or a red characterization/cancellation test mandates no extraction.

## JSON schema

The versioned report contains:

- `environment`: Python version and implementation plus platform information;
- `scenarios[].fixture`: scenario kind, size, concurrency, payload size, and composition; mixed
  execution explicitly reports `composition: reader/writer pairs`;
- `warmups` and `iterations`;
- `phases`: measured/unmeasured status, raw nanosecond samples, optional raw within-run sample
  groups, median, nearest-rank p95, throughput where meaningful, and an explanation for unavailable
  boundaries;
- deterministic `registry_hash`;
- `allocation_peak_bytes` and `retained_object_delta` from `tracemalloc`;
- `cancellation`: completion status and completion duration;
- `leaks`: pending task, process, and session counts;
- `task_count_peak`: the maximum observed asyncio task count during a measured sample;
- `operation_count`, `category_counts`, and `projection_counts`: independently checkable fixture
  execution and advertisement behavior;
- `lifecycle_status`: `completed` for local execution/registry fixtures or `settled` after the fake
  MCP inventory is fully published; and
- `decisions`: threshold, five primary values, optional five-value rerun, crossing count, median,
  primary/rerun/final states, and rerun-required state.

Partial, smoke, and non-five-run reports keep an empty `decisions` array. The documented full
`--scenario all --runs 5` command deterministically derives all seven Task 13 decisions from the raw
scenario samples and writes them in the same report. A human-readable crossed/uncrossed/inconclusive
decision record remains a Task 13 deliverable after deterministic fault tests are complete.
