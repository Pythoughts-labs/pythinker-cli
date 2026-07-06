# Full Pi TUI Renderer Port Design

## Goal
Port the core `@earendil-works/pi-tui` diff-renderer model from `blackbox/pi-main` into Pythinker’s agent TUI so streaming output, tool cards, status, and the input card are rendered as one stable, always-mounted terminal scene.

## Source behavior to preserve
- One root TUI tree owns the full visible terminal scene.
- Streaming assistant text updates an existing message/card instead of clearing and repainting scrollback.
- Tool execution cards stay mounted and mutate pending/running/done state in place.
- The prompt/input card stays visible at the bottom during agent runs.
- Rendering is diffed and coalesced to avoid terminal jump, flicker, and stale prompt fossils.

## Scope
Implement a Python-native renderer layer in Pythinker. Do not add a Node runtime or vendor the TypeScript package. Port the useful concepts and small algorithms only:

- `Component` protocol with `render(width) -> list[str]` and `invalidate()`.
- `Container`, `Text`, `Spacer`, and `Box` primitives.
- A `DiffRenderer` that tracks previous rendered lines and writes only changed terminal regions.
- A scheduler that coalesces render requests during token streaming.
- Shell integration path for agent-running mode first.

## Non-goals
- No wholesale visual redesign.
- No new third-party dependency.
- No full markdown renderer rewrite unless an existing Pythinker markdown path cannot support stable streaming.
- No replacement of prompt-toolkit input editing in the first pass.
- No unrelated focus-mode/sidebar changes.

## Architecture
Add a small Python renderer package under `src/pythinker_code/ui/shell/tui/`:

- `components.py`: component protocol and primitives.
- `diff.py`: line diff planning and terminal write helpers.
- `scene.py`: root scene composition for assistant stream, tool cards, status line, and prompt card.
- `scheduler.py`: render coalescing and invalidate/request-render boundary.

Existing prompt/session code will build a scene from current live-view state. The input card remains rendered by Pythinker’s existing prompt-card helpers, but it becomes a stable component in the scene instead of a surface that can be hidden during streaming.

## Data flow
1. Wire events update `_PromptLiveView` state as they do today.
2. The live view invalidates the TUI scene instead of forcing scrollback handoffs for every streaming update.
3. The scene renders to lines.
4. The diff renderer compares the new lines with the previous frame and writes only changed spans.
5. Final turn completion flushes stable assistant/tool content into scrollback exactly once.

## Failure handling
- If diff rendering fails, fall back to the existing safe full redraw path for that frame and log debug context without secrets.
- If terminal size is unknown, render at the current prompt-toolkit width.
- On resize, invalidate all cached component output and force one full-frame redraw.
- On interrupt/cancel, dispose pending timers/spinners and render the final stopped state before returning input control.

## Testing
- Unit tests for diff planning: insert, delete, modify, shrink, grow, empty frames, and width changes.
- Component tests for `Text`, `Spacer`, `Box`, and nested `Container` output.
- Regression tests that the prompt card remains visible during streaming, tool execution, scrollback handoff, and turn completion.
- PTY/e2e smoke test for no duplicated prompt marker and no missing input card during a running agent frame.

## Rollout
1. Land renderer primitives and tests without wiring them into the running shell.
2. Add scene composition behind an internal feature flag/default-off config.
3. Wire agent-running frames to the scene.
4. Flip default after focused prompt-layout and PTY tests pass.
