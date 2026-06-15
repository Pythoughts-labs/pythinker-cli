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
| 2.6 | 2 | `toolExecution.ts` | `soul/toolset.py` | done | `tests/core/test_toolset.py` | tool input snapshots isolated |
| 2.7 | 2 | `toolOrchestration.ts` | `soul/toolset.py` | done | `tests/core/test_toolset.py` | Shell remains exclusive; no parallel cascade |
| 2.8 | 2 | `context.ts` | `soul/dynamic_injections/git_status.py` | done | `tests/core/test_git_status_injection_provider.py` | Reuses collect_git_context |
| 2.9 | 2 | compaction | `soul/compaction.py` | done | `tests/core/test_simple_compaction.py` | multi-round tool pair regression |
| 2.10 | 2 | `query.ts` | `soul/pythinkersoul.py`, `config.py` | done | `tests/core/test_pythinkersoul_retry_recovery.py` | compaction_failed handoff |
| 2.11 | 2 | compaction cleanup | `soul/pythinkersoul.py`, `soul/context.py` | verify-existing | `tests/core/test_dynamic_injection_hooks.py`, `tests/core/test_compaction_restore.py` | provider rearm + restore invariants |
| 2.12 | 2 | `tools.ts` | `soul/toolset.py` | done | `tests/core/test_toolset.py` | deterministic MCP publish order |
| 3.1 | 3 | memdir recall | `tools/recall/__init__.py` | done | `tests/tools/test_recall.py` | search/read + bounded read windows |
| 3.2 | 3 | `memdir/**` | `config.py`, `project_memory.py`, `memory/` | done | `tests/core/test_project_memory.py`, `tests/core/test_memory_phase_bcd.py` | durable memory remains opt-in |
| 3.3 | 3 | working-set recall | `memory/recall.py` | done | `tests/core/test_memory_phase_bcd.py` | re-arms on working-set shift + memory mtime |
| 3.4 | 3 | `skills/loadSkillsDir.ts` | `skill/__init__.py`, `tools/skill/` | done | `tests/tools/test_skill_tool.py` | resource manifests present |
| 3.5 | 3 | bundled skills | `skills/customize-pythinker/`, `skills/agent-creator/` | done | `tests/core/test_builtin_authoring_skills.py` | config + agent authoring skills present |
| 3.6 | 3 | `memoryScan.ts` | `memory/recall.py`, `project_memory.py` | done | `tests/core/test_memory_phase_bcd.py` | lexical manifest scan with source/mtime |
| 3.7 | 3 | memory prompts | `agents/default/system.md`, `memory/recall.py` | done | `tests/core/test_memory_phase_bcd.py`, prompt audit | memory is background/stale reference |
| 3.8 | 3 | skill frontmatter | `skill/__init__.py`, `tools/skill/` | done | `tests/core/test_skill.py` | adopts name/description/type/scope; ignores non-Pythinker fields |
| 3.9 | 3 | `plugins/**` | `skill/__init__.py`, `plugin/` | done | ledger audit | plugins expose tools/config/skill roots; marketplace/output styles skipped |
| 4.1 | 4 | `services/mcp/client.ts` | `tools/mcp_resource/` | done | `tests/tools/test_mcp_resource.py` | resources + prompts listed; reads untrusted |
| 4.2 | 4 | MCP live refresh | `cli/mcp.py`, `soul/toolset.py` | todo | MCP tests | reconnect/list_changed — see tasks/todo.md |
| 4.3 | 4 | MCP docker stdio | `soul/toolset.py`, `cli/mcp.py` | done | `tests/core/test_mcp_docker_rm.py`, `tests/core/test_mcp_cleanup.py`, `tests/tools/test_mcp_startup_timeout.py` | --rm on add + config load via prepare_mcp_config_dict |
| 4.4 | 4 | MCP prompts | `tools/mcp_resource/`, `agents/default/agent.yaml` | done | `tests/tools/test_mcp_resource.py` | InvokeMcpPrompt returns untrusted messages |
| 4.5 | 4 | `elicitationHandler.ts` | `wire/`, `acp/` | future-approved-only | — | Needs Wire contract approval |
| 4.6 | 4 | MCP naming | `utils/mcp_names.py`, `soul/toolset.py`, `cli/mcp.py` | done | `tests/core/test_mcp_name_normalization.py` | Server key normalization + collision errors at load |
| 4.7 | 4 | MCP OAuth | `soul/toolset.py`, `cli/mcp.py` | done | `tests/tools/test_mcp_startup_timeout.py` | OAuth servers skip unauthorized with auth hint; hosted/XAA skipped |
| 5.1 | 5 | plan mode | `soul/permission.py`, subagents | verify-existing | `tests/core/test_permission_profiles.py` | profile downgrade + shell denial; MCP/plugin child E2E thin |
| 5.2 | 5 | subagent usage | `subagents/`, `tools/agent/` | verify-existing | `tests/subagents/test_usage_rollup.py` | roll-up helpers wired; resume/batch double-count tests missing |
| 5.3 | 5 | plan tool | `tools/plan/`, `soul/dynamic_injections/plan_mode.py` | done | `tests/tools/test_tool_descriptions.py`, `tests/core/test_plan_mode_injection_provider.py` | written plans must include verification |
| 5.4 | 5 | `TodoWriteTool` | `tools/todo/` | done | `tests/tools/test_todo.py` | cancelled status persists and renders distinctly |
| 5.5 | 5 | progress UI | `tools/progress/` | done | `tests/tools/test_progress.py` | ProgressNote producer exists; description anti-spam |
| 5.6 | 5 | suggestions | `tools/suggest/`, `ui/shell/prompt.py` | done | `tests/tools/test_suggest.py` | emit/render + Alt+S accept→prefill |
| 5.7 | 5 | ACP questions | `acp/`, `tools/ask_user/` | done | `tests/acp/test_session_question.py` | unsupported ACP clients get unsupported/fallback semantics |
| 5.8 | 5 | `schemas/hooks.ts` | `hooks/events.py` | verify-existing | `tests/e2e/test_hooks_wire_e2e.py`, `tests/tools/test_agent_tool.py` | supported lifecycle events include PostCompact/SessionEnd/SubagentStart/SubagentStop/Notification; prompt/HTTP hooks skipped |
| 5.9 | 5 | markdown agents | `subagents/discovery.py` | verify-existing | `tests/core/test_subagent_discovery.py` | max_turns/steps + disallowed_tools/exclude_tools mapped |
| 5.10 | 5 | `processUserInput/` | `ui/shell/` | verify-existing | `tests/ui_and_conv/test_shell_slash_commands.py`, `tests/utils/test_slash_command.py` | native slash and shell-mode routing covered |
| 5.11 | 5 | `keybindings/` | `ui/shell/keymap.py` | todo | shell tests | Keybinding parity |
| 5.12 | 5 | REPL tips | `ui/shell/` | future-approved-only | — | static tips require UX approval; no analytics |
| 5.13 | 5 | suggestions fork | `tools/suggest/` | done | `tests/tools/test_suggest.py` | uses Suggestion tool/event; speculation fork skipped |
| 6.1 | 6 | telemetry tree | `telemetry/` | done | `tests/core/test_otel_span_tree.py`, `tests/telemetry/test_telemetry.py`, `tests/telemetry/test_otel_resource.py` | connected spans, GenAI attribute plumbing, telemetry-off no-op |
| 6.2 | 6 | `services/vcr.ts` | `tests_e2e/` | future-approved-only | — | requires explicit cassette/redaction design |
| 6.3 | 6 | eval harness | `tests_ai/eval_gate.py`, `tests_e2e/eval_schema.py` | verify-existing | `tests/test_eval_harness_wiring.py`, `tests_e2e/test_eval_schema.py` | Offline schema + report budget gate; Harbor live metrics deferred |
| 6.4 | 6 | failure thresholds | `soul/pythinkersoul.py`, `config.py` | done | `tests/core/test_pythinkersoul_stuck_loop.py` | stuck/failure-threshold handoff with reset behavior |
| 6.5 | 6 | max steps | `soul/pythinkersoul.py`, `soul/btw.py` | verify-existing | `tests/core/test_max_steps_handoff.py`, `tests/core/test_pythinkersoul_stuck_loop.py` | shell/print handoff done; wire/ACP still status-only |
| 6.6 | 6 | telemetry sanitize | `telemetry/names.py`, `soul/toolset.py` | done | `tests/telemetry/test_tool_name_sanitize.py` | Span/metric labels sanitized; runtime tool names unchanged |
| 6.7 | 6 | VCR fixtures | `tests_e2e/` | future-approved-only | — | depends on Task 6.2 cassette design |
| 7.1 | 7 | model defense | `soul/dynamic_injections/model_defense.py` | verify-existing | injection tests | Model-keyed defense |
| 7.2 | 7 | `screens/REPL.tsx` | `ui/shell/` | todo | shell tests | Shell UI surfaces |
| 7.3 | 7 | Ink engine | `ui/shell/` | skipped | — | De-scope: no Pi-TUI replacement |
| 7.4 | 7 | `native-ts/file-index/` | `ui/shell/prompt.py` | todo | prompt tests | File mention polish |
| 7.5 | 7 | media limits | `utils/media_limits.py`, `tools/file/read_media.py` | done | `tests/utils/test_media_limits.py` | Per-kind byte caps + image pixel ceiling |
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
| Blackbox skill-only frontmatter (`allowed-tools`, `disable-model-invocation`, hooks/context/path/shell metadata) | skip | Pythinker skill loader intentionally keeps skills as instructional resources; agent/tool execution fields live in agent specs, hooks, and config |
| Plugin marketplace, plugin agents, plugin MCP expansion, plugin output styles | skip | Current Pythinker plugin scope is local tools/config plus skill-root discovery; expansion needs product approval |

## Phase Exit Gates

| phase | gate | status |
| ---: | --- | --- |
| 0 | Ledger complete; maintainers see remain/skip | done |
| 1 | `make check-pythinker-code && make test-pythinker-code` | verify-existing done; full suite has 7 pre-existing UI failures on branch |
| 2 | focused + check; full test if shared context changed | pending |
| 3–8 | per plan dashboard | pending |
