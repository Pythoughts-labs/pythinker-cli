from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from inline_snapshot import snapshot

from .wire_helpers import (
    build_approval_response,
    build_set_todo_call,
    build_shell_tool_call,
    collect_until_response,
    make_home_dir,
    make_work_dir,
    normalize_value,
    send_initialize,
    start_wire,
    summarize_messages,
    write_scripted_config,
)


def _extract_request_payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
    for msg in messages:
        if msg.get("method") != "request":
            continue
        params = msg.get("params")
        if not isinstance(params, dict):
            continue
        payload = params.get("payload")
        if isinstance(payload, dict):
            return payload
    raise AssertionError("Missing request payload")


def _tool_call_line(tool_call_id: str, name: str, args: Mapping[str, Any]) -> str:
    payload = {"id": tool_call_id, "name": name, "arguments": json.dumps(args)}
    return f"tool_call: {json.dumps(payload)}"


def _display_types(payload: dict[str, Any]) -> list[str]:
    display = payload.get("display")
    if not isinstance(display, list):
        return []
    types: list[str] = []
    for item in display:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if isinstance(item_type, str):
            types.append(item_type)
    return types


def test_shell_approval_approve(tmp_path) -> None:
    scripts = [
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-1", "env echo ok"),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=False,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run shell"},
            }
        )
        resp, messages = collect_until_response(
            wire,
            "prompt-1",
            request_handler=lambda msg: build_approval_response(msg, "approve"),
        )
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {"method": "event", "type": "TurnBegin", "payload": {"user_input": "run shell"}},
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "step1"},
                },
                {
                    "method": "event",
                    "type": "ToolCall",
                    "payload": {
                        "type": "function",
                        "id": "tc-1",
                        "function": {"name": "Shell", "arguments": '{"command": "env echo ok"}'},
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
                    "type": "ApprovalRequest",
                    "payload": {
                        "id": "<uuid>",
                        "tool_call_id": "tc-1",
                        "sender": "Shell",
                        "action": "run command",
                        "description": "Run command `env echo ok`",
                        "source_kind": "foreground_turn",
                        "source_id": "<uuid>",
                        "agent_id": None,
                        "subagent_type": None,
                        "source_description": None,
                        "display": [
                            {"type": "shell", "language": "bash", "command": "env echo ok"}
                        ],
                    },
                },
                {
                    "method": "event",
                    "type": "ApprovalResponse",
                    "payload": {"request_id": "<uuid>", "response": "approve", "feedback": ""},
                },
                {
                    "method": "event",
                    "type": "ToolExecutionStarted",
                    "payload": {"tool_call_id": "tc-1"},
                },
                {
                    "method": "event",
                    "type": "ToolOutputPart",
                    "payload": {"tool_call_id": "tc-1", "stream": "stdout", "text": "ok\n"},
                },
                {
                    "method": "event",
                    "type": "ToolResult",
                    "payload": {
                        "tool_call_id": "tc-1",
                        "return_value": {
                            "is_error": False,
                            "output": """\
<untrusted_data id="<NONCE>">
ok

</untrusted_data>\
""",
                            "message": "Command executed successfully.",
                            "display": [],
                            "extras": {"status": "success"},
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


def test_shell_approval_reject(tmp_path) -> None:
    scripts = [
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-1", "env echo ok"),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=False,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run shell"},
            }
        )
        resp, messages = collect_until_response(
            wire,
            "prompt-1",
            request_handler=lambda msg: build_approval_response(msg, "reject"),
        )
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {"method": "event", "type": "TurnBegin", "payload": {"user_input": "run shell"}},
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "step1"},
                },
                {
                    "method": "event",
                    "type": "ToolCall",
                    "payload": {
                        "type": "function",
                        "id": "tc-1",
                        "function": {"name": "Shell", "arguments": '{"command": "env echo ok"}'},
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
                    "type": "ApprovalRequest",
                    "payload": {
                        "id": "<uuid>",
                        "tool_call_id": "tc-1",
                        "sender": "Shell",
                        "action": "run command",
                        "description": "Run command `env echo ok`",
                        "source_kind": "foreground_turn",
                        "source_id": "<uuid>",
                        "agent_id": None,
                        "subagent_type": None,
                        "source_description": None,
                        "display": [
                            {"type": "shell", "language": "bash", "command": "env echo ok"}
                        ],
                    },
                },
                {
                    "method": "event",
                    "type": "ApprovalResponse",
                    "payload": {"request_id": "<uuid>", "response": "reject", "feedback": ""},
                },
                {
                    "method": "event",
                    "type": "ToolResult",
                    "payload": {
                        "tool_call_id": "tc-1",
                        "return_value": {
                            "is_error": True,
                            "output": "",
                            "message": "The tool call is rejected by the user. Stop what you are doing and wait for the user to tell you how to proceed.",
                            "display": [{"type": "brief", "text": "Rejected by user"}],
                            "extras": {"status": "denied"},
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
                {"method": "event", "type": "TurnEnd", "payload": {}},
            ]
        )
    finally:
        wire.close()


def test_approve_for_session(tmp_path) -> None:
    scripts = [
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-1", "env echo first"),
            ]
        ),
        "text: done",
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-2", "env echo second"),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=False,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run shell"},
            }
        )
        resp1, messages1 = collect_until_response(
            wire,
            "prompt-1",
            request_handler=lambda msg: build_approval_response(msg, "approve_for_session"),
        )
        assert resp1.get("result", {}).get("status") == "finished"

        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "run shell again"},
            }
        )
        resp2, messages2 = collect_until_response(wire, "prompt-2")
        assert resp2.get("result", {}).get("status") == "finished"
        assert all(msg.get("method") != "request" for msg in messages2)
        assert summarize_messages(messages1 + messages2) == snapshot(
            [
                {"method": "event", "type": "TurnBegin", "payload": {"user_input": "run shell"}},
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "step1"},
                },
                {
                    "method": "event",
                    "type": "ToolCall",
                    "payload": {
                        "type": "function",
                        "id": "tc-1",
                        "function": {"name": "Shell", "arguments": '{"command": "env echo first"}'},
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
                    "type": "ApprovalRequest",
                    "payload": {
                        "id": "<uuid>",
                        "tool_call_id": "tc-1",
                        "sender": "Shell",
                        "action": "run command",
                        "description": "Run command `env echo first`",
                        "source_kind": "foreground_turn",
                        "source_id": "<uuid>",
                        "agent_id": None,
                        "subagent_type": None,
                        "source_description": None,
                        "display": [
                            {"type": "shell", "language": "bash", "command": "env echo first"}
                        ],
                    },
                },
                {
                    "method": "event",
                    "type": "ApprovalResponse",
                    "payload": {
                        "request_id": "<uuid>",
                        "response": "approve_for_session",
                        "feedback": "",
                    },
                },
                {
                    "method": "event",
                    "type": "ToolExecutionStarted",
                    "payload": {"tool_call_id": "tc-1"},
                },
                {
                    "method": "event",
                    "type": "ToolOutputPart",
                    "payload": {"tool_call_id": "tc-1", "stream": "stdout", "text": "first\n"},
                },
                {
                    "method": "event",
                    "type": "ToolResult",
                    "payload": {
                        "tool_call_id": "tc-1",
                        "return_value": {
                            "is_error": False,
                            "output": """\
<untrusted_data id="<NONCE>">
first

</untrusted_data>\
""",
                            "message": "Command executed successfully.",
                            "display": [],
                            "extras": {"status": "success"},
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
                {
                    "method": "event",
                    "type": "TurnBegin",
                    "payload": {"user_input": "run shell again"},
                },
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "step1"},
                },
                {
                    "method": "event",
                    "type": "ToolCall",
                    "payload": {
                        "type": "function",
                        "id": "tc-2",
                        "function": {
                            "name": "Shell",
                            "arguments": '{"command": "env echo second"}',
                        },
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
                    "method": "event",
                    "type": "ToolExecutionStarted",
                    "payload": {"tool_call_id": "tc-2"},
                },
                {
                    "method": "event",
                    "type": "ToolOutputPart",
                    "payload": {"tool_call_id": "tc-2", "stream": "stdout", "text": "second\n"},
                },
                {
                    "method": "event",
                    "type": "ToolResult",
                    "payload": {
                        "tool_call_id": "tc-2",
                        "return_value": {
                            "is_error": False,
                            "output": """\
<untrusted_data id="<NONCE>">
second

</untrusted_data>\
""",
                            "message": "Command executed successfully.",
                            "display": [],
                            "extras": {"status": "success"},
                        },
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


def test_yolo_skips_approval(tmp_path) -> None:
    scripts = [
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-1", "env echo ok"),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
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
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run shell"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        assert all(msg.get("method") != "request" for msg in messages)
        assert summarize_messages(messages) == snapshot(
            [
                {"method": "event", "type": "TurnBegin", "payload": {"user_input": "run shell"}},
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "step1"},
                },
                {
                    "method": "event",
                    "type": "ToolCall",
                    "payload": {
                        "type": "function",
                        "id": "tc-1",
                        "function": {"name": "Shell", "arguments": '{"command": "env echo ok"}'},
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
                    "method": "event",
                    "type": "ToolExecutionStarted",
                    "payload": {"tool_call_id": "tc-1"},
                },
                {
                    "method": "event",
                    "type": "ToolOutputPart",
                    "payload": {"tool_call_id": "tc-1", "stream": "stdout", "text": "ok\n"},
                },
                {
                    "method": "event",
                    "type": "ToolResult",
                    "payload": {
                        "tool_call_id": "tc-1",
                        "return_value": {
                            "is_error": False,
                            "output": """\
<untrusted_data id="<NONCE>">
ok

</untrusted_data>\
""",
                            "message": "Command executed successfully.",
                            "display": [],
                            "extras": {"status": "success"},
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


def test_display_block_shell(tmp_path) -> None:
    scripts = [
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-1", "env echo ok"),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=False,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run shell"},
            }
        )
        resp, messages = collect_until_response(
            wire,
            "prompt-1",
            request_handler=lambda msg: build_approval_response(msg, "approve"),
        )
        assert resp.get("result", {}).get("status") == "finished"
        payload = _extract_request_payload(messages)
        assert "shell" in _display_types(payload)
        assert normalize_value(payload) == snapshot(
            {
                "id": "<uuid>",
                "tool_call_id": "tc-1",
                "sender": "Shell",
                "action": "run command",
                "description": "Run command `env echo ok`",
                "source_kind": "foreground_turn",
                "source_id": "<uuid>",
                "agent_id": None,
                "subagent_type": None,
                "source_description": None,
                "display": [{"type": "shell", "language": "bash", "command": "env echo ok"}],
            }
        )
    finally:
        wire.close()


def test_display_block_diff_write_file(tmp_path) -> None:
    write_args = {"path": "file.txt", "content": "hello", "mode": "overwrite"}
    scripts = [
        "\n".join(
            [
                "text: write",
                _tool_call_line("tc-1", "WriteFile", write_args),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=False,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "write file"},
            }
        )
        resp, messages = collect_until_response(
            wire,
            "prompt-1",
            request_handler=lambda msg: build_approval_response(msg, "approve"),
        )
        assert resp.get("result", {}).get("status") == "finished"
        payload = _extract_request_payload(messages)
        assert "diff" in _display_types(payload)
        assert normalize_value(payload) == snapshot(
            {
                "id": "<uuid>",
                "tool_call_id": "tc-1",
                "sender": "WriteFile",
                "action": "edit file",
                "description": "Write file `<work_dir>/file.txt`",
                "source_kind": "foreground_turn",
                "source_id": "<uuid>",
                "agent_id": None,
                "subagent_type": None,
                "source_description": None,
                "display": [
                    {
                        "type": "diff",
                        "path": "<work_dir>/file.txt",
                        "old_text": "",
                        "new_text": "hello",
                        "old_start": 1,
                        "new_start": 1,
                        "is_summary": False,
                    }
                ],
            }
        )
    finally:
        wire.close()


def test_display_block_diff_str_replace(tmp_path) -> None:
    replace_args = {
        "path": "file.txt",
        "edit": {"old": "hello", "new": "hi", "replace_all": False},
    }
    scripts = [
        "\n".join(
            [
                "text: replace",
                _tool_call_line("tc-1", "StrReplaceFile", replace_args),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    work_dir = make_work_dir(tmp_path)
    (work_dir / "file.txt").write_text("hello", encoding="utf-8")
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=False,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "replace"},
            }
        )
        resp, messages = collect_until_response(
            wire,
            "prompt-1",
            request_handler=lambda msg: build_approval_response(msg, "approve"),
        )
        assert resp.get("result", {}).get("status") == "finished"
        payload = _extract_request_payload(messages)
        assert "diff" in _display_types(payload)
        assert normalize_value(payload) == snapshot(
            {
                "id": "<uuid>",
                "tool_call_id": "tc-1",
                "sender": "StrReplaceFile",
                "action": "edit file",
                "description": "Edit file `<work_dir>/file.txt`",
                "source_kind": "foreground_turn",
                "source_id": "<uuid>",
                "agent_id": None,
                "subagent_type": None,
                "source_description": None,
                "display": [
                    {
                        "type": "diff",
                        "path": "<work_dir>/file.txt",
                        "old_text": "hello",
                        "new_text": "hi",
                        "old_start": 1,
                        "new_start": 1,
                        "is_summary": False,
                    }
                ],
            }
        )
    finally:
        wire.close()


def test_display_block_todo(tmp_path) -> None:
    script = "\n".join(
        ["text: todo", build_set_todo_call("tc-1", [{"title": "one", "status": "pending"}])]
    )
    scripts = [script, "text: done"]
    config_path = write_scripted_config(tmp_path, scripts)
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
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "todo"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {"method": "event", "type": "TurnBegin", "payload": {"user_input": "todo"}},
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "todo"},
                },
                {
                    "method": "event",
                    "type": "ToolCall",
                    "payload": {
                        "type": "function",
                        "id": "tc-1",
                        "function": {
                            "name": "SetTodoList",
                            "arguments": '{"todos": [{"title": "one", "status": "pending"}]}',
                        },
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
                            "output": "Todo list updated",
                            "message": "Todo list updated",
                            "display": [
                                {"type": "todo", "items": [{"title": "one", "status": "pending"}]}
                            ],
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
                {
                    "method": "event",
                    "type": "TodoListUpdated",
                    "payload": {"items": [["one", "pending"]], "complete": False, "source": "tool"},
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


def test_tool_call_part_streaming(tmp_path) -> None:
    part_middle = '"todos":[{"title":"a","status":"pending"}]'
    part_middle_json = json.dumps({"arguments_part": part_middle})
    script = "\n".join(
        [
            "text: start",
            f"tool_call: {json.dumps({'id': 'tc-1', 'name': 'SetTodoList', 'arguments': None})}",
            'tool_call_part: {"arguments_part": "{"}',
            f"tool_call_part: {part_middle_json}",
            'tool_call_part: {"arguments_part": "}"}',
            "tool_call_part:",
        ]
    )
    config_path = write_scripted_config(tmp_path, [script, "text: done"])
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
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "stream"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {"method": "event", "type": "TurnBegin", "payload": {"user_input": "stream"}},
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "start"},
                },
                {
                    "method": "event",
                    "type": "ToolCall",
                    "payload": {
                        "type": "function",
                        "id": "tc-1",
                        "function": {"name": "SetTodoList", "arguments": None},
                        "extras": None,
                    },
                },
                {"method": "event", "type": "ToolCallPart", "payload": {"arguments_part": "{"}},
                {
                    "method": "event",
                    "type": "ToolCallPart",
                    "payload": {"arguments_part": '"todos":[{"title":"a","status":"pending"}]'},
                },
                {"method": "event", "type": "ToolCallPart", "payload": {"arguments_part": "}"}},
                {"method": "event", "type": "ToolCallPart", "payload": {"arguments_part": None}},
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
                            "output": "Todo list updated",
                            "message": "Todo list updated",
                            "display": [
                                {"type": "todo", "items": [{"title": "a", "status": "pending"}]}
                            ],
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
                {
                    "method": "event",
                    "type": "TodoListUpdated",
                    "payload": {"items": [["a", "pending"]], "complete": False, "source": "tool"},
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


def test_default_agent_missing_tool(tmp_path) -> None:
    dmail_args = {"message": "hi"}
    scripts = [
        "\n".join(
            [
                "text: missing tool",
                _tool_call_line("tc-1", "SendDMail", dmail_args),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
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
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "dmail"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {"method": "event", "type": "TurnBegin", "payload": {"user_input": "dmail"}},
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "missing tool"},
                },
                {
                    "method": "event",
                    "type": "ToolCall",
                    "payload": {
                        "type": "function",
                        "id": "tc-1",
                        "function": {"name": "SendDMail", "arguments": '{"message": "hi"}'},
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
                    "method": "event",
                    "type": "ToolResult",
                    "payload": {
                        "tool_call_id": "tc-1",
                        "return_value": {
                            "is_error": True,
                            "output": "",
                            "message": "Tool `SendDMail` not found",
                            "display": [{"type": "brief", "text": "Tool `SendDMail` not found"}],
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


def test_custom_agent_exclude_tool(tmp_path) -> None:
    shell_args = {"command": "echo hi"}
    scripts = [
        "\n".join(
            [
                "text: missing tool",
                _tool_call_line("tc-1", "Shell", shell_args),
            ]
        ),
        "text: done",
    ]
    config_path = write_scripted_config(tmp_path, scripts)
    agent_path = tmp_path / "agent.yaml"
    agent_path.write_text(
        "\n".join(
            [
                "version: 1",
                "agent:",
                "  extend: default",
                "  exclude_tools:",
                '    - "pythinker_code.tools.shell:Shell"',
            ]
        ),
        encoding="utf-8",
    )
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
        agent_file=agent_path,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "shell"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {"method": "event", "type": "TurnBegin", "payload": {"user_input": "shell"}},
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "missing tool"},
                },
                {
                    "method": "event",
                    "type": "ToolCall",
                    "payload": {
                        "type": "function",
                        "id": "tc-1",
                        "function": {"name": "Shell", "arguments": '{"command": "echo hi"}'},
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
                    "method": "event",
                    "type": "ToolResult",
                    "payload": {
                        "tool_call_id": "tc-1",
                        "return_value": {
                            "is_error": True,
                            "output": "",
                            "message": "Tool `Shell` not found",
                            "display": [{"type": "brief", "text": "Tool `Shell` not found"}],
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
