# Repository Map

This page is the routing index for the Pythinker Code codebase: a factual, per-subsystem
map of where each capability lives, what its load-bearing entry points are, and which trust
boundaries pass through it. It exists so that an agent or contributor can locate the right
files before reading or editing, without scanning the whole tree.

It is a router, not a tutorial. For runtime behavior and concepts see
[Agent Architecture](./agent-architecture); for the always-on rules that govern changes see
the repository's root `AGENTS.md`. The root `AGENTS.md` keeps only a short repo map and points
here for detail. Paths are relative to the repository root unless noted.

The CLI is a uv workspace: the application lives under `src/pythinker_code/`, and reusable
layers are split into `packages/pythinker-core`, `packages/pythinker-host`,
`packages/pythinker-review`, and `sdks/pythinker-sdk`. The vendored reference repositories
under `blackbox/` are out of scope and are not part of this map.

## How AGENTS.md guidance loads

`AGENTS.md` files are merged from the project root down to the session working directory by
`load_agents_md` in `src/pythinker_code/soul/agent.py`, capped at 32 KiB and allocated
leaf-first. Subagents inherit the parent's already-merged guidance rather than re-resolving
from their own directory. A nested `AGENTS.md` therefore only loads when a session's working
directory is inside that subtree, which is why nested guides exist for directories people
actually `cd` into (for example `web/`, `dashboard/`, `tests_e2e/`) and not for every module.

## Trust boundaries at a glance

The security-relevant edges of the system, independent of any one subsystem:

- **Where input enters.** User input arrives at `PythinkerSoul.run` (and mid-turn through the
  steering queue), over the Wire JSON-RPC protocol (`src/pythinker_code/wire/server.py`),
  through the ACP server (`src/pythinker_code/acp/server.py`), via CLI flags
  (`src/pythinker_code/cli/__init__.py`), from config files (`src/pythinker_code/config.py`),
  and over HTTP for the web and dashboard backends.
- **Where untrusted content is parsed.** Model tool-call arguments are validated in
  `pythinker_core.tooling` (`CallableTool2`); MCP output flows through
  `pythinker_core.tooling.mcp`; web fetch/search results, file reads, and background task
  output are wrapped with `UntrustedData` (`src/pythinker_code/utils/trust.py`); ingested
  `AGENTS.md` is run through `strip_invisible_chars` and size-capped before it reaches the
  system prompt; plugin and update downloads are SSRF- and size-guarded.
- **Where authorization happens.** `Approval.request` in `src/pythinker_code/soul/approval.py`
  is the single gate for side-effecting tool calls (permgate-1a coarse action, permgate-1b
  destructive exclusion, permgate-2 config-surface exclusion, permgate-3 sibling drain).
  Config-surface edits classified by `is_config_surface_path`
  (`src/pythinker_code/utils/path.py`) are never session-approved. Subagent tool access is
  constrained by `ToolPolicy` allowlists.
- **Where side effects occur.** Filesystem, shell, and SSH execution are funneled through
  `packages/pythinker-host`; background work runs in isolated processes with a cleaned
  environment (`get_clean_env`); auth performs network calls and persists tokens (config files
  written `chmod 0o600`, or the OS keyring); telemetry egress to Sentry/OTel is opt-out.

## Runtime path

The end-to-end flow when a session starts and processes a turn:

1. **Process entry** — `src/pythinker_code/__main__.py:main` routes into the Typer tree at
   `src/pythinker_code/cli/__init__.py`, which parses flags and constructs the app.
2. **App setup** — `src/pythinker_code/app.py:PythinkerCLI.create` loads config, selects the
   LLM, restores the session and `Context`, builds the `Runtime`, loads the agent spec, and
   constructs `PythinkerSoul`. `run_shell` / `run_print` / `run_acp` / `run_wire_stdio` select
   the frontend.
3. **Agent spec loading** — `src/pythinker_code/agentspec.py:load_agent_spec` parses and
   validates YAML specs (resolving `extend`); tools are loaded by import path and subagent
   types registered later in `src/pythinker_code/soul/agent.py:load_agent`.
4. **Core loop** — `src/pythinker_code/soul/pythinkersoul.py:PythinkerSoul.run` handles user
   input and slash commands, calls the LLM through `pythinker_core.step`, runs tools, gates
   side effects through approvals, injects dynamic reminders, and compacts the context.
5. **Tool execution** — `src/pythinker_code/soul/toolset.py:PythinkerToolset` loads built-in
   and MCP tools, injects dependencies, executes calls, and returns structured results.
6. **Wire and UI** — `src/pythinker_code/soul/run_soul` connects the soul to
   `src/pythinker_code/wire/`; Shell, Print, ACP, Web, and Dashboard frontends consume Wire events.

## Runtime and entry

| Path | Purpose | Key entry points and interfaces |
| --- | --- | --- |
| `src/pythinker_code/__main__.py` | Process entry. | `main` |
| `src/pythinker_code/cli/` | Typer command tree and UI-mode routing; lazy-loaded subcommands. | `cli`, `pythinker`, `login`, `logout`, `term`, `acp`, lazy group `info`, `export`, `mcp`, `plugin`, `skill`, `review`, `secscan`, `security-scan`, `debug`, `update`, `dashboard`, `web` |
| `src/pythinker_code/app.py` | Builds `PythinkerCLI`, `Runtime`, and `PythinkerSoul`; wires telemetry and frontends. | `PythinkerCLI.create`, `PythinkerCLI.run`, `run_shell` / `run_print` / `run_acp` / `run_wire_stdio` |
| `src/pythinker_code/config.py` | Three-scope config resolution (user → project → local TOML) with env overlay and JSON→TOML migration; `SecretStr` fields; scope locks on `api_key`/`providers`/`services`. | `Config`, `load_config`, `save_config`, `get_config_file` |
| `src/pythinker_code/llm.py` | Provider/model selection and capability derivation; wires `pythinker-core` backends. | `LLM`, `create_llm`, `augment_provider_with_env_vars`, `derive_model_capabilities` |
| `src/pythinker_code/agentspec.py` | Parses/validates agent YAML specs and resolves `extend`. | `load_agent_spec`, `ResolvedAgentSpec`, `DEFAULT_AGENT_FILE` |

## Soul: the agent loop

The soul is the heart of the runtime. Beyond the loop itself it owns approvals, context and
compaction, slash commands, dynamic prompt injection, and a checkpoint-rewind mechanism.

The agent loop emits per-turn and per-step wire events for orchestration observability. The
canonical list lives in `src/pythinker_code/wire/types.py` (`Event` union): `StepBegin`,
`StepRetry`, `StepInterrupted`, `ToolExecutionStarted`, `StatusUpdate`, plus
`TodoListUpdated`, `SubagentToolFallback`, `AgentListDelta`, `ToolUseSkipped`, and
`ContextOverflowRecovered`.

| Path | Purpose | Key entry points and interfaces |
| --- | --- | --- |
| `src/pythinker_code/soul/pythinkersoul.py` | Core loop: user input, slash commands, LLM calls, tool runs, compaction, telemetry spans. | `PythinkerSoul`, `PythinkerSoul.run`, `FLOW_COMMAND_PREFIX` |
| `src/pythinker_code/soul/agent.py` | `Runtime` and `Agent` construction, system-prompt assembly, AGENTS.md discovery. | `Runtime`, `Agent`, `load_agent`, `load_agents_md`, `BuiltinSystemPromptArgs` |
| `src/pythinker_code/soul/context.py` | Conversation history, checkpoints, JSONL persistence. | `Context` |
| `src/pythinker_code/soul/toolset.py` | Loads built-in + MCP tools, injects deps, executes calls. | `PythinkerToolset` |
| `src/pythinker_code/soul/slash.py` | Slash-command registry and dispatch. | `registry` |
| `src/pythinker_code/soul/dynamic_injection.py` (+ `dynamic_injections/`) | Injects budgeted `<system-reminder>` content per step: plan-mode, auto-mode, model-defense. | `DynamicInjectionProvider` |
| `src/pythinker_code/soul/permission.py` | Per-step permission profiles (`read_only`/`plan`/`ask`/`implement`/`review`/`verify`) and destructiveness classification. | `tool_destructive_reason`, `shell_command_signature` |
| `src/pythinker_code/soul/denwarenji.py` | D-Mail checkpoint rewind (`BackToTheFuture`). | — |
| `src/pythinker_code/soul/flow_runner.py` | Ralph Loop driver for `/flow` and iterative commands. | — |
| `src/pythinker_code/soul/deliberation.py` | Blind-advisor deliberation for auto-mode decisions. | `deliberation_scope` |

## Approvals and trust

| Path | Purpose | Key entry points and interfaces |
| --- | --- | --- |
| `src/pythinker_code/soul/approval.py` | The approval gate for side-effecting tool calls; config-surface protection; session-scope rules; unattended runs fail closed. | `Approval`, `Approval.request`, `Approval.share`, `ApprovalState`, `ApprovalResult` |
| `src/pythinker_code/approval_runtime/` | Session source of truth for pending approvals; projected to the root Wire stream. | `ApprovalRuntime`, `ApprovalRequestRecord`, `ApprovalSource` |

Role: `auth-authz`. Never bypass approvals by calling lower-level side-effecting helpers
directly; route through `Approval.request`.

## Agent specs and subagents

| Path | Purpose | Key entry points and interfaces |
| --- | --- | --- |
| `src/pythinker_code/agents/` | Built-in YAML specs and prompt files (`default/`, `okabe/`). Registration keys live in `agent.yaml:subagents`. | `default/agent.yaml`, per-role YAMLs via `extend: ./agent.yaml` |
| `src/pythinker_code/subagents/` | Registry, builders, foreground/background runners, and per-instance persistence under `session/subagents/<agent_id>/`. | `LaborMarket`, `SubagentStore`, `SubagentBuilder`, `ForegroundSubagentRunner`, `SubagentRunSpec`, `prepare_soul` |

Built-in subagent roles (12), from `agents/default/agent.yaml`: `coder`, `code-reviewer`,
`debugger`, `explore`, `plan`, `planner`, `scout`, `review`, `security-reviewer`,
`implementer`, `judge`, `verifier`. External Markdown agents (from `.claude/agents`,
`.pythinker/agents`, `.codex/agents`, `.agents/agents`) are discovered and materialized to
wrapper YAMLs; built-ins win name conflicts. Subagents inherit the parent
`Runtime.builtin_args` via `copy_for_subagent()`.

## Tools

Tools are small, async, dependency-injected `CallableTool2[Params]` classes registered by
import path (`pythinker_code.tools.<name>:<Name>`); descriptions load from `.md` files via
`load_desc`. Side-effecting tools pass through `Approval.request`, and external content is
wrapped with `UntrustedData`.

| Path | Tools |
| --- | --- |
| `src/pythinker_code/tools/file/` | `ReadFile`, `WriteFile`, `StrReplaceFile`, `Glob`, `Grep`, `ReadMediaFile` |
| `src/pythinker_code/tools/shell/` | `Shell` |
| `src/pythinker_code/tools/web/` | `SearchWeb`, `FetchURL` (conditional on deps) |
| `src/pythinker_code/tools/agent/` | `Agent`, `RunAgents` |
| `src/pythinker_code/tools/background/` | `TaskOutput`, `TaskList`, `TaskInput`, `TaskStop`, `TaskHandoff` |
| `src/pythinker_code/tools/` (other) | `AskUserQuestion`, `EnterPlanMode`/`ExitPlanMode`, `Think`, `SetTodoList`, `Memory`, `Recall`, `Scratchpad`, `Suggest`, `Progress`, `ReadSkill`, `SendDMail`, `ListMcpResources`/`ReadMcpResource` |
| `src/pythinker_code/tools/utils.py`, `display.py` | `ToolResultBuilder`, `ToolResultStatus`, `load_desc`; `DiffDisplayBlock`, `TodoDisplayBlock`, `BackgroundTaskDisplayBlock`, `ShellDisplayBlock` |

Tools should depend on `pythinker_core.tooling` types (for example `ToolReturnValue`,
`DisplayBlock`) rather than `pythinker_code/wire/`, except for the documented bridge tools.
See `src/pythinker_code/tools/AGENTS.md`.

## Providers, auth, and usage

| Path | Purpose | Key entry points and interfaces |
| --- | --- | --- |
| `src/pythinker_code/auth/` | Multi-provider OAuth/API-key auth; shared token store and refresh; platform registry. | `OAuthManager`, `refresh_token`, `OAuthToken`, `Platform`, `managed_provider_key`, `managed_model_key`, `parse_managed_provider_key`, `list_models` |
| `src/pythinker_code/usage_ratelimit_cache.py` | Rate-limit cache fed by HTTP response hooks; backs `/usage` when no adapter data exists. | `RateLimitCache` |
| `src/pythinker_code/ui/shell/usage_adapters/` | Per-platform usage adapters keyed by `platform_id` in `ADAPTERS`. | — |

Provider modules in `auth/`: `openai`, `anthropic_direct`, `opencode_go`, `minimax`,
`deepseek`, `openrouter`, `z_ai`, `alibaba`, `lm_studio`, `ollama`, `moonshot`, and
`github_feedback`. Managed provider keys follow `managed:<platform_id>`; managed model ids
follow `<platform_id>/<model_id>`. Provider-aware code derives the provider from the active
model; `/usage` defaults to the active provider, with `/usage all` as the explicit aggregate.

## Wire and UI frontends

| Path | Purpose | Key entry points and interfaces |
| --- | --- | --- |
| `src/pythinker_code/wire/` | JSON-RPC 2.0 event protocol between soul and UIs; `wire.jsonl` session persistence/replay (current 1.9, legacy 1.1). | `Wire`, `WireServer`, `WireMessage` (discriminated `Event` \| `Request`), `ApprovalRequest`, `ToolCallRequest`, `QuestionRequest`, `HookRequest`, `RootWireHub`, `serialize_wire_message` |
| `src/pythinker_code/ui/shell/` | Default interactive TUI: prompt, slash autocomplete, streaming visualization, tool renderers, theme, usage display. | `Shell`, `CustomPromptSession`, `register_tool_renderer`, `visualize`, `get_tui_tokens` |
| `src/pythinker_code/ui/print/` | Non-interactive output (text / stream-json). | `Print` |
| `src/pythinker_code/ui/acp/` | Deprecated single-session ACP shim (raises on use); the live server is `src/pythinker_code/acp/`. | `ACP` |

The agent loop emits per-turn and per-step events for UI, replay, and dashboard consumers. The
canonical list lives in the `Event` union in `src/pythinker_code/wire/types.py`; commonly consumed
events include `StepBegin`, `StepRetry`, `StepInterrupted`, `ToolExecutionStarted`, `StatusUpdate`,
`TodoListUpdated`, `SubagentToolFallback`, `AgentListDelta`, `ToolUseSkipped`, and
`ContextOverflowRecovered`.

The shell can run with a working directory inside its subtree, so `src/pythinker_code/ui/`
is a candidate for a focused nested guide on prompt, visualization, and component layout.

## ACP server

| Path | Purpose | Key entry points and interfaces |
| --- | --- | --- |
| `src/pythinker_code/acp/` | Multi-session JSON-RPC ACP server for IDE integrations; client-backed filesystem (`ACPHost`) and approval bridge. | `acp_main`, `ACPServer`, `ACPSession`, `ACPContentBlock` |

Full session lifecycle: `initialize`, `new_session`, `load_session`, `resume_session`,
`list_sessions`, `set_session_mode`, `set_session_model`, `set_config_option`,
`close_session`, `authenticate`, `prompt`, `cancel`. Untrusted model output is stripped via
`strip_untrusted_envelope` at the `convert.py` boundary. See
`src/pythinker_code/acp/AGENTS.md`.

## Skills, hooks, and plugins

| Path | Purpose | Key entry points and interfaces |
| --- | --- | --- |
| `src/pythinker_code/skill/`, `src/pythinker_code/skills/` | Skill discovery/loading across scopes (project > user > extra > built-in), local specialization, flow skills; injected via `PYTHINKER_SKILLS`. Bundled skills live in `skills/`. | `Skill`, `discover_skills_from_roots`, `index_skills`, `format_skills_for_prompt`, `Flow`, `SkillLockFile` |
| `src/pythinker_code/hooks/` | Lifecycle hook engine: 13 events, server-side shell commands and client-side Wire subscriptions; fail-open (block only on explicit exit code 2 / structured deny). | `HookEngine`, `HookDef`, `HookEventType`, `HOOK_EVENT_TYPES`, `run_hook`, `events` |
| `src/pythinker_code/plugin/` | Plugin discovery, install (local/git/zip with SSRF + traversal guards, staged atomic install), and subprocess tool execution with fresh credential injection. | `parse_plugin_json`, `PluginSpec`, `install_plugin`, `list_plugins`, `load_plugin_tools`, `PluginTool` |

## Memory, background, and notifications

| Path | Purpose | Key entry points and interfaces |
| --- | --- | --- |
| `src/pythinker_code/memory/` | Relevance-ranked recall and injection of per-project durable memory and scratch notes; approval-gated consolidation; compaction harvesting. Store lives at `~/.pythinker/projects/<project-key>/memory/` (`MEMORY.md`, `USER.md`, `JOURNAL.md`). | `RecallInjectionProvider`, `gather_candidates`, `LexicalRetriever`, `generate_inbox_candidates`, `CompactionHarvester`, `sanitize_candidate_block` |
| `src/pythinker_code/background/` | Async bash and subagent tasks with lifecycle, process control, heartbeat/staleness recovery, and disk-backed state. | `BackgroundTaskManager`, `BackgroundAgentRunner`, `BackgroundTaskStore`, `TaskView`, `TaskSpec`, `run_background_task_worker` |
| `src/pythinker_code/notifications/` | Notification delivery queue with claim/ack/recover; LLM-facing notification messages. | `NotificationManager`, `NotificationWatcher`, `NotificationEvent` |
| `src/pythinker_code/telemetry/` | Opt-out telemetry (OTel + Sentry) with event buffering; scrubbing at transport. | `track`, `attach_sink`, `is_enabled`, `otel.init`, `sentry.init` |
| `src/pythinker_code/prompts/` | `INIT` and `COMPACT` prompt templates. | `INIT`, `COMPACT` |
| `src/pythinker_code/deps/` | Build-time `Makefile` target that vendors the ripgrep binary (`download-ripgrep`). | — |

## Utilities

`src/pythinker_code/utils/` holds shared, security-relevant helpers. Notable: `trust.py`
(`UntrustedData`, `strip_invisible_chars`, `strip_untrusted_envelope`, `INVISIBLE_CHARS`),
`path.py` (`find_project_root`, `is_config_surface_path`, `list_directory`,
`is_within_workspace`), `io.py` (`atomic_json_write`), `subprocess_env.py` (`get_clean_env`),
`export.py` (session export/import), `slashcmd.py`, `frontmatter.py`, `sensitive.py`,
`aioqueue.py`/`broadcast.py`, and the `rich/` styling subpackage. `is_config_surface_path`
is load-bearing for approval gating of persistent-backdoor vectors (`AGENTS.md`, agent specs,
`.pythinker` config).

## Web and dashboard backends and frontends

| Path | Purpose | Key entry points and interfaces |
| --- | --- | --- |
| `src/pythinker_code/web/` | FastAPI backend (port 5494) managing CLI sessions via subprocess workers; bearer-token auth; `/api/*`; sensitive-path restriction. | `create_app`, `run_web_server`, `PythinkerCLIRunner`, `SessionProcess`, `AuthMiddleware` |
| `src/pythinker_code/dashboard/` | FastAPI read-only tracing/statistics backend (port 5495) for the visualizer. | `create_app`, `run_dashboard_server` |
| `web/` | React 19 + Vite 8 + TypeScript SPA chat UI; bundled into the package. See `web/AGENTS.md`. | `main.tsx`, `App`, `apiClient`, generated client `src/lib/api/`, `useSessionStream` |
| `dashboard/` | React 19 + Vite session-tracing visualizer. See `dashboard/AGENTS.md`. | `main.tsx`, `App`, hand-written `src/lib/api.ts` (`WireEvent`, `ContextMessage`, `SessionInfo`), feature panels under `src/features/` |

Both frontends build with `tsc -b && vite build` and are synced into the Python package by
`scripts/build_web.py` (`web/dist` → `src/pythinker_code/web/static`) and
`scripts/build_dashboard.py` (`dashboard/dist` → `src/pythinker_code/dashboard/static`).

## Workspace packages and SDK

| Path | Purpose | Key entry points and interfaces |
| --- | --- | --- |
| `packages/pythinker-core/` | LLM abstraction: message models, streaming chat providers, tool abstractions, and the `generate`/`step` primitives. Independently versioned (1.x). | `generate`, `step`, `Message`, `ContentPart`, `ToolCall`, `ChatProvider`, `Toolset`, `ToolReturnValue`/`ToolOk`/`ToolError`, `CallableTool2`, `DisplayBlock`; contrib providers (`Anthropic`, `GoogleGenAI`, `OpenAIResponses`) and `LinearContext` |
| `packages/pythinker-host/` | OS abstraction for filesystem + shell across local and SSH backends via a context-var-dispatched `Host` protocol. | `Host`, `HostPath`, `LocalHost`, `HostProcess`, `get_current_host`/`set_current_host` |
| `packages/pythinker-review/` | Standalone review/security/debug engine and stateful Reviewflow; strict Pydantic schemas with fail-closed evidence validation. State in `.pythinker-review/` and `.pythinker-review-flow/`. See `packages/pythinker-review/AGENTS.md`. | `run_engine`, `ReviewLLM`, `Finding`, `RawFinding`, `ReviewerOutput`, Reviewflow `init`/`map`/`review`/`fix` |
| `packages/pythinker-code/` | Thin distribution package exposing the `pythinker-code` script. | — |
| `sdks/pythinker-sdk/` | Lightweight async SDK for the Pythinker API with MCP integration; re-exports core types. | `PythinkerClient`, `Conversation`, `MCPToolset`, `MCPServerConfig` |

Review artifact commands (`describe`, `improve`/`suggest`, `ask`, `labels`, `changelog`,
`docs`, `compliance`) are read-only; only Reviewflow `fix` and `open-pr` mutate.

## Tests

| Path | Purpose |
| --- | --- |
| `tests/` | Unit/integration, organized by subsystem (`auth/`, `acp/`, `core/`, `tools/`, `cli/`, `hooks/`, `background/`, `notifications/`, `telemetry/`, `subagents/`, `ui/`, `dashboard/`, `web/`, `e2e/`). Shared fixtures (`config`, `llm`, `runtime`, `session`, `tools`) in `tests/conftest.py`; `pytest.ini` sets `asyncio_mode=auto` and excludes `tests_e2e`. |
| `tests_e2e/` | End-to-end `pythinker --wire` JSON-RPC tests (W-01…W-42 taxonomy) plus CLI/MCP flows; `wire_helpers.py`, `cassette.py` record/replay; `inline_snapshot` with path normalization. See `tests_e2e/AGENTS.md`. |
| `tests_ai/` | Accuracy smoke harness invoking agents via the Harbor framework (`scripts/run.py`). |

## Build, tooling, and release

Builds and checks fan out across the workspace from the root `Makefile`
(`make prepare` / `format` / `check` / `test` / `ai-test` / `build` / `build-bin`, plus
`web-*` / `dashboard-*` dev servers and per-package `check-*` / `test-*`). Tooling: `uv` workspace,
`ruff` (lint + format), `pyright` (enforced), `ty` (advisory). `scripts/release.py` rewrites
versions across the five packages but never pushes `main` or tags. Distribution covers
PyInstaller binaries (`pythinker.spec`), native installers (`scripts/install-native.sh`,
`scripts/install.ps1`), and OS package managers (Homebrew, Scoop, winget). Release workflows
in `.github/workflows/` trigger on `v[0-9]+.[0-9]+.[0-9]+` tags. The docs site (`docs/`) is
VitePress; the English changelog is auto-synced from the root `CHANGELOG.md` via
`npm run sync` and must not be hand-edited.
