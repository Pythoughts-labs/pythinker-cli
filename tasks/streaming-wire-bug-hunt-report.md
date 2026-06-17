# Targeted Streaming/Wire Bug Hunt Report

**Date:** 2026-06-17 · **Branch:** `feat/lsp-implementation-capability-guard`
**Reviewed state:** streaming fix committed as `6f50c5c fix(tui): preserve streaming finalize
continuity and fence safety` (this commit landed *during* the review; findings are verified
against HEAD `6f50c5c`).

> **Working-tree volatility note.** The streaming diff that was uncommitted at the start of this
> review (prompt.py, _interactive.py, _live_view.py, the three test files, CHANGELOG) was committed
> as `6f50c5c` mid-review, and a *separate, live* uncommitted change is now adding **more** of the
> same hardcoded-path debug logging (`_agent_block_debug_log`) to `_blocks.py`, plus new untracked
> tool-renderer files (`lsp.py`, `mcp_resource.py`, `worktree.py`). A parallel editing session is
> active in this repo. Fixes were **not** applied to avoid clobbering that live work — see
> "Merge Recommendation".

## Scope

Inspected (target list):
- `src/pythinker_code/ui/shell/visualize/_live_view.py`
- `src/pythinker_code/ui/shell/visualize/_interactive.py`
- `src/pythinker_code/ui/shell/visualize/_blocks.py`
- `src/pythinker_code/ui/shell/visualize/streaming.py` — **does not exist.** No such module. The
  streaming logic lives in `_blocks.py` and `src/pythinker_code/ui/shell/markdown/streaming.py`
  (`markdown_commit_boundary`). Target path is invalid; treated `markdown/streaming.py` as the
  one-hop equivalent.
- `src/pythinker_code/ui/shell/prompt.py`
- `src/pythinker_code/soul/pythinkersoul.py`
- `src/pythinker_code/soul/__init__.py`
- `src/pythinker_code/wire/__init__.py`
- `src/pythinker_code/ui/console.py` (no findings; `render_to_ansi` consumed by the views)
- `tests/ui_and_conv/test_stream_pacing.py`, `test_streaming_content_block.py`,
  `test_visualize_running_prompt.py`

One-hop expansions (forced by call graph):
- `ui/shell/components/report_update.py` (`looks_like_report_update` / `parse_report_update`) — to
  resolve the report_update double-emission question.
- `ui/shell/markdown/streaming.py` (`markdown_commit_boundary`) — boundary semantics for H3.
- `utils/broadcast.py` + `tests/utils/test_broadcast_queue.py` — wire transport drop/buffer (H8).

## Executive Summary

- **Critical:** 1 — F-01 committed machine-specific, ungated, hot-path debug-log writer (the 19 MB
  `.cursor/debug-e13c80.log`), now being *expanded* by live uncommitted work.
- **High:** 0
- **Medium:** 2 — F-02 19 MB debug log untracked but not git-ignored; F-03 `_compose_composing`
  row-budget loop re-renders to ANSI up to ~12×/compose (redundant with prompt-side row fitting).
- **Low:** 1 — F-04 report_update finalize re-renders from full `raw_text` (safe today; latent).
- **Not bugs / verified safe:** H1 (pacing moved to base view), H3 (`_last_commit_scan_len`
  optimization), H4 (FlushReason policy), H5 (incremental commit / no double-emission / no loss),
  H6 (compaction wire pairing), H7 (0.5 s UI shutdown), H8 (wire buffering — no event loss),
  H9 (token-rate accounting).

## Findings

### F-01 — Committed machine-specific, ungated, hot-path debug-log writer
**Severity:** Critical (merge blocker) — maps to AGENTS.md tripwire family C12/C02 and the task's
hypothesis #10.
**Files:**
- `src/pythinker_code/ui/shell/prompt.py:1876` `_AGENT_PROMPT_DEBUG_LOG_PATH =
  "/Users/panda/Projects/active/Projects/pythinker-code-main/.cursor/debug-e13c80.log"`
- `prompt.py:1883` `_agent_prompt_debug_log(...)` — **no env gate**; always builds the payload and
  `open(..., "a")`.
- Call sites: `prompt.py:799` inside `_fit_formatted_text_to_rows` (unconditional) and
  `prompt.py:3298` inside `CustomPromptSession._render_agent_prompt_message` (guarded only by
  `if agent_status_rows or body_rows or pinned_rows`). Both are per-prompt-render hot paths.
- Dead support locals computed only to feed the log: `agent_status_rows` (`prompt.py:3279`) and
  `body_rows` (`prompt.py:3284`; `body_rows` is reassigned at `:3322` before any real use, and the
  non-modal branch never reads it).
- **Live uncommitted expansion:** `_blocks.py` (working tree) is adding `_agent_block_debug_log`
  with the **same** `/Users/panda/.../.cursor/debug-e13c80.log` path.
- Env-gated sibling: `_blocks.py:189-190` `_STREAM_PACING_DEBUG` /
  `_STREAM_PACING_LOG = "/tmp/pythinker-stream-pacing.log"`, written by `_blocks.py:863`
  `_log_pacing_event` (called from `append`/`reveal_tick`/`reveal_all`/`drain_for_transition`/
  `prepare_for_finalize`). Gated off by default but `/tmp` is POSIX-only and unbounded.
- Dead debug method: `_interactive.py:147` `_debug_content_state` — defined, **never called**
  (verified: 0 call sites in `src/` or `tests/`).

**Evidence:** `git show HEAD:.../prompt.py | grep -c _agent_prompt_debug_log` → 3.
`git log -S_AGENT_PROMPT_DEBUG_LOG_PATH` → introduced by `6f50c5c`. On-disk artifact:
`.cursor/debug-e13c80.log` = 19 MB / 41 155 lines, every line `runId:"post-fix"`,
`location:"...prompt.py:..._render_agent_prompt_message"`.

**Reachability:** Direct. `_render_agent_prompt_message` / `_fit_formatted_text_to_rows` run on
every interactive prompt repaint. On this machine that is the 19 MB log; on any other machine the
parent dir is absent so every call raises `FileNotFoundError` (caught + swallowed) — i.e. a
silently-failing FS syscall per render, still pure overhead and dead weight.

**Why it matters:** Machine-specific absolute path, unbounded growth, hot-path filesystem writes,
and writes into the partially-tracked `.cursor/` directory. It is investigation scaffolding for the
H8/H9 hunt that was committed (and is being further expanded) rather than stripped. Violates the
"no hot-path FS writes / env-gated, bounded, non-machine-specific" rule.

**Recommended fix:** Remove all of it as one surgical cleanup, since it is all artifacts of the same
investigation: `prompt.py` (`_AGENT_PROMPT_DEBUG_*`, `_agent_prompt_debug_log`, both call sites, and
the now-dead `agent_status_rows`/`body_rows` locals); `_blocks.py` (`_STREAM_PACING_DEBUG`,
`_STREAM_PACING_LOG`, `_log_pacing_event` + its 5 call sites, the uncommitted `_agent_block_debug_log`,
and the now-unused `import os`); `_interactive.py` (`_debug_content_state`). Keep `random`/`time`/`json`
imports only where still used elsewhere (ruff will confirm). Then delete `.cursor/debug-e13c80.log`.

**Test coverage needed:** `tests/test_ai_static_requirements.py`-style guard: assert no
`src/pythinker_code/**` source contains a `/Users/` absolute path or an unconditional
`open(<hardcoded>, "a")` on a render path. (A static scan is the right gate — a unit test cannot
catch "someone re-adds a hardcoded debug path".)

### F-02 — 19 MB debug log is untracked but NOT git-ignored
**Severity:** Medium.
**Files:** `.cursor/debug-e13c80.log` (19 MB), `.gitignore` (only ignores
`src/pythinker_code/deps/tmp`; no `.cursor` entry). `.cursor/` is already partially tracked
(`.cursor/rules/...`, `.cursor/settings.json`).
**Evidence:** `git check-ignore .cursor/debug-e13c80.log` → not ignored; `git ls-files .cursor/`
shows tracked siblings.
**Reachability:** A `git add .` / `git add -A` would stage a 19 MB machine-local log.
**Why it matters:** Accidental commit of a large machine-local artifact into a tracked directory.
**Recommended fix:** Delete the log and add `.cursor/debug-*.log` (and consider `/tmp`-style debug
logs) to `.gitignore`. Note: `tasks/streaming-render-rootcause.md` is also untracked-not-ignored,
but it is a useful design doc — leave it (or git-ignore `tasks/` if that matches repo convention).
**Test coverage needed:** none (hygiene).

### F-03 — `_compose_composing` row-budget loop re-renders to ANSI repeatedly (perf design-risk)
**Severity:** Medium — design/perf risk, **not** a correctness bug.
**Files:** `_blocks.py:_compose_composing` (the `while True:` budget loop) →
`_blocks.py:916 _renderable_row_count` → `render_to_ansi`; interacts with
`_interactive.py:render_running_prompt_body` (which calls `render_to_ansi` again) and
`prompt.py:_fit_formatted_text_to_rows` (which row-clips a third time).
**Evidence:** When `_preview_row_budget` is set (interactive), the loop calls `render_to_ansi` once
per iteration — up to `_COMPOSING_PREVIEW_LINES` (12) decrements plus one per committed-block pop —
to measure height, then `render_running_prompt_body` renders the result again, then
`_fit_formatted_text_to_rows` clips again. Runs per `prompt_session.invalidate()` (≈25 fps while
streaming).
**Reachability:** Every interactive streamed turn whose preamble exceeds the row budget (long
output / small terminal).
**Why it matters:** Redundant full-renderable ANSI rendering on a 25 fps hot path; can cost CPU and
introduce input lag on slower machines / large outputs. The task's hypothesis #2 flagged exactly
this "new row-budget/render-to-ANSI loop redundant with prompt.py preamble fitting."
**Recommended fix (local, optional):** measure rows from a cached single render instead of
re-rendering each iteration (e.g. compute committed/preview row counts once and trim arithmetically),
or memoize `_renderable_row_count` by renderable identity. Do **not** rewrite the view. Defer unless
profiling shows real lag — it is correct as written.
**Test coverage needed:** a perf/`render_to_ansi`-call-count assertion if fixed; otherwise a comment
documenting the deliberate cost ceiling.

### F-04 — report_update finalize re-renders from full `raw_text` ignoring `_committed_len` (latent)
**Severity:** Low — verified safe today; defensive note.
**Files:** `_blocks.py:858 _render_report_update_body` (`parse_report_update(self.raw_text)`),
called first in `_blocks.py:promote_to_scrollback`.
**Evidence:** Repro (`/tmp/repro_report_update_double.py` + chunk-size sweep) shows a report_update
content block commits **0** blocks incrementally across chunk sizes 1, 4, 8, 16, 64, full — so its
prose is never emitted to scrollback before the card. XOR check (probe text in exactly one of
{incremental, final}) held for both report_update and generic prose at every chunk size: **no
duplication, no loss.**
**Reachability:** Not reachable today. Becomes reachable only if a future change causes a
report_update block to commit leading prose incrementally (`take_committed_renderables` → emitted),
because `_render_report_update_body` re-renders the **entire** `raw_text` (it ignores
`_committed_len`) and would re-include the already-emitted prose.
**Why it matters:** Safe-by-accident: the no-duplication property rests on report_update happening
to commit 0 incremental blocks, not on an explicit guard.
**Recommended fix:** none required now. Optionally add a regression test pinning "report_update
emits 0 incremental commits and exactly one card," so the invariant is enforced rather than
incidental.

## Verified Safe Invariants

- **H1 — pacing moved to base `_LiveView`.** `_live_view.py:220` now sets
  `_stream_pacing = smooth_streaming_enabled() and not reduced_motion_enabled()` in the base
  `__init__` (was hard `False`); the duplicate assignment was removed from `_PromptLiveView`.
  **Safe:** the base view runs its own reveal loop `_frame_refresh_loop` (`_live_view.py:287`,
  task-started at `:396` in `visualize_loop`) which calls `advance_stream_reveal()` every
  `STREAM_FRAME_INTERVAL_S`; `_PromptLiveView` runs `_status_refresh_loop` (`_interactive.py:242`,
  started `:375`). Every view that gets `_stream_pacing=True` therefore has a tick driver — no
  blank-then-`reveal_all` dump. Print/ACP do not use `_LiveView` at all (grep of `ui/print/`,
  `acp/` for `_LiveView`/`advance_stream_reveal`/`reveal_tick` → empty), so non-Live consumers are
  unaffected.
- **H3 — `_flush_committed` `_last_commit_scan_len` optimization.** Skips the expensive
  `markdown_commit_boundary` re-parse until a new `\n` appears beyond the last scanned length, and
  re-scans the **full** pending when it does. Since any new committable boundary necessarily
  coincides with a new newline, no boundary is ever permanently missed. **Verified:** content is
  preserved (XOR) across chunk sizes 1–145; only commit *timing/granularity* varies with chunking.
- **H4 — FlushReason policy honored.** `prepare_for_finalize` (`_blocks.py:660`): TURN_END / CANCEL
  / ERROR → `reveal_all()`; TOOL_START / TEXT_TO_THINK / THINK_TO_TEXT → bounded
  `drain_for_transition()`. `drain_for_transition` **is wired** (not dead): `_interactive.py:327`
  `_drain_content_for_transition` (bounded by `_TRANSITION_DRAIN_MAX_TICKS=12`) and
  `prepare_for_finalize`. `reveal_all()` intentionally does not call `_flush_committed`; final
  completeness comes from `promote_to_scrollback` using `_pending_text_for_final()` =
  `raw_text[_committed_len:]` (the full uncommitted tail), so no text is stranded behind the reveal
  cursor at finalize.
- **H5 — incremental commit + idempotent promotion.** `take_committed_renderables` empties
  `_committed_renderables`; `promote_to_scrollback` is guarded by `_promoted_to_scrollback` and uses
  the remaining (post-take) committed list + `_pending_text_for_final`. **No double emission, no
  loss** (verified empirically). `_PromptLiveView._emit_incremental_content_commits` prints stable
  slices above the prompt via `run_in_terminal` then `invalidate()` — correct prompt-toolkit
  paint-before-print ordering. `_LiveView` (Rich Live) never takes committed renderables, so its
  finalize path is unchanged.
- **H6 — compaction wire pairing.** `pythinkersoul.py:2437` `wire_send(CompactionBegin())` then
  `try: … except Exception: track(success=False); raise finally: wire_send(CompactionEnd())`
  (`:2541-2544`) — `CompactionEnd` always fires, even on failure. The inner `except`
  (`:2518-2529`) restores `history_before_compaction` after `clear()`, so an I/O fault cannot
  truncate live context to just the system prompt. No missing-end / double-restore.
- **H7 — UI shutdown ≤ 0.5 s.** `soul/__init__.py:266` `wire.shutdown()` then `:269`
  `await asyncio.wait_for(ui_task, timeout=0.5)`; `TimeoutError` is caught and the task is cancelled
  by `wait_for`. The bounded transition drain (≤12 × `stream_reveal_interval_s` < 0.5 s) cannot
  block past the hard cap.
- **H8 — wire backpressure / event loss.** `WireSoulSide.send` → `BroadcastQueue.publish_nowait` →
  `Queue.put_nowait` on an **unbounded** queue. Events are **buffered, never dropped or blocked**;
  the branch adds `test_publish_nowait_buffers_for_slow_subscriber` asserting 100 messages buffer
  for a slow subscriber with zero loss. So content deltas, tool-call parts, merge buffers, and
  `CompactionEnd` are not lost. (Pre-existing theoretical risk: unbounded growth if a consumer hangs
  permanently — not introduced by this change.)
- **H9 — token-rate accounting.** `_record_token_rate_sample` (`_blocks.py:896`) uses a sliding
  ~1.5 s window with float cumulative tokens; returns `None` until `_TOKEN_RATE_MIN_SAMPLES`, and on
  non-positive elapsed/delta — no negative/stale rate. Rate display stops at finalize because
  `flush_content` sets `_current_content_block = None`, after which `render_pinned_status_tail` falls
  back to `_working_indicator()`. Unchanged by this branch.

## Test Results

```
uv run pytest -q tests/ui_and_conv/test_stream_pacing.py \
  tests/ui_and_conv/test_streaming_content_block.py \
  tests/ui_and_conv/test_visualize_running_prompt.py
# 209 passed, 1 warning in 0.76s
```

Repro scripts (temporary, `/tmp`): `repro_report_update_double.py` and an inline chunk-size sweep —
both confirm no double emission / no loss (F-04 safe today).

Not yet run (required before any PR per AGENTS.md pre-PR gate): full
`make check-pythinker-code && make test-pythinker-code` plus `tests_e2e`.

## Recommended additional tests (task ask)

- `_PromptLiveView` incremental commit emission (not only base `_LiveView`): assert
  `_emit_incremental_content_commits` emits committed slices once and that finalize emits only the
  remaining tail (no overlap). *(Partial coverage exists at
  `test_visualize_running_prompt.py:195`.)*
- `prepare_for_finalize(TOOL_START)` bounded drain: assert it calls `drain_for_transition` (bounded),
  not `reveal_all`, and that scrollback still contains the complete tail via `_pending_text_for_final`.
- `prepare_for_finalize(TURN_END)` full reveal: assert `reveal_all` + complete promotion.
- No double scrollback after incremental commits (general + report_update) — lock the XOR property.
- No prompt overlay / paint-before-print: assert `_emit_incremental_content_commits` uses
  `run_in_terminal` + `invalidate`.
- Dropped/queued wire events around paired compaction (CompactionBegin/End survive a slow consumer).
- Shutdown within the 0.5 s UI-task contract.
- Static guard: no hardcoded `/Users/` path or unconditional hot-path `open(..., "a")` in
  `src/pythinker_code/**` (F-01 regression guard).

## Merge Recommendation

**Block merge** until F-01 is removed (committed debug scaffolding with a machine-specific path +
ungated hot-path FS writes + the 19 MB `.cursor/debug-e13c80.log`, and the live uncommitted
expansion of the same). F-02 is part of the same cleanup. F-03 and F-04 are non-blocking follow-ups.

**Do not apply the F-01 fix blindly right now:** a parallel session is actively editing `_blocks.py`
(adding more of the same debug logging) and creating new tool-renderer files. Removing the debug
scaffolding while those edits are uncommitted would clobber live work. Sequence the cleanup once the
parallel edits are committed/parked, then run the full pre-PR gate.
