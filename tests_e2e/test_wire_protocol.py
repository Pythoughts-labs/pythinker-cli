from __future__ import annotations

import json
from typing import Any

from inline_snapshot import snapshot

from tests_e2e.wire_helpers import (
    collect_until_response,
    make_home_dir,
    make_work_dir,
    normalize_response,
    send_initialize,
    start_wire,
    summarize_messages,
    write_scripted_config,
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def test_initialize_handshake(tmp_path) -> None:
    config_path = write_scripted_config(tmp_path, ["text: hello"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        resp = send_initialize(wire)
        result = _as_dict(resp.get("result"))
        assert result.get("protocol_version") == "1.9"
        assert "slash_commands" in result
        assert normalize_response(resp) == snapshot(
            {
                "result": {
                    "protocol_version": "1.9",
                    "server": {"name": "Pythinker CLI", "version": "<VERSION>"},
                    "slash_commands": [
                        {
                            "name": "init",
                            "description": "Analyze the codebase and generate an `AGENTS.md` file",
                            "aliases": [],
                        },
                        {
                            "name": "recap",
                            "description": "Recap Pythinker sessions. Usage: /recap [on|off|today|yesterday|week|YYYY-MM-DD]",
                            "aliases": [],
                        },
                        {
                            "name": "compact",
                            "description": "Compact the context (optionally with a custom focus, e.g. /compact keep db discussions)",
                            "aliases": [],
                        },
                        {"name": "clear", "description": "Clear the context", "aliases": ["reset"]},
                        {
                            "name": "yolo",
                            "description": "Toggle YOLO mode (auto-approve all actions)",
                            "aliases": [],
                        },
                        {
                            "name": "auto",
                            "description": "Toggle auto mode (no user present: auto-dismiss AskUserQuestion, auto-approve tool calls)",
                            "aliases": [],
                        },
                        {
                            "name": "accept-edits",
                            "description": "Toggle accept-edits mode (auto-approve reversible in-workspace file edits)",
                            "aliases": [],
                        },
                        {
                            "name": "plan",
                            "description": "Toggle plan mode. Usage: /plan [on|off|view|clear]",
                            "aliases": [],
                        },
                        {
                            "name": "goal",
                            "description": "Set a thread goal pursued across turns until verified. Usage: /goal <objective> | view | pause | resume | clear",
                            "aliases": [],
                        },
                        {
                            "name": "learn",
                            "description": "Extract reusable lessons from this session and save them to project memory",
                            "aliases": [],
                        },
                        {
                            "name": "best-practices",
                            "description": "Inject engineering best practices (code changes, testing, todos, debugging) into context",
                            "aliases": ["bp"],
                        },
                        {
                            "name": "add-dir",
                            "description": "Add a directory to the workspace. Usage: /add-dir <path>. Run without args to list added dirs",
                            "aliases": [],
                        },
                        {
                            "name": "export",
                            "description": "Export current session context to a markdown file",
                            "aliases": [],
                        },
                        {
                            "name": "import",
                            "description": "Import context from a file or session ID",
                            "aliases": [],
                        },
                        {
                            "name": "skill:agent-creator",
                            "description": 'Author a new project-specific Pythinker subagent (a specialist like "migration-reviewer" or "api-contract-checker") with a correct spec, a persona-rich system prompt, and a structured output contract. Use when the user wants to create, scaffold, or design a custom agent / subagent, or asks how Pythinker agent YAML / markdown agent files, tool scoping, or the extend-inheritance schema work.',
                            "aliases": [],
                        },
                        {
                            "name": "skill:check-impl-against-spec",
                            "description": "Compare an implementation against a product or technical spec and report gaps with evidence.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:create-pr",
                            "description": "Prepare a pull request by summarizing changes, verification, risks, and reviewer guidance without adding AI footers.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:customize-pythinker",
                            "description": "Edit Pythinker's own configuration — agent YAML specs and extend-inheritance, the permission profiles that gate tools, plugin.json, and hook lifecycle events. Use ONLY when the user wants to configure, customize, or extend Pythinker itself (its agents, permissions, plugins, or hooks). For authoring a new agent use agent-creator; for authoring a skill use skill-creator; for general usage Q&A use pythinker-code-help.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:designer-skill",
                            "description": "Prescriptive frontend design guidance via the designer-skill MCP server. Use when the user asks to use designer-skill, improve UI/UX, run the anti-slop ship gate, apply a design system, or enhance pages/components with MCP-backed design references — especially for Pythinker docs and marketing surfaces with DESIGN.md/PRODUCT.md.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:diagnose-ci-failures",
                            "description": "Diagnose failing CI, lint, typecheck, build, or test logs and propose or implement the smallest verified fix.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:fix-errors",
                            "description": "Fix concrete errors from logs, failing commands, exceptions, or diagnostics with root-cause-first discipline.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:implement-specs",
                            "description": "Implement one or more checked-in specs using scout-plan-implement-verify workflow.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:pr-walkthrough",
                            "description": "Produce a concise reviewer-friendly walkthrough of a PR or diff, including changed areas, behavior, tests, and risks.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:pythinker-code-help",
                            "description": "Answer Pythinker CLI usage, configuration, and troubleshooting questions. Use when user asks about Pythinker CLI installation, setup, configuration, slash commands, keyboard shortcuts, MCP integration, providers, environment variables, how something works internally, or any questions about Pythinker CLI itself.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:reproduce-bug-report",
                            "description": "Reproduce a bug report with evidence-first investigation, bounded variants, and a clear repro/non-repro verdict.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:resolve-merge-conflicts",
                            "description": "Resolve git merge/rebase conflicts safely by preserving both sides' intent and validating the result.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:review-pr",
                            "description": "Review a pull request or working-tree diff with severity-scored, evidence-backed findings.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:skill-creator",
                            "description": "Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an existing skill) that extends Pythinker's capabilities with specialized knowledge, workflows, or tool integrations.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:spec-driven-implementation",
                            "description": "Implement a feature or fix from product/technical specs while checking the final code against the stated requirements.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:write-product-spec",
                            "description": "Draft a product spec from a user request, issue, or bug report with goals, non-goals, requirements, and acceptance criteria.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:write-tech-spec",
                            "description": "Draft a technical implementation spec from product requirements and codebase evidence.",
                            "aliases": [],
                        },
                    ],
                    "hooks": {
                        "supported_events": [
                            "PreToolUse",
                            "PostToolUse",
                            "PostToolUseFailure",
                            "UserPromptSubmit",
                            "Stop",
                            "StopFailure",
                            "SessionStart",
                            "SessionEnd",
                            "SubagentStart",
                            "SubagentStop",
                            "PreCompact",
                            "PostCompact",
                            "Notification",
                        ],
                        "configured": {},
                    },
                    "capabilities": {"supports_question": True},
                }
            }
        )
    finally:
        wire.close()


def test_initialize_external_tool_conflict(tmp_path) -> None:
    config_path = write_scripted_config(tmp_path, ["text: hello"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    external_tools = [
        {
            "name": "Shell",
            "description": "Conflicts with built-in",
            "parameters": {"type": "object", "properties": {}},
        }
    ]

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        resp = send_initialize(wire, external_tools=external_tools)
        result = _as_dict(resp.get("result"))
        external_tools_result = _as_dict(result.get("external_tools"))
        rejected = external_tools_result.get("rejected")
        assert isinstance(rejected, list)
        assert any(isinstance(item, dict) and item.get("name") == "Shell" for item in rejected)
        assert normalize_response(resp) == snapshot(
            {
                "result": {
                    "protocol_version": "1.9",
                    "server": {"name": "Pythinker CLI", "version": "<VERSION>"},
                    "slash_commands": [
                        {
                            "name": "init",
                            "description": "Analyze the codebase and generate an `AGENTS.md` file",
                            "aliases": [],
                        },
                        {
                            "name": "recap",
                            "description": "Recap Pythinker sessions. Usage: /recap [on|off|today|yesterday|week|YYYY-MM-DD]",
                            "aliases": [],
                        },
                        {
                            "name": "compact",
                            "description": "Compact the context (optionally with a custom focus, e.g. /compact keep db discussions)",
                            "aliases": [],
                        },
                        {"name": "clear", "description": "Clear the context", "aliases": ["reset"]},
                        {
                            "name": "yolo",
                            "description": "Toggle YOLO mode (auto-approve all actions)",
                            "aliases": [],
                        },
                        {
                            "name": "auto",
                            "description": "Toggle auto mode (no user present: auto-dismiss AskUserQuestion, auto-approve tool calls)",
                            "aliases": [],
                        },
                        {
                            "name": "accept-edits",
                            "description": "Toggle accept-edits mode (auto-approve reversible in-workspace file edits)",
                            "aliases": [],
                        },
                        {
                            "name": "plan",
                            "description": "Toggle plan mode. Usage: /plan [on|off|view|clear]",
                            "aliases": [],
                        },
                        {
                            "name": "goal",
                            "description": "Set a thread goal pursued across turns until verified. Usage: /goal <objective> | view | pause | resume | clear",
                            "aliases": [],
                        },
                        {
                            "name": "learn",
                            "description": "Extract reusable lessons from this session and save them to project memory",
                            "aliases": [],
                        },
                        {
                            "name": "best-practices",
                            "description": "Inject engineering best practices (code changes, testing, todos, debugging) into context",
                            "aliases": ["bp"],
                        },
                        {
                            "name": "add-dir",
                            "description": "Add a directory to the workspace. Usage: /add-dir <path>. Run without args to list added dirs",
                            "aliases": [],
                        },
                        {
                            "name": "export",
                            "description": "Export current session context to a markdown file",
                            "aliases": [],
                        },
                        {
                            "name": "import",
                            "description": "Import context from a file or session ID",
                            "aliases": [],
                        },
                        {
                            "name": "skill:agent-creator",
                            "description": 'Author a new project-specific Pythinker subagent (a specialist like "migration-reviewer" or "api-contract-checker") with a correct spec, a persona-rich system prompt, and a structured output contract. Use when the user wants to create, scaffold, or design a custom agent / subagent, or asks how Pythinker agent YAML / markdown agent files, tool scoping, or the extend-inheritance schema work.',
                            "aliases": [],
                        },
                        {
                            "name": "skill:check-impl-against-spec",
                            "description": "Compare an implementation against a product or technical spec and report gaps with evidence.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:create-pr",
                            "description": "Prepare a pull request by summarizing changes, verification, risks, and reviewer guidance without adding AI footers.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:customize-pythinker",
                            "description": "Edit Pythinker's own configuration — agent YAML specs and extend-inheritance, the permission profiles that gate tools, plugin.json, and hook lifecycle events. Use ONLY when the user wants to configure, customize, or extend Pythinker itself (its agents, permissions, plugins, or hooks). For authoring a new agent use agent-creator; for authoring a skill use skill-creator; for general usage Q&A use pythinker-code-help.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:designer-skill",
                            "description": "Prescriptive frontend design guidance via the designer-skill MCP server. Use when the user asks to use designer-skill, improve UI/UX, run the anti-slop ship gate, apply a design system, or enhance pages/components with MCP-backed design references — especially for Pythinker docs and marketing surfaces with DESIGN.md/PRODUCT.md.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:diagnose-ci-failures",
                            "description": "Diagnose failing CI, lint, typecheck, build, or test logs and propose or implement the smallest verified fix.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:fix-errors",
                            "description": "Fix concrete errors from logs, failing commands, exceptions, or diagnostics with root-cause-first discipline.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:implement-specs",
                            "description": "Implement one or more checked-in specs using scout-plan-implement-verify workflow.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:pr-walkthrough",
                            "description": "Produce a concise reviewer-friendly walkthrough of a PR or diff, including changed areas, behavior, tests, and risks.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:pythinker-code-help",
                            "description": "Answer Pythinker CLI usage, configuration, and troubleshooting questions. Use when user asks about Pythinker CLI installation, setup, configuration, slash commands, keyboard shortcuts, MCP integration, providers, environment variables, how something works internally, or any questions about Pythinker CLI itself.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:reproduce-bug-report",
                            "description": "Reproduce a bug report with evidence-first investigation, bounded variants, and a clear repro/non-repro verdict.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:resolve-merge-conflicts",
                            "description": "Resolve git merge/rebase conflicts safely by preserving both sides' intent and validating the result.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:review-pr",
                            "description": "Review a pull request or working-tree diff with severity-scored, evidence-backed findings.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:skill-creator",
                            "description": "Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an existing skill) that extends Pythinker's capabilities with specialized knowledge, workflows, or tool integrations.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:spec-driven-implementation",
                            "description": "Implement a feature or fix from product/technical specs while checking the final code against the stated requirements.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:write-product-spec",
                            "description": "Draft a product spec from a user request, issue, or bug report with goals, non-goals, requirements, and acceptance criteria.",
                            "aliases": [],
                        },
                        {
                            "name": "skill:write-tech-spec",
                            "description": "Draft a technical implementation spec from product requirements and codebase evidence.",
                            "aliases": [],
                        },
                    ],
                    "external_tools": {
                        "accepted": [],
                        "rejected": [{"name": "Shell", "reason": "conflicts with builtin tool"}],
                    },
                    "hooks": {
                        "supported_events": [
                            "PreToolUse",
                            "PostToolUse",
                            "PostToolUseFailure",
                            "UserPromptSubmit",
                            "Stop",
                            "StopFailure",
                            "SessionStart",
                            "SessionEnd",
                            "SubagentStart",
                            "SubagentStop",
                            "PreCompact",
                            "PostCompact",
                            "Notification",
                        ],
                        "configured": {},
                    },
                    "capabilities": {"supports_question": True},
                }
            }
        )
    finally:
        wire.close()


def test_external_tool_call(tmp_path) -> None:
    tool_args = json.dumps({"path": "README.md"})
    tool_call = json.dumps({"id": "tc-1", "name": "ext_tool", "arguments": tool_args})
    scripts = [
        "\n".join(
            [
                "text: calling external tool",
                f"tool_call: {tool_call}",
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    external_tools = [
        {
            "name": "ext_tool",
            "description": "External tool",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        send_initialize(wire, external_tools=external_tools)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run external tool"},
            }
        )

        def handle_request(msg: dict[str, Any]) -> dict[str, Any]:
            params = msg.get("params")
            payload = params.get("payload") if isinstance(params, dict) else None
            tool_call_id = payload.get("id") if isinstance(payload, dict) else None
            assert isinstance(tool_call_id, str)
            return {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "result": {
                    "tool_call_id": tool_call_id,
                    "return_value": {
                        "is_error": False,
                        "output": "Opened",
                        "message": "Opened README.md",
                        "display": [],
                    },
                },
            }

        resp, messages = collect_until_response(wire, "prompt-1", request_handler=handle_request)
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {
                    "method": "event",
                    "type": "TurnBegin",
                    "payload": {"user_input": "run external tool"},
                },
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "calling external tool"},
                },
                {
                    "method": "event",
                    "type": "ToolCall",
                    "payload": {
                        "type": "function",
                        "id": "tc-1",
                        "function": {"name": "ext_tool", "arguments": '{"path": "README.md"}'},
                        "extras": None,
                    },
                },
                {
                    "method": "event",
                    "type": "StatusUpdate",
                    "payload": {
                        "context_usage": None,
                        "context_tokens": None,
                        "max_context_tokens": None,
                        "token_usage": None,
                        "message_id": None,
                        "model_name": "scripted_echo",
                        "provider_key": "scripted_provider",
                        "plan_mode": False,
                        "mcp_status": None,
                    },
                },
                {
                    "method": "request",
                    "type": "ToolCallRequest",
                    "payload": {
                        "id": "tc-1",
                        "name": "ext_tool",
                        "arguments": '{"path": "README.md"}',
                    },
                },
                {
                    "method": "event",
                    "type": "ToolExecutionStarted",
                    "payload": {"tool_call_id": "tc-1"},
                },
                {
                    "method": "event",
                    "type": "ToolResult",
                    "payload": {
                        "tool_call_id": "tc-1",
                        "return_value": {
                            "is_error": False,
                            "output": "Opened",
                            "message": "Opened README.md",
                            "display": [],
                            "extras": None,
                        },
                    },
                },
                {
                    "method": "event",
                    "type": "AgentListDelta",
                    "payload": {
                        "items": [
                            "- `code-reviewer`: Diff-focused code review with severity-scored findings. (Tools: Shell, SetTodoList, ReadFile, Glob, Grep, ReadSkill). When to use: Use to run a read-only, diff-focused, professional code review — severity-scored findings across correctness, security, reliability, performance, maintainability, and standards compliance, in any programming language — or a code-reviewr-derived PR artifact workflow on the current branch. It runs offline by design and never modifies the repository; third-party API claims it cannot verify from the repository come back under RISKS as needs-verification items for the parent to check. For diffs above roughly 1,500 changed lines or 25 files, dispatch one instance per subsystem with an explicit file list and synthesize, instead of one instance for the whole diff.",
                            "- `coder`: Good at general software engineering tasks. (Tools: Shell, SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, WriteFile, StrReplaceFile, ReadSkill, SearchWeb, FetchURL, mcp__context7__resolve-library-id, mcp__context7__query-docs). When to use: Use this agent for non-trivial software engineering work that may require reading files, editing code, running commands, and returning a compact but technically complete summary to the parent agent. It delivers production-ready, idiomatic, verified changes in any language the project uses, with current-docs verification for third-party APIs, and never expands beyond its brief.",
                            "- `debugger`: Failure/log/stack-trace root-cause analysis with reproduction evidence. (Tools: Shell, SetTodoList, ReadFile, Glob, Grep, SmartSearch). When to use: Use for failing tests, stack traces, runtime errors, flaky failures, regressions, or debugging requests where the root cause should be found before editing code. Read-only and safe to fan out in parallel — one focused failure per instance — it returns the named mechanism, confidence, evidence, the recommended minimal fix, and the verification that would prove it.",
                            '- `explore`: Fast codebase exploration with prompt-enforced read-only behavior. (Tools: Shell, SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, ReadSkill). When to use: Fast agent specialized for exploring codebases. Use this when you need to quickly find files by patterns (e.g. "src/**/*.yaml"), search code for keywords (e.g. "database connection"), or answer questions about the codebase (e.g. "how does the auth module work?"). When calling this agent, specify the desired thoroughness level: "quick" for basic searches, "medium" for moderate exploration, or "thorough" for comprehensive analysis across multiple locations and naming conventions. Use this agent for any read-only exploration that will clearly require more than 3 tool calls. Prefer launching multiple explore agents concurrently when investigating independent questions. Absence claims come with the searches that back them.',
                            "- `implementer`: Scoped implementation with minimal edits and verification. (Tools: Shell, SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, WriteFile, StrReplaceFile, ReadSkill, SearchWeb, FetchURL, mcp__context7__resolve-library-id, mcp__context7__query-docs). When to use: Use this agent when the required code change is already specified and should be implemented with minimal, idiomatic edits and a quick verification pass. It executes the spec faithfully — escalating instead of improvising when the spec does not match reality — and emits a <coding_artifact> block so the result can be chained directly into the verifier.",
                            "- `judge`: Independent final quality gate for answers, reports, and code-change summaries. (Tools: Shell, SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, ReadSkill). When to use: Use this agent as an independent final quality gate and advisor before delivering non-trivial code changes, reports, audits, or findings to the user. It judges the parent agent's evidence, actions, and proposed final answer — verifying claims against the packet's artifacts and local sources, and requiring the parent's citation for load-bearing external-API, version, and best-practice claims it cannot check offline — and recommends fixes without ever applying them.",
                            "- `plan`: Read-only implementation planning and architecture design. (Tools: SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, ReadSkill, SearchWeb, FetchURL). When to use: Use this agent when the parent agent needs a step-by-step implementation plan, key file identification, and architectural trade-off analysis before code changes are made. It returns dependency-ordered, wave-parallelized tasks — each with artifacts, acceptance criteria, a specialist recommendation, and a proving verification — grounded in repository evidence and current third-party documentation.",
                            "- `planner`: Read-only recon planner that decomposes tasks into distinct parallel seeds. (Tools: Shell, ReadFile, Glob, Grep, SmartSearch). When to use: Use this agent before spawning N parallel workers on a large or open-ended task. It scouts the repository cheaply, partitions the problem space along one decomposition axis, and returns distinct, self-contained seeds so workers start from non-overlapping vantage points. A single-seed result signals the task is not worth parallelizing.",
                            "- `review`: Read-only code review with severity-scored findings. (Tools: Shell, SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, ReadSkill). When to use: Use this agent for direct, read-only code review after changes are made, or when the parent needs severity-scored findings before deciding what to fix. It reviews the diff/files itself with reads and searches — for the CLI/Reviewflow-driven review pipeline, use `code-reviewer` instead. Findings arrive BLOCKER-first with evidence, trigger conditions, and a dispatch-ready fix description; it runs offline by design, so third-party API claims it cannot verify from the repository are explicitly downgraded to needs-verification items for the parent to check. For diffs above roughly 1,500 changed lines or 25 files, dispatch one instance per subsystem with an explicit file list and synthesize, instead of one instance for the whole diff.",
                            "- `scout`: Read-only external docs, dependency-source, and API freshness researcher. (Tools: Shell, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, ReadSkill, SearchWeb, FetchURL). When to use: Use this agent for external libraries, SDK docs, upstream source comparisons, API freshness checks, registry/package verification, and dependency behavior research — including verifying the `needs verification` third-party claims that offline reviewer/debugger agents return under RISKS. It returns version-pinned, source-cited facts — local installed source first, then official docs via live web research — with conflicts and unverifiable gaps reported explicitly instead of papered over.",
                            "- `security-reviewer`: Diff-focused security review with validated findings. (Tools: Shell, SetTodoList, ReadFile, Glob, Grep). When to use: Use for security review: diff-only review on the current branch (default) or repo-wide vulnerability discovery via the security-scan pipeline. Can run in parallel with `code-reviewer`; for large diffs, scope each instance to the trust-boundary files of one subsystem. Returns reachability-validated findings — source → sink anchored, precondition-stated, CWE-classified, version-checked against the project's pins — with scanner hits treated as leads until verified. It runs offline by design, so advisory-dependent claims come back under RISKS as needs-verification items for the parent to check.",
                            '- `verifier`: Read-only validation runner for tests, lint, and builds. (Tools: Shell, SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, ReadSkill). When to use: Use this agent when the parent needs tests, lint, type checks, builds, or other validation gates run and reported without applying fixes — e.g. "run the tests", "does it build", post-edit gate checks, or re-running a suspected flaky suite. Not for fixing failures, writing tests, updating snapshots, or formatting: it is read-only by design and reports proposed fixes under RISKS instead of applying them.',
                        ],
                        "complete": True,
                    },
                },
                {"method": "event", "type": "StepBegin", "payload": {"n": 2}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "done"},
                },
                {
                    "method": "event",
                    "type": "StatusUpdate",
                    "payload": {
                        "context_usage": None,
                        "context_tokens": None,
                        "max_context_tokens": None,
                        "token_usage": None,
                        "message_id": None,
                        "model_name": "scripted_echo",
                        "provider_key": "scripted_provider",
                        "plan_mode": False,
                        "mcp_status": None,
                    },
                },
                {"method": "event", "type": "TurnEnd", "payload": {}},
            ]
        )
    finally:
        wire.close()


def test_prompt_without_initialize(tmp_path) -> None:
    config_path = write_scripted_config(tmp_path, ["text: hello without init"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
    )
    try:
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "hi"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {"method": "event", "type": "TurnBegin", "payload": {"user_input": "hi"}},
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "hello without init"},
                },
                {
                    "method": "event",
                    "type": "StatusUpdate",
                    "payload": {
                        "context_usage": None,
                        "context_tokens": None,
                        "max_context_tokens": None,
                        "token_usage": None,
                        "message_id": None,
                        "model_name": "scripted_echo",
                        "provider_key": "scripted_provider",
                        "plan_mode": False,
                        "mcp_status": None,
                    },
                },
                {
                    "method": "event",
                    "type": "AgentListDelta",
                    "payload": {
                        "items": [
                            "- `code-reviewer`: Diff-focused code review with severity-scored findings. (Tools: Shell, SetTodoList, ReadFile, Glob, Grep, ReadSkill). When to use: Use to run a read-only, diff-focused, professional code review — severity-scored findings across correctness, security, reliability, performance, maintainability, and standards compliance, in any programming language — or a code-reviewr-derived PR artifact workflow on the current branch. It runs offline by design and never modifies the repository; third-party API claims it cannot verify from the repository come back under RISKS as needs-verification items for the parent to check. For diffs above roughly 1,500 changed lines or 25 files, dispatch one instance per subsystem with an explicit file list and synthesize, instead of one instance for the whole diff.",
                            "- `coder`: Good at general software engineering tasks. (Tools: Shell, SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, WriteFile, StrReplaceFile, ReadSkill, SearchWeb, FetchURL, mcp__context7__resolve-library-id, mcp__context7__query-docs). When to use: Use this agent for non-trivial software engineering work that may require reading files, editing code, running commands, and returning a compact but technically complete summary to the parent agent. It delivers production-ready, idiomatic, verified changes in any language the project uses, with current-docs verification for third-party APIs, and never expands beyond its brief.",
                            "- `debugger`: Failure/log/stack-trace root-cause analysis with reproduction evidence. (Tools: Shell, SetTodoList, ReadFile, Glob, Grep, SmartSearch). When to use: Use for failing tests, stack traces, runtime errors, flaky failures, regressions, or debugging requests where the root cause should be found before editing code. Read-only and safe to fan out in parallel — one focused failure per instance — it returns the named mechanism, confidence, evidence, the recommended minimal fix, and the verification that would prove it.",
                            '- `explore`: Fast codebase exploration with prompt-enforced read-only behavior. (Tools: Shell, SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, ReadSkill). When to use: Fast agent specialized for exploring codebases. Use this when you need to quickly find files by patterns (e.g. "src/**/*.yaml"), search code for keywords (e.g. "database connection"), or answer questions about the codebase (e.g. "how does the auth module work?"). When calling this agent, specify the desired thoroughness level: "quick" for basic searches, "medium" for moderate exploration, or "thorough" for comprehensive analysis across multiple locations and naming conventions. Use this agent for any read-only exploration that will clearly require more than 3 tool calls. Prefer launching multiple explore agents concurrently when investigating independent questions. Absence claims come with the searches that back them.',
                            "- `implementer`: Scoped implementation with minimal edits and verification. (Tools: Shell, SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, WriteFile, StrReplaceFile, ReadSkill, SearchWeb, FetchURL, mcp__context7__resolve-library-id, mcp__context7__query-docs). When to use: Use this agent when the required code change is already specified and should be implemented with minimal, idiomatic edits and a quick verification pass. It executes the spec faithfully — escalating instead of improvising when the spec does not match reality — and emits a <coding_artifact> block so the result can be chained directly into the verifier.",
                            "- `judge`: Independent final quality gate for answers, reports, and code-change summaries. (Tools: Shell, SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, ReadSkill). When to use: Use this agent as an independent final quality gate and advisor before delivering non-trivial code changes, reports, audits, or findings to the user. It judges the parent agent's evidence, actions, and proposed final answer — verifying claims against the packet's artifacts and local sources, and requiring the parent's citation for load-bearing external-API, version, and best-practice claims it cannot check offline — and recommends fixes without ever applying them.",
                            "- `plan`: Read-only implementation planning and architecture design. (Tools: SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, ReadSkill, SearchWeb, FetchURL). When to use: Use this agent when the parent agent needs a step-by-step implementation plan, key file identification, and architectural trade-off analysis before code changes are made. It returns dependency-ordered, wave-parallelized tasks — each with artifacts, acceptance criteria, a specialist recommendation, and a proving verification — grounded in repository evidence and current third-party documentation.",
                            "- `planner`: Read-only recon planner that decomposes tasks into distinct parallel seeds. (Tools: Shell, ReadFile, Glob, Grep, SmartSearch). When to use: Use this agent before spawning N parallel workers on a large or open-ended task. It scouts the repository cheaply, partitions the problem space along one decomposition axis, and returns distinct, self-contained seeds so workers start from non-overlapping vantage points. A single-seed result signals the task is not worth parallelizing.",
                            "- `review`: Read-only code review with severity-scored findings. (Tools: Shell, SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, ReadSkill). When to use: Use this agent for direct, read-only code review after changes are made, or when the parent needs severity-scored findings before deciding what to fix. It reviews the diff/files itself with reads and searches — for the CLI/Reviewflow-driven review pipeline, use `code-reviewer` instead. Findings arrive BLOCKER-first with evidence, trigger conditions, and a dispatch-ready fix description; it runs offline by design, so third-party API claims it cannot verify from the repository are explicitly downgraded to needs-verification items for the parent to check. For diffs above roughly 1,500 changed lines or 25 files, dispatch one instance per subsystem with an explicit file list and synthesize, instead of one instance for the whole diff.",
                            "- `scout`: Read-only external docs, dependency-source, and API freshness researcher. (Tools: Shell, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, ReadSkill, SearchWeb, FetchURL). When to use: Use this agent for external libraries, SDK docs, upstream source comparisons, API freshness checks, registry/package verification, and dependency behavior research — including verifying the `needs verification` third-party claims that offline reviewer/debugger agents return under RISKS. It returns version-pinned, source-cited facts — local installed source first, then official docs via live web research — with conflicts and unverifiable gaps reported explicitly instead of papered over.",
                            "- `security-reviewer`: Diff-focused security review with validated findings. (Tools: Shell, SetTodoList, ReadFile, Glob, Grep). When to use: Use for security review: diff-only review on the current branch (default) or repo-wide vulnerability discovery via the security-scan pipeline. Can run in parallel with `code-reviewer`; for large diffs, scope each instance to the trust-boundary files of one subsystem. Returns reachability-validated findings — source → sink anchored, precondition-stated, CWE-classified, version-checked against the project's pins — with scanner hits treated as leads until verified. It runs offline by design, so advisory-dependent claims come back under RISKS as needs-verification items for the parent to check.",
                            '- `verifier`: Read-only validation runner for tests, lint, and builds. (Tools: Shell, SetTodoList, ReadFile, ReadMediaFile, Glob, Grep, SmartSearch, ReadSkill). When to use: Use this agent when the parent needs tests, lint, type checks, builds, or other validation gates run and reported without applying fixes — e.g. "run the tests", "does it build", post-edit gate checks, or re-running a suspected flaky suite. Not for fixing failures, writing tests, updating snapshots, or formatting: it is read-only by design and reports proposed fixes under RISKS instead of applying them.',
                        ],
                        "complete": True,
                    },
                },
                {"method": "event", "type": "TurnEnd", "payload": {}},
            ]
        )
    finally:
        wire.close()
