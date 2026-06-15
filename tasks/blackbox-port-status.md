# Blackbox Reference Port — Live Status Ledger

> Reactivated 2026-06-15 on `feat/agent-behaviour-tweaks`. Source plan:
> `docs/superpowers/plans/agent_enhancment.md`. Reference tree (read-only):
> `blackbox/pythinker-src/`.

## Legend

| Column | Meaning |
| --- | --- |
| `gap_id` | Roadmap gap or task id |
| `phase` | Roadmap phase |
| `reference_path` | Blackbox source (or `missing-reference`) |
| `target_paths` | Pythinker modules |
| `status` | `todo`, `verify-existing`, `in_progress`, `done`, `skipped`, `future-approved-only` |
| `test_gate` | Focused test command or phase gate |
| `notes` | Decision, rationale, de-scope proof |

## Whole-Source Reverse Engineering Inventory

| reference_area | reference_paths | target_paths | decision | status | tests_or_skip_gate |
| --- | --- | --- | --- | --- | --- |
| Runtime loop | `query.ts`, `query/` | `soul/pythinkersoul.py`, `soul/context.py`, `soul/compaction.py` | adapt | todo | Phase 2 gate |
| Tool contract | `services/tools/`, `tools/**` | `soul/toolset.py`, `tools/**`, `hooks/**` | adopt/adapt | todo | Phase 2 gate |
| CLI/bootstrap | `entrypoints/cli.tsx`, `main.tsx` | `cli/__init__.py`, `app.py`, `ui/shell/` | adapt; skip Bun/Ink | skip | De-scope: Typer/Rich native |
| Non-interactive transports | `cli/print.ts`, `entrypoints/sdk/` | `ui/print/`, `wire/`, `acp/` | adapt; skip CCR | skip | De-scope: CCR remote transport |
| Terminal UI | `screens/`, `components/`, `ink/` | `ui/shell/`, `wire/` | adapt; skip Ink | adapt | Phase 7 gate |
| Slash commands | `commands/**` | `soul/slash.py`, `ui/shell/slash.py` | adapt | todo | Phase 5 |
| Keybindings | `keybindings/`, `components/PromptInput/` | `ui/shell/keymap.py`, `ui/shell/prompt.py` | adapt | todo | Phase 5.11 |
| Permissions UI | `components/permissions/`, `utils/permissions/` | `soul/permission.py`, `soul/approval.py`, `approval_runtime/` | adopt invariants | verify-existing | Phase 1.3–1.4 |
| Background tasks | `Task.ts`, `tasks/**` | `background/`, `tools/background/` | adapt; skip remote/dream | adapt | native-equivalent partial |
| Services core | `services/tools/`, `services/compact/` | `soul/toolset.py`, `soul/compaction.py` | adopt/adapt | todo | Phase 2 |
| MCP | `services/mcp/**` | `soul/toolset.py`, `tools/mcp_resource/`, `cli/mcp.py` | adopt portable | verify-existing | Phase 4; reconnect todo |
| Memory | `memdir/**` | `project_memory.py`, `memory/`, `tools/memory/`, `tools/recall/` | adopt hygiene | verify-existing | Phase 3 |
| Skills | `skills/**` | `skill/__init__.py`, `tools/skill/`, `skills/**` | adapt | todo | Phase 3.4–3.8 |
| Agents/subagents | `tools/AgentTool/**` | `agentspec.py`, `subagents/`, `tools/agent/` | adapt | todo | Phase 5 |
| Hooks | `utils/hooks/**` | `hooks/**` | adapt | todo | Phase 5.8 |
| Plugins | `plugins/**` | `plugin/`, `cli/plugin.py` | adapt if approved | todo | Phase 3.9 |
| Settings | `utils/settings/**` | `config.py` | adapt selective | todo | Phase 7.7 |
| Output styles | `outputStyles/**` | dynamic injections | skip | skipped | De-scope: not Pythinker goal |
| Auth/providers | `utils/auth.ts`, `services/oauth/**` | `auth/**`, `llm.py` | native-equivalent | verify-existing | Phase 7.6 audit |
| Constants/schemas | `constants/**`, `schemas/**` | `agents/default/system.md`, `wire/types.py` | adapt | verify-existing | Phase 1.1 |
| Native TS | `native-ts/file-index/` | `ui/shell/prompt.py` | adapt ideas | todo | Phase 7.4 |
| File/search UX | `tools/GrepTool`, `tools/FileReadTool` | `tools/file/**` | adapt | verify-existing | Phase 1.2 |
| LSP | `services/lsp/**` | none | future-approved-only | future-approved-only | Too large without approval |
| Sandbox | `utils/sandbox/**` | none | future-approved-only | future-approved-only | Phase 7.8 decision |
| Computer/voice/buddy | `voice/**`, `buddy/**` | none | skip | skipped | Product-only features |
| Remote/bridge | `bridge/**`, `remote/**` | `acp/`, `wire/` | native-equivalent IDE | skipped | CCR not a goal |
| Migrations | `migrations/**` | `config.py` helpers | adapt patterns | todo | Phase 7.7 |
| Scratch/runtime | `.pythinker/scratch/` | `scratchpad.py` | native-equivalent | done | Existing |
| Harness/evals | `services/vcr.ts` | `tests_e2e/`, `tests_ai/` | adapt | todo | Phase 6 |

## Missing / Ambiguous Reference Artifacts

| artifact | status | substitute |
| --- | --- | --- |
| `blackbox/pythinker-src/src/query/transitions.ts` | missing-reference | `query/stopHooks.ts`, `query/tokenBudget.ts`, `query.ts` |
| `blackbox/pythinker-src/src/skills/mcpSkills.js` | missing-reference | Do not infer; audit `skill/__init__.py` only |

## Roadmap Task Status (61 tasks)

| gap_id | phase | reference_path | target_paths | status | test_gate | notes |
| --- | ---: | --- | --- | --- | --- | --- |
| 1.1 | 1 | `constants/prompts.ts`, `utils/messages.ts`, `utils/xml.ts` | `agents/default/system.md`, `utils/trust.py` | done | `tests/utils/test_trust.py`, `tests/core/test_default_agent.py` | Verified 2026-06-15; focused tests pass |
| 1.2 | 1 | `utils/messages.ts`, `tools/` | `tools/web/*`, `tools/file/grep_local.py`, `tools/shell/`, `tools/mcp_resource/`, `tools/recall/` | done | `tests/tools/test_untrusted_wrapping.py` | Verified 2026-06-15; wrapping tests pass |
| 1.3 | 1 | `utils/permissions/` | `tools/file/__init__.py`, `soul/approval.py` | done | `tests/core/test_approval_auto.py` | Verified 2026-06-15; config/dangerous edit gates covered |
| 1.4 | 1 | `utils/permissions/permissions.ts` | `soul/permission.py`, `approval_runtime/` | done | `tests/core/test_approval_auto.py`, `tests/core/test_permission_profiles.py` | Verified 2026-06-15; 102 approval tests pass |
| 2.1 | 2 | `query.ts`, `services/tools/toolExecution.ts` | `tools/utils.py`, `tools/shell/` | done | `tests/utils/test_result_builder.py` | Verified; spill + recovery hints |
| 2.2 | 2 | compaction reference | `soul/compaction.py`, `soul/context.py` | done | `tests/core/test_context_pruning.py` | prune_stale_tool_outputs present |
| 2.3 | 2 | tool descriptions | `tools/**/*.md`, `tools/agent/` | done | `tests/tools/test_tool_descriptions.py` | when-to-use in tool .md files |
| 2.4 | 2 | `query.ts` | `soul/pythinkersoul.py` | verify-existing | soul recovery tests | orphan repair, truncation nudge |
| 2.5 | 2 | hooks/permissions | `hooks/engine.py`, `soul/permission.py` | verify-existing | `tests/hooks/test_engine.py` | PreToolUse block tests |
| 2.6 | 2 | `toolExecution.ts` | `soul/toolset.py` | todo | tool input tests | Immutability audit pending |
| 2.7 | 2 | `toolOrchestration.ts` | `soul/toolset.py` | todo | shell parallel tests | Cascade decision pending |
| 2.8 | 2 | `context.ts` | `soul/dynamic_injections/git_status.py` | done | `tests/core/test_git_status_injection_provider.py` | Reuses collect_git_context |
| 2.9 | 2 | compaction | `soul/compaction.py` | todo | compaction tests | API-round grouping |
| 2.10 | 2 | `query.ts` | `soul/compaction.py` | todo | compaction failure tests | Autocompact circuit breaker |
| 2.11 | 2 | compaction cleanup | `soul/compaction.py`, `soul/context.py` | todo | compaction tests | Post-compact cleanup |
| 2.12 | 2 | `tools.ts` | `soul/toolset.py` | todo | toolset tests | Pool ordering + collisions |
| 3.1 | 3 | memdir recall | `tools/recall/__init__.py` | verify-existing | `tests/tools/test_recall.py` | Cross-session recall |
| 3.2 | 3 | `memdir/**` | `project_memory.py`, `memory/` | todo | memory tests | Durable defaults |
| 3.3 | 3 | working-set recall | `memory/recall.py` | verify-existing | recall injection tests | Re-arm on working-set shift |
| 3.4 | 3 | `skills/loadSkillsDir.ts` | `skill/__init__.py` | todo | skill tests | Resource manifests |
| 3.5 | 3 | bundled skills | `skills/**` | todo | skill load tests | Config authoring skills |
| 3.6 | 3 | `memoryScan.ts` | `project_memory.py` | todo | memory scan tests | Manifest scan |
| 3.7 | 3 | memory prompts | `agents/default/system.md` | todo | prompt tests | Ignore/trust invariants |
| 3.8 | 3 | skill frontmatter | `skill/__init__.py` | todo | skill tests | Frontmatter parity |
| 3.9 | 3 | `plugins/**` | `plugin/` | todo | docs | Plugin scope ledger |
| 4.1 | 4 | `services/mcp/client.ts` | `tools/mcp_resource/` | verify-existing | `tests/tools/test_mcp_resource.py` | Resources; prompts deferred |
| 4.2 | 4 | MCP live refresh | `cli/mcp.py`, `soul/toolset.py` | todo | MCP tests | reconnect/list_changed — see tasks/todo.md |
| 4.3 | 4 | MCP docker stdio | `soul/toolset.py` | todo | MCP tests | Close timeouts |
| 4.4 | 4 | MCP prompts | future `tools/mcp_prompt/` | todo | MCP prompt tests | InvokeMcpPrompt |
| 4.5 | 4 | `elicitationHandler.ts` | `wire/`, `acp/` | future-approved-only | — | Needs Wire contract approval |
| 4.6 | 4 | MCP naming | `soul/toolset.py` | todo | MCP tests | Server name normalization |
| 4.7 | 4 | MCP OAuth | `auth/`, `cli/mcp.py` | future-approved-only | — | Maintainer decision |
| 5.1 | 5 | plan mode | `soul/permission.py`, subagents | todo | plan mode tests | Subagent restrictions |
| 5.2 | 5 | subagent usage | `subagents/`, `tools/agent/` | todo | agent tests | Token/cost roll-up |
| 5.3 | 5 | plan tool | `tools/plan/` | todo | plan tests | Verification in written plans |
| 5.4 | 5 | `TodoWriteTool` | `tools/todo/` | verify-existing | `tests/tools/test_todo.py` | cancelled status |
| 5.5 | 5 | progress UI | `tools/progress/` | verify-existing | progress tests | ProgressNote producer |
| 5.6 | 5 | suggestions | `tools/suggest/` | verify-existing | suggest tests | Non-blocking suggestions |
| 5.7 | 5 | ACP questions | `acp/`, `tools/ask_user/` | todo | ACP tests | Question consistency |
| 5.8 | 5 | `schemas/hooks.ts` | `hooks/events.py` | todo | hook tests | Event parity matrix |
| 5.9 | 5 | markdown agents | `agentspec.py` | todo | agentspec tests | Field parity |
| 5.10 | 5 | `processUserInput/` | `ui/shell/` | todo | shell tests | Command processing |
| 5.11 | 5 | `keybindings/` | `ui/shell/keymap.py` | todo | shell tests | Keybinding parity |
| 5.12 | 5 | REPL tips | `ui/shell/` | todo | manual smoke | Spinner tips |
| 5.13 | 5 | suggestions fork | `tools/suggest/` | todo | suggest tests | No speculation fork |
| 6.1 | 6 | telemetry tree | `telemetry/` | todo | telemetry tests | GenAI trace tree |
| 6.2 | 6 | `services/vcr.ts` | `tests_e2e/` | todo | replay tests | Record/replay HTTP |
| 6.3 | 6 | eval harness | `tests_ai/` | todo | eval gates | Trajectory evals |
| 6.4 | 6 | failure thresholds | `soul/pythinkersoul.py` | todo | soul tests | Graceful yield |
| 6.5 | 6 | max steps | `soul/pythinkersoul.py` | todo | soul tests | Final handoff turn |
| 6.6 | 6 | telemetry sanitize | `telemetry/` | todo | telemetry tests | Tool name sanitization |
| 6.7 | 6 | VCR fixtures | `tests_e2e/` | todo | fixture discipline | Eval fixture rules |
| 7.1 | 7 | model defense | `soul/dynamic_injections/model_defense.py` | verify-existing | injection tests | Model-keyed defense |
| 7.2 | 7 | `screens/REPL.tsx` | `ui/shell/` | todo | shell tests | Shell UI surfaces |
| 7.3 | 7 | Ink engine | `ui/shell/` | skipped | — | De-scope: no Pi-TUI replacement |
| 7.4 | 7 | `native-ts/file-index/` | `ui/shell/prompt.py` | todo | prompt tests | File mention polish |
| 7.5 | 7 | media limits | `config.py`, tools | todo | config tests | API limit constants |
| 7.6 | 7 | `utils/auth.ts` | `auth/**` | todo | auth tests | Provider pattern audit |
| 7.7 | 7 | `migrations/**` | `config.py` | todo | config tests | Migration audit |
| 7.8 | 7 | `utils/sandbox/**` | none | future-approved-only | — | Sandbox decision |
| 7.9 | 7 | session search | `soul/context.py` | todo | session tests | Search semantics |

## Explicit De-Scope (skip proof)

| area | decision | rationale |
| --- | --- | --- |
| GrowthBook/Statsig, new telemetry endpoints | skip | Opt-out OTel only; no new hosted telemetry per AGENTS.md |
| pythinkerai hosted MCP, TEAMMEM, KAIROS, voice, buddy | skip | Product-hosted; outside CLI goals |
| React/Ink renderer, Yoga layout | skip | Pythinker uses Rich/prompt_toolkit |
| Output styles directory | skip | Use dynamic injections only if approved |
| CCR remote bridge | skip | ACP/wire cover IDE integration |
| Pi-TUI engine replacement | skip | Phase 7.3 explicit |

## Phase Exit Gates

| phase | gate | status |
| ---: | --- | --- |
| 0 | Ledger complete; maintainers see remain/skip | done |
| 1 | `make check-pythinker-code && make test-pythinker-code` | verify-existing done; full suite has 7 pre-existing UI failures on branch |
| 2 | focused + check; full test if shared context changed | pending |
| 3–8 | per plan dashboard | pending |
