# Streaming render bug — root-cause report

**Date:** 2026-06-16 · **Branch:** `feat/tui-streaming-pr`
**Status:** Root cause PROVEN (code + reproduction). Fix plan pending blackbox-design synthesis.

## Symptom

During active streaming of an assistant review message, after `Findings:` the live preview
shows raw partial structured content (`Composing…`, then `{`, `"title": …`, `"severity": "low"`,
`},`, `"body": …`, plus `… output clipped to fit terminal`). After the message finalizes, the same
content renders as a clean Rich panel (`LSP module review`, badges `3 low`/`1 info`, grouped
`Low`/`Info` sections).

## Root cause (one sentence)

The assistant streams its findings as a fenced ` ```report ` JSON block; the **live preview
renders the *uncommitted pending tail* of the raw text as plain text with no structure-awareness**,
and the markdown commit-boundary logic *deliberately* keeps the still-open ` ```report ` fence in
that pending buffer — so the entire partial findings JSON is shown verbatim until the fence closes,
at which point a **different** renderer (`render_agent_body`) parses the now-complete block into the
clean panel. This is a **stream-lifecycle / renderer-mismatch bug, not a content-generation bug.**

## The two code paths (different renderers — confirmed)

| Phase | Entry | Renderer | What it shows |
|---|---|---|---|
| **Active preview** | `_ContentBlock._compose_composing` `_blocks.py:651` | `_build_preview` `:730` → `_render_preview_text` `:670` | `_pending_text()` = `raw_text[_committed_len:_revealed_len]` as **plain `Text`** (only `sanitize_ansi` + space-table repair via `_normalize_streaming_preview_text` `:165`). **No markdown, no ` ```report ` parsing, no suppression.** |
| **Finalize → scrollback** | `_ContentBlock.promote_to_scrollback` `:509` (via `_live_view.flush_content` `:1296`) | `render_agent_body` `components/report.py:503` → `parse_report_block` `:434` → `render_report` `:394` | Extracts top-level ` ```report ` fences, parses JSON, renders severity-grouped **Rich `Panel`**. |

**Why the JSON sits in `pending`:** `_flush_committed` `:577` commits only complete markdown blocks
via `_find_committed_boundary` `:315` → `markdown_commit_boundary` (`markdown/streaming.py:66`). An
**open fence** (no closing ` ``` `) is never a committable block, so everything from ` ```report `
onward stays uncommitted and is routed to the raw preview.

**Why scrollback is clean (preview is NOT promoted):** `flush_content` `:1311` calls
`promote_to_scrollback()`, which **re-renders from `raw_text`** through `render_agent_body` and emits
once; the transient preview renderable is discarded. So the raw text never reaches scrollback —
the bug is **purely in the transient preview display.**

## Reproduction (decisive — generates BOTH screenshots from ONE input)

`/tmp/repro_stream_leak.py` (temp, removable). Streams a prose + ` ```report ` message into a real
`_ContentBlock`:

- **Mid-stream `compose()`** → preview contains raw `['"severity"', '"title"', '"location"',
  '"body"', '},', '{']`. Visual output matches screenshot 1 (`● Composing…`, raw ` ```report ` JSON,
  trailing caret).
- `markdown_commit_boundary(MID) = 59` → commits only `"…Findings:\n"`; pending starts at
  `\n```report\n{"title": …`.
- **After stream completes** → `promote_to_scrollback()` renders the clean
  `╭─ LSP module review ─╮` panel with `● 3 low ● 1 info`, grouped `Low`/`Info`. Matches screenshot 2.
- `has_report_block(FULL) = True`; final render contains a Rich `Panel = True`.
- **Paced variant** (`paced=True`, the real shell path) **also leaks** the same tokens — pacing
  changes reveal speed, not the structural leak.

## Hypothesis matrix

| # | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| H-ROOT | Preview renders uncommitted pending tail raw; open ` ```report ` fence held in pending; final parses it into a panel | **CONFIRMED** | `_blocks.py:651/659/670/577/315`, `report.py:503`, repro |
| A1 | Content/model bug (model emits broken output) | REJECTED | `has_report_block(FULL)=True`; final render clean; valid JSON |
| A2 | Preview receives parsed tool output / structured objects | REJECTED | preview input is `raw_text` accumulated via `append()`; plain str |
| A3 | Raw preview is promoted into scrollback (double render) | REJECTED | `flush_content` re-renders from `raw_text`; transient preview discarded (`:1311`) |
| A4 | It's a subagent tool-output (`_ToolCallBlock`) leak | REJECTED (as the screenshot) | `"Composing"` label exclusive to `_ContentBlock._compose_spinner` `:697`; `tool_renderers/agent.py:_render_call` shows spinner + findings *table*, never raw streamed text |
| A5 | `looks_like_report_update` early-return suppresses commit | REJECTED | `report_update.py:121` matches only `"report update complete"`, not ` ```report ` |
| A6 | `… output clipped to fit terminal` is an independent bug | RECLASSIFIED → symptom | `_COMPOSING_PREVIEW_LINES=12` tail-limit + `prompt.py:_fit_formatted_text_to_rows:760` row-crop; triggered *because* the raw pending block is many lines |
| A7 | Redraw frequency / flicker is the cause | REJECTED | static single `compose()` leaks; no redraw involved |

### Primary-goal answers
1. **Active preview renderer:** `_ContentBlock._compose_composing` → `_build_preview` → `_render_preview_text` (plain `Text`). Interactive shell wraps it in `_PromptLiveView` (prompt_toolkit), cropped by `_fit_formatted_text_to_rows`.
2. **Finalized renderer:** `promote_to_scrollback` → `render_agent_body` → `render_report` (Rich `Panel`).
3. **Different paths?** Yes — plain-text tail vs structured markdown/report parse.
4. **What the preview receives:** raw partially-accumulated assistant **markdown text** (the uncommitted tail), not parsed/structured objects.
5. **Where raw JSON enters UI:** `_compose_composing` `:659-664` (`preview = _build_preview(pending)`).
6. **Preview rendering incomplete structured data?** It blindly tails raw text — no structure awareness, no suppression. That is the defect.
7. **Finalize replaces or appends?** Replaces — transient preview dropped, clean block emitted once.
8. **Raw preview promoted to scrollback?** No — only `promote_to_scrollback()` (re-render from `raw_text`).
9. **Clipping cause:** preview tail-limit (`_COMPOSING_PREVIEW_LINES`) + prompt_toolkit row-fit crop; a *consequence* of the large raw pending block, not terminal-width wrapping per se.
10. **Category:** stream-lifecycle + renderer-mismatch. Content is correct.

## Known limitation (separate, smaller)
If a stream is **cancelled mid-fence**, the ` ```report ` never closes → `parse_report_block` returns
`None` → `render_agent_body` falls back to markdown → raw JSON lands in **scrollback** (not just
preview). Out of scope for the preview fix; note for the fix plan.

## Blackbox reference synthesis (study complete)

Neither reference is Python: **`pythinker-x` = codex-rs** (Rust/Ratatui), **`pythinker-src` = TS/React-Ink**.
Neither has a fenced-`report`-JSON panel, but both render the in-progress tail **through the real
markdown renderer** (not plain text like our `_render_preview_text`). codex-rs is the **decisively
better** design for *this* leak; it adds two things on top of a two-region model:

- **Newline-gated commit** (`pythinker-x/codex-rs/tui/src/markdown_stream.rs:87-96`): never render
  past the last `\n`; a partial line is never shown.
- **Fence-aware holdback** (`table_detect.rs:143-195` `FenceTracker` + `table_holdback.rs` +
  `controller.rs:373-401` `active_tail_budget_lines`): a structurally-unstable region (table, or
  anything inside an open fence) is kept in the **mutable tail** until it closes, then committed
  atomically. Structured *review findings* are a typed event formatted on completion — never
  streamed as text at all (`protocol.rs:3162-3190`, `review_format.rs:23-82`).
- TS ref (`Markdown.tsx:176-235` `StreamingMarkdown`): stable-prefix/unstable-suffix split at the
  last top-level block boundary, both through `<Markdown>`. Relies *implicitly* on `marked` lexing
  an unclosed fence as one token — no explicit suppression, no placeholder. Weaker.

**What our repo already has (≈ the two-region model):** commit boundary (`markdown_commit_boundary`),
committed scrollback (`_flush_committed`), transient tail (`_compose_composing`), atomic promotion
(`promote_to_scrollback` re-renders from `raw_text`). The **one missing piece vs codex-rs is the
fence-aware holdback** of the incomplete structured block — exactly our gap.

Note: simply "render the preview tail through markdown" (the other blackbox trait) does **not** fix
this leak — an incomplete ` ```report ` still renders as a raw code block, and a complete-but-
uncommitted one would flash a full panel mid-preview. The **holdback is the real fix.**

## Fix plan (FINAL)

**Adopt codex-rs's fence-aware holdback, scoped to the one structured block that transforms on
finalize (` ```report `).** Minimal, surgical, and the faithful port of the decisive blackbox idea.

1. **New helper** in `_blocks.py` — `_holdback_incomplete_report(text) -> str`: if the pending text
   contains a top-level ` ```report ` opener (line-anchored `^```report$`) with **no closing
   ` ``` ` after it**, truncate at the opener and append a stable placeholder line (e.g.
   `… formatting review findings`). Cheap regex scan — no markdown-it per tick (mirrors `FenceTracker`).
2. **Hook it** into the composing preview only: `_compose_composing` → before `_build_preview`
   (or as the first step inside `_build_preview`, guarded to composing). Leaves
   `_compose_thinking_stream` untouched.
3. **Scope guard (advisor #2):** match only ` ```report ` (and, if desired, report_update). Ordinary
   ` ```python `/` ```ts ` fences keep streaming line-by-line — do **not** suppress them.
4. **No change to commit/finalize:** open fence already isn't committed; closed fence already renders
   the panel via `render_agent_body`. Scrollback is already correct.
5. **Tests:** unit test asserting mid-stream `compose()` of a partial ` ```report ` shows the
   placeholder and **none** of `"severity"/"title"/{`; and that an ordinary ` ```python ` fence is
   **not** suppressed; plus the existing finalize-panel behavior is unchanged. Convert
   `/tmp/repro_stream_leak.py` into a focused regression test under `tests/`.

**Known limitation (separate, smaller):** stream cancelled mid-fence → `parse_report_block` returns
`None` → raw JSON reaches **scrollback**. Optional follow-up: on cancel/finalize, if an unterminated
` ```report ` fence is present, drop or close it before promotion. Out of scope for the preview fix.

**Optional larger polish (NOT bundled):** render the preview tail through `render_agent_body`/markdown
(the other blackbox trait). Bigger behavioral change, per-tick parse cost, and many pinned-preview
tests would move. Not required to fix this bug; defer unless explicitly wanted.

## Status — report-leak fix SHIPPED on this branch
- `_blocks.py`: added `_suppress_unclosed_report_fence_preview` + wired into `_normalize_streaming_preview_text` (preview-only).
- `tests/ui_and_conv/test_streaming_content_block.py`: `TestReportFenceSuppression` (9 tests).
- Gates: `make check-pythinker-code` green (ruff+format+pyright 0 errors); 699 stream/preview/report tests pass; static-requirements pass.

## Follow-up — streaming "glitch" trio share ONE architectural root (interactive TUI)

Three reported symptoms, one cause:
1. raw ` ```report ` JSON leaks in the live preview — **FIXED** above (same transient preview, raw text).
2. "composing stalls then dumps the rest" — sliver of prose, then the rest pops (Image #4: `● Let▍` + a `Flowing…` subagent).
3. "at the end the full report flickers onto the screen" — long message (Image #1: `Fluttering… 11m, ↓24k tokens`) snaps in at once.

**Unified root cause (code-confirmed):** In the interactive path (`_PromptLiveView`), a whole assistant
text run is held in the **transient prompt preamble** the entire time it streams. Committed markdown
blocks accumulate *in-block* (`_ContentBlock._committed_renderables`, appended by `_flush_committed`
`_blocks.py:577`) and are re-rendered every frame by `_compose_composing` — they reach **real
scrollback only at `flush_content`** (`_live_view.py:1296`), which fires solely at turn boundaries /
the next tool call (`append_tool_call:1400`) / think↔text transitions, **never per content-part**.
Consequences:
- The pending tail is paced (`reveal_tick`, ~½ backlog per 40 ms `STREAM_FPS=25`), but `flush_content`
  calls `reveal_all()` — an **instant dump** — so a fast model that calls a tool before the ~400 ms
  drain finishes pops the tail in (symptom 2).
- A long message's entire committed body is transient (cropped by `_fit_formatted_text_to_rows` →
  "output clipped to fit terminal") and is **printed to scrollback all at once at finalize** → flicker
  (symptom 3).

`smooth_streaming` defaults **True** (`config.py:996`); turning it off only removes the paced buffer,
not the transient-until-finalize architecture, so it would not fix the flicker.

**User-chosen direction:** "Fix the drain, keep smooth."

**Fix = adopt the blackbox codex-rs incremental-commit model** (stable lines → scrollback as they
complete; only the mutable tail stays transient + paced — `pythinker-x` `streaming.rs:326-349`,
`controller.rs`). Staged, test-first:

- **Stage 1 — incremental scrollback commit (fixes flicker #3).** When `_flush_committed` produces a
  committed block mid-stream, emit it to real scrollback immediately (interactive already prints above
  the prompt at finalize) and stop re-rendering it in the preamble. Preamble then holds only spinner +
  small pending tail. Finalize commits just the remaining tail.
- **Stage 2 — drain before final flush (fixes dump #2).** Before `flush_content` commits the last tail
  on a tool call, let the paced reveal finish (bounded await of the drain in the dispatch loop) instead
  of `reveal_all()` popping it.

**Risk / scope:** delicate, heavily-tested path; must keep `_LiveView` (Rich Live) and `_PromptLiveView`
(prompt_toolkit) in parity, guard against double-emission, preserve block spacing, and update the many
tests that assert committed blocks appear in `compose()` output. Larger than the report-leak patch —
proceed as its own staged change.
