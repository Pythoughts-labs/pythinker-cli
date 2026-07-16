# Tool-execution engine before/after characterization

## Decision

**GO for the behavior-preserving extraction, with an explicit local overhead finding.**

The private execution engine preserved every deterministic safety outcome, every threshold decision,
and the `PythinkerToolset` registry/MCP facade. It did not improve local timing. Median execution
fixtures were 3.6–14.5% slower, deduplication fixtures were 19.4–20.7% slower, and registry projection
was 3.8–4.2% slower in this sequential local run. These are absolute increases of roughly 0.05–26 ms
for execution fixtures and 0.30–1.22 ms for deduplication fixtures. No performance improvement is
claimed.

The extraction ships for the approved ownership and cancellation guarantees: terminal batch
construction, ordered result supervision, bounded cancellation, fail-closed poison while late tasks
drain, and one authoritative batch summary. The benchmark's existing decision states did not change;
all leak and cancellation checks remained green. The measured overhead remains a residual risk to
watch in future characterization.

The 2026-07-10 report's NO-GO applied to a narrower gate-only `_ToolExecutionPipeline` experiment that
failed to deepen the module. This change moves the full execution state machine and adds the approved
batch/cancellation contract; it does not reinterpret the earlier timing result as a performance win.

## Compared revisions and invocation

- Before: `10aedf26` (`main` after PR #206)
- After: `719322d4` (Task 4 head; Task 5 changes are report-only)
- Python: CPython 3.14.6
- Platform: macOS 26.5.2, arm64
- Warm-ups: 1 per fixture
- Measured runs: 5 per fixture
- Command, run once in each worktree:

  ```bash
  uv run python scripts/benchmark_toolset.py --scenario all --runs 5 --output <report>.json
  ```

- Before machine record: [`2026-07-15-tool-execution-before.json`](./2026-07-15-tool-execution-before.json)
- After machine record: [`2026-07-15-tool-execution-after.json`](./2026-07-15-tool-execution-after.json)
- Directionality: local engineering evidence only, not universal product telemetry.

The before run completed first in the base worktree; the after run then used the same machine, Python,
fixture matrix, and five-run protocol. No sample was discarded or selectively rerun.

## Median timing comparison

| Fixture | Before | After | Delta |
| --- | ---: | ---: | ---: |
| Safe execution, size 1 | 1.484 ms | 1.689 ms | +13.8% |
| Safe execution, size 10 | 14.030 ms | 15.267 ms | +8.8% |
| Safe execution, size 100 | 140.739 ms | 156.571 ms | +11.2% |
| Exclusive execution, size 1 | 1.460 ms | 1.513 ms | +3.6% |
| Exclusive execution, size 10 | 13.882 ms | 14.658 ms | +5.6% |
| Exclusive execution, size 100 | 138.604 ms | 150.126 ms | +8.3% |
| Mixed execution, 1 pair | 2.866 ms | 3.282 ms | +14.5% |
| Mixed execution, 10 pairs | 27.182 ms | 30.005 ms | +10.4% |
| Mixed execution, 100 pairs | 285.880 ms | 311.995 ms | +9.1% |
| Deduplication, 1 KiB | 1.517 ms | 1.816 ms | +19.7% |
| Deduplication, 100 KiB | 1.926 ms | 2.299 ms | +19.4% |
| Deduplication, 1 MiB | 5.879 ms | 7.096 ms | +20.7% |
| Registry projection p95, 50 tools | 0.298 ms | 0.310 ms | +4.2% |
| Registry projection p95, 500 tools | 3.129 ms | 3.248 ms | +3.8% |
| Registry projection p95, 5,000 tools | 32.233 ms | 33.553 ms | +4.1% |
| MCP startup-to-ready, 1 server | 1827.940 ms | 1717.103 ms | -6.1% |
| MCP startup-to-ready, 10 servers | 1894.863 ms | 1821.090 ms | -3.9% |
| MCP startup-to-ready, 50 servers | 2167.554 ms | 2220.232 ms | +2.4% |

The machine records contain all raw samples, phase timings, within-run projections, cancellation
measurements, hashes, environment fields, and operation counts.

## Deterministic decision comparison

| Decision | Threshold | Before median/state | After median/state |
| --- | ---: | --- | --- |
| Execution framework overhead, short safe size 1 | 10% | 99.5059%, crossed | 99.4994%, crossed |
| MCP lifecycle/startup, 10 servers | 20% | 3.5190%, uncrossed | 3.6227%, uncrossed |
| MCP cleanup, 1 server | 6 s | 0.000135 s, uncrossed | 0.000137 s, uncrossed |
| MCP cleanup, 10 servers | 6 s | 0.000311 s, uncrossed | 0.000287 s, uncrossed |
| MCP cleanup, 50 servers | 6 s | 0.001047 s, uncrossed | 0.001072 s, uncrossed |
| Registry projection p95, 500 tools | 5 ms | 3.1288 ms, uncrossed | 3.2478 ms, uncrossed |
| Mixed gate wait/end-to-end, 10 pairs | 25% | 89.7602%, crossed | 89.4886%, crossed |

Every decision had either zero or five crossings; neither report permitted an inconclusive rerun.

## Safety and fault outcomes

- All 18 before scenarios and all 18 after scenarios reported zero leaked tasks, processes, and
  sessions.
- Every execution fixture reported completed cancellation, queued-reader completion, queued-writer
  completion, and successful later-call recovery before and after.
- Cancellation medians remained bounded at 6.98–8.47 ms after extraction across the harness fixtures.
- Registry hashes remained stable within every fixture.
- Focused cancellation tests additionally cover a tool that ignores cancellation, timeout poisoning,
  blocked new batches, late drain recovery, repeated caller cancellation, completed-result snapshots,
  callback deactivation, and absence of unhandled task warnings.
- Full Task 4 validation passed 7,060 root tests and 65 e2e tests.

## Residual risk

The harness exercises the compatibility `handle()` path and deterministic local no-op tools; it is
not a production workload and does not isolate each added coroutine/frame. The consistent timing
increase is nevertheless treated as real directional evidence, not dismissed as noise. Future
changes to execution scheduling should rerun the same all/5 comparison and should prefer reducing the
measured overhead without weakening task ownership, cancellation bounds, or facade compatibility.
