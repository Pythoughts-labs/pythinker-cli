# TUI thinking Markdown and activity motion design

**Status:** Approved by the user on 2026-07-11

## Problem

When `show_thinking_stream` is enabled, the live thinking preview displays model Markdown as
plain italic text. Formatting markers such as `**...**` and HTML comments such as `<!-- -->`
therefore leak into the visible TUI instead of rendering cleanly.

The subagent activity tree also applies the coral shimmer to individual running tool rows. This
makes one row appear specially selected or more active than its siblings. The intended motion
language has one source of truth: only the bottom verb-spinner/status line shimmers while work is
active.

## Goals

- Render visible thinking-stream Markdown through the existing trusted TUI Markdown boundary.
- Hide Markdown HTML comments instead of exposing their source syntax.
- Keep activity-tree rows stable and readable in every lifecycle state.
- Apply the coral shimmer only to the single bottom verb-spinner/status line.
- Preserve reduced-motion, no-animation, no-color, truncation, and streaming behavior.

## Non-goals

- Changing whether reasoning is emitted, persisted, or enabled by default.
- Hiding the thinking stream or changing its six-line preview limit.
- Animating every running, queued, completed, or failed activity row.
- Changing subagent scheduling, execution state, concurrency, or event semantics.
- Reworking the general Markdown renderer or the composing-response preview.

## Design

### Thinking preview

`_ContentBlock._compose_thinking_stream` will continue to build the bounded live preview from the
existing streaming normalization and wrapping path. At the display boundary, it will pass that
preview to the same agent-body Markdown renderer used for committed assistant content instead of
constructing a plain `Text` object.

The preview remains nested under the existing thinking bullet and spinner. Rendering is limited to
the already bounded preview, so the change does not parse the complete accumulated reasoning on
every frame. The shared Markdown renderer remains responsible for ANSI sanitization and Markdown
semantics, including suppressing HTML comments.

Incomplete streaming Markdown must fail safely as readable text; it must not raise out of the Live
render loop. The final committed reasoning path remains unchanged because it already renders through
the agent-body boundary.

### Activity motion

`render_activity_tree` will render every row detail with the stable muted activity style, regardless
of whether the row is waiting, running, completed, failed, denied, or interrupted. Existing state
markers and their running pulse remain unchanged because they convey lifecycle state without moving
the verb text.

The bottom working indicator remains the only verb shimmer. Its current `activity_status_line` /
`_todo_activity_line` paths continue to use the coral shimmer palette, terminal capability checks,
and reduced-motion fallback. No second animation implementation is introduced.

## Failure behavior

- Empty thinking previews continue to show only the thinking spinner.
- Malformed or incomplete Markdown remains visible and cannot crash the TUI.
- ANSI control sequences remain sanitized at the established render boundary.
- Reduced-motion and static-output modes keep the verb label stable.
- Completed, failed, denied, and interrupted activity rows never gain motion.

## Test design

Tests will be written before production changes and observed failing for the expected reason.

- A thinking-stream preview containing `**bold**` renders the text without literal `**` markers.
- A thinking-stream preview containing `<!-- hidden -->` does not expose the comment or delimiters.
- Malformed/incomplete Markdown in the preview remains renderable without an exception.
- Running activity-tree detail uses one stable muted style at different timestamps and does not use
  shimmer palette spans.
- The bottom verb-spinner retains coral shimmer spans while motion is enabled.
- Reduced-motion behavior remains static and existing lifecycle markers remain correct.

Focused verification will run the thinking-stream, activity-tree, and shell-motion test modules,
followed by `make check-pythinker-code` and `make test-pythinker-code` before completion.

## Scope and rollback

The implementation should touch only the thinking preview render boundary, the activity-tree detail
style, their focused tests, task documentation, and the required Unreleased changelog entry for
shipped-code changes. Rollback is a direct revert of those small rendering changes; no configuration
or persisted-data migration is involved.
