# Clean-code-guard follow-ups — feat/agent-behaviour-tweaks

Deferred findings from the deep scan of `git diff 1ad0339c127c...HEAD`.
Do not land in the current PR; track here for follow-up.

## Status

- [x] Type the new `report`-block parser (`src/pythinker_code/ui/shell/tool_renderers/agent.py:114-124`).
      Resolved: replaced ad-hoc `Any` with `TypedDict` (`_ReportFinding`, `_ReportBlock`) + `cast`.
      `uv run pyright src/pythinker_code/ui/shell/tool_renderers/agent.py` → 0 errors.
- [x] Format the two unformatted test files
      (`tests/ui_and_conv/test_review_findings_parser.py`,
      `tests/ui_and_conv/test_md_repair_characterization.py`).
      Resolved: `uv run ruff format` → both clean.

## Deferred — medium severity

### 1. `disconnect_mcp_server` swallows close errors and overwrites `info.error` (C03 + C04)

- Location: `src/pythinker_code/soul/toolset.py:1378-1385`
- Mechanism: `except Exception` catches `asyncio.TimeoutError` (subclass of `Exception` since
  Python 3.11) from `asyncio.wait_for(info.client.close(), ...)` and logs at DEBUG, then
  unconditionally sets `info.status = "failed"` and `info.error = "disconnected"` — clobbering
  any pre-existing connect/refresh error.
- Impact: a user disconnecting a hung MCP server sees `error="disconnected"` with no
  actionable cause. `mcp_status_snapshot` cannot distinguish a clean user-initiated disconnect
  from a close timeout.
- Suggested fix: catch `(TimeoutError, ClientError)` (or the specific client class used here)
  explicitly, log at WARNING, and only overwrite `info.error` when no prior error is recorded.
- Acceptance: a new test feeds a hanging `info.client.close()` and asserts
  `mcp_status_snapshot(...)["error"]` reflects the timeout, not the literal string
  `"disconnected"`.

### 2. `reconnect_mcp_server` re-raises the raw exception, not the classified error string

- Location: `src/pythinker_code/soul/toolset.py:1418-1426`
- Mechanism: `_connect_mcp_server` sets `info.error = _classify_mcp_connect_error(e, ...)`,
  but the call site raises `MCPRuntimeError(f"Failed to reconnect MCP server '{server_name}':
  {error}")` where `error` is the original `Exception` object, not `info.error`.
- Impact: the user sees an unwrapped traceback message instead of the structured classification
  that `mcp_status_snapshot` already shows.
- Suggested fix: raise with `MCPRuntimeError(info.error or str(error))`.
- Acceptance: a new test monkey-patches `_connect_mcp_server` to raise, asserts the
  re-raised `MCPRuntimeError` message starts with the classified string.

### 3. `mcp_tool_runtime_key` normalizes server name — silent public contract change

- Location: `src/pythinker_code/utils/mcp_names.py:31-35` and
  `src/pythinker_code/soul/toolset.py:553, 1294, 1365`
- Mechanism: keys are now `mcp__<normalized-server>__<tool>` (e.g. `my server/v2` →
  `myserverv2`). Previous contract was `mcp__<raw>__<tool>`.
- Impact: agent yamls or hand-written tool references that hardcoded raw unsafe keys will
  silently fail to resolve. The `Runtime.mcp_tools` docstring (`src/pythinker_code/soul/agent.py:237-238`)
  still says raw `mcp__<server>__<tool>`.
- Suggested fix: update the `Runtime.mcp_tools` docstring to note normalization, and add a
  regression test that registers a server named e.g. `my server/v2` and asserts
  `runtime.mcp_tools` carries the normalized key.
- Acceptance: docstring + test pass; AGENTS.md "Preserve public compatibility" rule satisfied.

### 4. `read_media` description claims 100 MB max, but images are now capped at 20 MB

- Location: `src/pythinker_code/tools/file/read_media.md:10` and
  `src/pythinker_code/tools/file/read_media.py:69`
- Mechanism: `read_media.md` interpolates `MAX_MEDIA_MEGABYTES = 100` (from
  `utils/media_limits.py: max(MAX_IMAGE_BYTES=20MB, MAX_VIDEO_BYTES=100MB) // 1MB`).
  But images are capped at 20 MB (plus a 20 MP pixel limit in
  `tools/file/read_media.py:69`).
- Impact: a model reading the description will try a 50 MB image and get a runtime
  `ToolError(... exceeds the max 20 MB limit for image files)`.
- Suggested fix: document per-kind limits in `read_media.md`
  (`{MAX_IMAGE_MEGABYTES} MB for images / {MAX_MEDIA_MEGABYTES} MB for video`) or
  expose `MAX_IMAGE_MEGABYTES` separately.
- Acceptance: `make check-tools` (or `uv run pytest tests/tools/test_read_media.py`) passes
  and the description matches the runtime error string for the first oversized image read.

## Deferred — low severity

- `InvokeMcpPrompt` hides exception class in user-facing error
  (`src/pythinker_code/tools/mcp_resource/__init__.py:172-179`). Prepend
  `type(exc).__name__` to the message.
- `_publish_connected_mcp_tools` comment claims "configured order" but uses dict insertion
  order (`src/pythinker_code/soul/toolset.py:539-549`). Reword comment to
  "deterministic per session — dict insertion order".
- `_render_prompt_messages` does not branch on multimodal content
  (`src/pythinker_code/tools/mcp_resource/__init__.py:133-142`); text-only is the common case.

## Verification gates I ran

- `uv run pyright src/pythinker_code/ui/shell/tool_renderers/agent.py` → 0 errors (was 4).
- `uv run ruff format --check` on the two test files → clean.
- `uv run ruff check` on dirty files → clean.
- `uv run pytest tests/ui_and_conv/test_review_findings_parser.py tests/ui_and_conv/test_markdown_guards.py tests/ui_and_conv/test_md_repair_characterization.py` → 52/52 passed.
- `git diff 1ad0339c127c...HEAD -- pyproject.toml uv.lock` → empty (no new deps;
  AGENTS.md zero-new-bundled-deps satisfied).
- `git diff 1ad0339c127c...HEAD -- src/ | grep "^+.*except Exception"` → 5 new bare
  `except Exception`; the only load-bearing C03 tripwire is `disconnect_mcp_server`
  (item 1 above).

## Residual risk

- The `_RE_REPORT_BLOCK` regex (`.*?` lazy) is untested for nested-`report` blocks.
  LLM output rarely produces this; low real-world risk. Add a regression test if observed.
- Multi-server `mcp_status_snapshot` race when disconnect + reconnect overlap (concurrent
  path; needs a focused stress test).
- Whether any external user config or agent yaml hardcodes raw
  `mcp__<unsafe-server>__<tool>` keys (depends on user-side state, not in repo).
