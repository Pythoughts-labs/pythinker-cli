# Changelog

## Unreleased

## 0.8.0 (2026-05-21)

First-class review-first workflows: code review, security scanning, root-cause debugging, PR artifact helpers, and Reviewflow stateful review/fix workflows.

- Added the `pythinker-review` workspace package and wired it into the root `pythinker-code` package via the pinned `pythinker-review==0.1.0` dependency.
- Added `pythinker review`, `pythinker secscan`, `pythinker security-scan`, and `pythinker debug` command surfaces, backed by standalone `pythinker-review`, `pythinker-secscan`, `pythinker-security-scan`, and `pythinker-debug` console scripts.
- Added read-only PR artifact/helper commands: `describe`, `improve`/`suggest`, `ask`, `ask-line`, `labels`, `changelog`, `docs`, `compliance`, `help-docs`, `similar-issues`, `tools`, and `config`.
- Added Reviewflow workflow commands and state models for `init`, `map`, `review`, `report`, `next`, `show --finding`, `triage`, `revalidate`, `fix`, `open-pr`, `ci`, and `doctor`.
- Added the `code-reviewer`, `security-reviewer`, and `debugger` subagent roles for interactive Pythinker sessions.
- Hardened review output validation so unsafe paths, stale line ranges, mismatched evidence snippets, malformed model JSON, worker failures, and timeouts fail closed unless `--allow-partial` is explicitly used.
- Refreshed README release notes with the 0.8.0 review/security/debug feature set and the pinned `pythinker-code==0.8.0` upgrade snippet required by the release gate.

Upgrade with `pythinker update` or `pip install --upgrade pythinker-code==0.8.0`.

## 2.6.0 (2026-05-13)

Packaging fix: pin `pythinker-core[contrib]==1.1.0` so the Kimi K2.x / DeepSeek strict-interleaved reasoning-replay fix reaches PyPI installs.

### Why 2.6.0 lands the same day as 2.5.0

The runtime fix for the strict-interleaved `thinking is enabled but reasoning_content is missing in assistant tool call message at index N` rejection landed in the `pythinker-core` source tree on 2026-05-11 (released in `pythinker-code` 2.4.0 source), but **the published `pythinker-core==1.0.0` on PyPI predates that change** (uploaded 2026-05-07). `pythinker-code` 2.4.0 and 2.5.0 both pinned `pythinker-core[contrib]==1.0.0`, so PyPI users on Kimi K2.5 / K2.6 / DeepSeek through OpenCode Go (and elsewhere) kept hitting the bug even on the latest CLI. Reported in [#37](https://github.com/Pythoughts-labs/pythinker-code/issues/37).

### The fix

- **`pythinker-core` bumped to 1.1.0** (published independently on PyPI) — carries the strict-interleaved replay logic that emits `reasoning_content` on every assistant turn for `kimi-k2*` / `deepseek*` models, falling back to `extract_text()` then `"[reasoning unavailable]"` when no `ThinkPart` was captured.
- **Root pin updated to `pythinker-core[contrib]==1.1.0`** in `pyproject.toml`. New installs of `pip install --upgrade pythinker-code==2.6.0` (and `uv tool install`/`upgrade`) get the fix automatically.
- See `packages/pythinker-core/CHANGELOG.md` for the full pythinker-core 1.1.0 entry.

### No app-level behavior changes vs 2.5.0

Everything that landed in 2.5.0 is still there. The 2.6.0 diff is exclusively the version bump and the upstream-dep pin; existing 2.5.0 users **must** upgrade to 2.6.0 if they use Kimi K2.x, DeepSeek, or any other strict-interleaved-thinking model through an OpenAI-compatible provider.

Upgrade with `pythinker update` or `pip install --upgrade pythinker-code==2.6.0`.

## 2.5.0 (2026-05-13)

bk_box_main coding-agent runtime port, Windows self-upgrade fix, FetchURL SSRF hardening, and a broad reliability/security pass.

### Subagent runtime & permissions

- Runtime-enforced permission profiles for every built-in role: **read-only**, **plan**, **ask**, **implement**, **review**, **verify**. Profiles are snapshot per LLM step in the new `src/pythinker_code/soul/permission.py` so a mid-step model switch can't escalate. Plan mode now **hard-denies** non-plan writes and dangerous shell mutations instead of relying on prompt-deny.
- New plan-handoff workflow in `src/pythinker_code/tools/plan/handoff.py` with dynamic injection through `soul/dynamic_injections/plan_mode.py`. Smooth handoff from `plan` → `implement` without re-priming the context.
- New smart-search grep variant; new subagent metadata plumbing (`subagents/models.py`, `subagents/store.py`, `subagents/builder.py`, `subagents/runner.py`).

### Background tasks

- Recovery distinguishes **`recoverable`** (resumable via a stored `agent_id`) from **`lost`** (worker is gone with no resume target). Agent instances are parked as `idle` rather than failed when the underlying task is recoverable.
- Guards against overwriting terminal task states; subagent races on instance transitions closed.
- `pythinker-host`: subprocess teardown now kills the **entire child process tree** and creates a new session group, so background workers can no longer survive their parent on Linux/macOS.

### FetchURL — SSRF + resource-exhaustion hardening

- `pythinker_code.tools.web.fetch._validate_fetch_url` blocks **private, loopback, link-local, multicast, and reserved** IPv4/IPv6 ranges; rejects non-`http`/`https` schemes and host-less URLs up front.
- Responses are streamed with a hard **5 MB** ceiling (`_read_limited`) honoring `Content-Length`. Both the direct path and the configured fetch-service path enforce the same caps.

### Web / vis surface

- Upload limits, open-in path escaping, and vis auth all hardened (`src/pythinker_code/web/`, `src/pythinker_code/vis/`, `vis/src/lib/api.ts`).

### Plugin

- Plugin definitions no longer persist host credentials. Plugin **name validation** tightened to reject path-traversal and shell-meta characters.

### Telemetry & observability

- OTel `service.name` normalized to a stable value, decoupled from the configured display name, so SigNoz dashboards keep working across rebrands.
- Sentry filters drop test-process noise and benign shutdown errors; `pythinker_code/telemetry/config.py` and `pythinker_code/telemetry/crash.py` updated accordingly.
- New `tests/telemetry/test_otel_resource.py` asserts the resource identity used by the dashboards.

### Windows

- `pythinker update` on Windows now spawns the upgrade in a **detached console** and exits the parent process before `uv tool upgrade` runs, releasing the lock on the running `pythinker.exe`. Fixes the `os error 32: The process cannot access the file because it is being used by another process` error that blocked self-upgrade.
- New CI matrix entry on **`windows-2025-vs2026`** (experimental, non-blocking) for the pythinker-host and pythinker-cli build, validating Visual Studio 2026 / MSVC v144 forward-compat before GitHub eventually deprecates `windows-2022`.

### Feedback

- New `feedback` config block: `endpoint_url`, `api_key`, `custom_headers`. The `/feedback` slash command now routes user submissions to a user-configured HTTP endpoint instead of being a no-op.

### UI

- Pythinker version is shown on the welcome screen.

### CI

- Pre-push hooks mirror CI's `check` target (`ruff format --check`, `ruff check`, `pyright`) so local pushes catch the same regressions CI does.
- README + CHANGELOG release-validate gate hardened; the GitHub Release publish step is now resilient to transient upstream failures.
- Spell-check vocabulary fix in `soul/permission.py` for an internal error string the typos crate flagged; experimental `windows-2025-vs2026` build no longer collides with `windows-2022` on the shared `pythinker-x86_64-pc-windows-msvc` artifact name.

### Compatibility

- `pythinker_core.contrib.chat_provider.anthropic`: handle the six new tool-result block types added by anthropic SDK 0.101 (`web_fetch_tool_result`, `code_execution_tool_result`, `bash_code_execution_tool_result`, `text_editor_code_execution_tool_result`, `tool_search_tool_result`, `container_upload`). pyright is exhaustive again.

Upgrade with `pythinker update` or `pip install --upgrade pythinker-code==2.5.0`.

## 2.4.0 (2026-05-11)

Subagent roles overhaul, Kimi K2 provider support, and a ripgrep-free Grep fallback.

- New built-in subagents under `src/pythinker_code/agents/default/`:
  - `implementer.yaml` — scoped code changes with minimum surrounding edits and a quick verification pass.
  - `review.yaml` — read-only code review with severity-scored findings (BLOCKER / MAJOR / MINOR / NIT).
  - `verifier.yaml` — read-only validation runner that reports `PASS` / `FAIL` / `FLAKY` without applying fixes.
- `coder.yaml`, `explore.yaml`, and `plan.yaml` now emit a standard `### SUMMARY / EVIDENCE / CHANGES / RISKS / BLOCKERS` response contract so the parent agent can consume subagent output without re-parsing prose.
- `agent.yaml` registers the three new roles; `tools/agent/description.md` documents the Scout → Plan → Implement → Review → Verify workflow and the parallel review/verification pattern.
- `agents/default/system.md`: adds decomposition guidance (preview → todo list → parallel chunks), enforces post-tool-call verification before acting on results, and tells the agent to cross-check at least one load-bearing subagent finding before editing from it.
- Kimi K2.5 / K2.6 and other strict interleaved-thinking providers:
  - `packages/pythinker-core/.../chat_provider/pythinker.py`: always emit `reasoning_content` on assistant tool-call replays so the strict "thinking is enabled but reasoning_content is missing in assistant tool call message at index N" error no longer trips multi-step tool flows.
  - `packages/pythinker-core/.../contrib/chat_provider/openai_legacy.py`: replay reasoning metadata on every assistant turn for `kimi-k2*` / `deepseek*` models (falls back to the assistant text or `"[reasoning unavailable]"` when reasoning content was not retained).
  - `src/pythinker_code/llm.py`: route Kimi K2 thinking through the provider-specific `extra_body={"thinking": {"type": "enabled"|"disabled"}}` body field instead of OpenAI's `reasoning_effort` (which Kimi ignores), and persist `LLM.thinking` across `clone_llm_with_model_alias` so model switches preserve the user's thinking choice.
- `tools/file/grep_local.py`:
  - Pure-Python `rg`-free fallback (`_python_grep`) honoring `pattern`, `path`, `glob`, `type` (bash / c / cpp / go / java / js / json / md / py / rust / sh / toml / ts / txt / yaml / zsh), `ignore_case`, `multiline`, `context` / `before_context` / `after_context`, `line_number`, `output_mode` (`content` / `files_with_matches` / `count_matches`), `offset`, `head_limit`, and the standard sensitive-file redaction. `.gitignore` / `.ignore` and the VCS metadata directories (`.git`, `.svn`, `.hg`, `.bzr`, `.jj`, `.sl`) are respected unless `include_ignored=true`.
  - `_find_existing_rg` now honors `PYTHINKER_RG_PATH` and additionally probes `/usr/bin`, `/usr/local/bin`, `~/.cargo/bin`, `~/.local/bin`, and `~/.pi/agent/bin` before falling through to download.
  - Downloader retries against the upstream GitHub releases mirror (`https://github.com/BurntSushi/ripgrep/releases/download/<version>/...`) when the CDN mirror is unreachable, and the failure path now degrades into the Python fallback instead of raising.
- `.gitignore`: ignore generated code-graph outputs and the local reference-scan scratch area.
- `AGENTS.md` rewritten to reflect the new subagent roster and workflow.

## 2.3.0 (2026-05-09)

Telemetry & observability audit.

- New `pythinker_code/telemetry/errors.py` helper `report_handled_error(exc, *, site, tool=None, **attrs)` forwards caught-and-rendered exceptions to both Sentry/Bugsink and the OTel `error` event stream. Both forwarding paths are `contextlib.suppress`-wrapped so monitoring can never break the host program.
- 38 silent-catch sites instrumented across `tools/`, `auth/`, `soul/`, `acp/`, `hooks/`, `subagents/`. Tool failures, OAuth errors, MCP server hiccups, hook callback failures, and subagent crashes now reach Bugsink and SigNoz.
- `pythinker_code/telemetry/otel.py`: TracerProvider now uses `ParentBased(TraceIdRatioBased(rate))` driven by `PYTHINKER_OTEL_TRACE_SAMPLE_RATE` (default 1.0).
- New `pythinker.mcp.call` span around every MCPTool RPC.
- New `/report-error` slash command (aliases `/report`, `/report-error`).
- `docs/en/reference/telemetry.md` documents the full telemetry contract.
- `chore(test)`: updated google-genai snapshot for pydantic 2.13.4 + google-genai 2.0.0.


## 2.2.1 (2026-05-09)

CI hardening: macOS binary build is now optional-codesign.

- `.github/workflows/release-pythinker-cli.yml`: detect whether `APPLE_CERTIFICATE_P12` and `APPLE_NOTARIZATION_KEY_P8` repo secrets are configured. When they aren't, skip the keychain setup, codesign, and notarization steps and ship an ad-hoc-signed PyInstaller binary instead of failing the whole release. The 2.2.0 release run failed because those secrets were empty in CI; this makes the release matrix all-green even without an Apple Developer cert.
- `.github/workflows/release-pythinker-cli.yml`: add `skip-existing: true` to `pypa/gh-action-pypi-publish` so re-runs of the release workflow against an already-published version are no-ops instead of HTTP 400 errors.
- macOS-arm64 binary downloads from the GitHub Release page will now show a Gatekeeper warning on first launch when the secrets aren't configured. Users can clear it with `xattr -d com.apple.quarantine ./pythinker`. PyPI install (`pip install pythinker-code`) is unaffected.

## 2.2.0 (2026-05-09)

Installer UX: animated logo + Windows PATH automation.

- `scripts/install.sh`: Tetris-style logo animation. Pieces (walls, bars, eyes, ears, antenna) fall from above the canvas one at a time and settle into the robot head before install proceeds. Static logo remains the fallback for non-TTY / `NO_COLOR` / `TERM=dumb` / `CI=1` / `PYTHINKER_NO_ANIMATION=1`. Tunables: `PYTHINKER_LOGO_FRAME_DELAY` and `PYTHINKER_LOGO_STAGGER_DELAY` (seconds).
- `scripts/install.ps1`: same animation, PowerShell port. Tunables: `PYTHINKER_LOGO_FRAME_DELAY_MS` and `PYTHINKER_LOGO_STAGGER_DELAY_MS` (milliseconds).
- `scripts/install.ps1`: idempotently append `$USERPROFILE\.local\bin` to the User PATH via `[Environment]::SetEnvironmentVariable` and broadcast `WM_SETTINGCHANGE` so future shells (and Explorer-spawned children) pick up the new PATH automatically. No-op when the directory is already present.
- `scripts/install.ps1`: force `[Console]::OutputEncoding = UTF8` so the box-drawing characters render correctly on Windows hosts.

## 2.1.2 (2026-05-09)

Windows installer fix.

- `scripts/install.ps1`: dot-source Astral's `uv` bootstrap inside `& { ... }` instead of spawning `powershell -File`, so `$env:PATH` updates land in the running process.
- `scripts/install.ps1`: refresh PATH after `uv tool install` and verify `pythinker` resolves before printing success; otherwise emit the absolute shim path.
- README: invoke the downloaded `install.ps1` with `& $installer` (current shell) instead of a `powershell -NoProfile -File $installer` subprocess. `uv`, `uvx`, and `pythinker` are now usable in the calling shell as soon as the installer exits.
- `tests/test_installation_docs.py`: lock in current-session invocation in the README and a dot-sourced `uv` bootstrap in `install.ps1`.

## 2.1.1 (2026-05-08)

Documentation refresh.

- README: image and key file links converted to absolute GitHub URLs so the project page on PyPI renders the logo, demo GIFs, and architecture diagram correctly.
- README: added a "What's New in 2.1.0" section summarising the TUI CLI and slash-command enhancements.
- README: corrected the License section to Apache-2.0 (matches `pyproject.toml` and the `LICENSE` badge).

## 2.1.0 (2026-05-08)

TUI CLI design and slash command enhancements.

- New selectors package — interactive `/theme`, `/thinking`, `/model`, `/login`, `/settings`, `/extension`, and `/show-images` panels replace inline numeric/text prompts.
- New `/thinking` slash command for adjusting reasoning effort at runtime.
- `SettingsList` component + `/settings` selector for live `Config` editing (theme, default model, TUI style, default thinking, telemetry).
- Selector framework: `SelectorHeader` sentinel and per-row `on_change` callback.
- Card-style TUI polish: bordered shell card, footer/toolbar, tool renderers (read/write/edit/grep/find/bash/agent) and a diff component; subagent tool cards get a running-dots spinner.
- Pythinker-branded slash menu, input row, and tool execution components.
- Prompt template discovery simplified to `.pythinker/prompts` at project and user scope (legacy directory lookup removed).
- TUI style flag now accepts only `card` (default) or `pythinker`; the legacy alias has been dropped.

- Update notifier: standalone "Update Available" banner shown above the welcome panel when a newer release is on PyPI; suppressed by `PYTHINKER_CLI_NO_AUTO_UPDATE=1`.
- New `pythinker update` subcommand (`--check` for a non-installing check) that auto-detects the install method (`uv tool`, `pipx`, or `pip`) and runs the matching upgrade command.
- Image paste placeholder now renders as `[Image #N]` in the input/buffer (mirroring `[Pasted text #N +K lines]`); history serialization keeps the canonical `[image:<id>,WxH]` form so attachments still resolve cross-session.
- Lower default paste-placeholderize thresholds to 200 chars / 5 lines (was 1000 / 15) so medium-length pastes collapse into `[Pasted text #N]` instead of overflowing the compact input window. Override via `PYTHINKER_CLI_PASTE_CHAR_THRESHOLD` and `PYTHINKER_CLI_PASTE_LINE_THRESHOLD`.

## 1.0.0 (2026-05-06)

Initial release of Pythinker CLI.

- Interactive terminal AI coding agent with shell command mode (Ctrl-X)
- ACP server (`pythinker acp`) for IDE integrations such as Zed and JetBrains
- MCP tool support: `pythinker mcp` sub-commands and ad-hoc `--mcp-config-file`
- Multi-provider auth: OpenAI, OpenAI Codex, Anthropic, OpenRouter, DeepSeek, MiniMax, Opencode-Go
- Skills, plugins, and slash commands across project / user / built-in scopes
- Session persistence with subagents and approval workflows
- Print mode and wire mode for non-interactive and programmatic use
- Plan mode for read-only research and design before code changes
- YOLO mode (`--yolo` / `/yolo`) to dangerously skip permission approvals while keeping the user reachable via `AskUserQuestion`
- Auto mode (`--auto` / `/auto`) for unattended runs: auto-approves tool calls and auto-dismisses `AskUserQuestion` so the agent finishes end-to-end without a user
