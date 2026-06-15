from __future__ import annotations

import hashlib
import json
import sys
import textwrap
from pathlib import Path

from inline_snapshot import snapshot

from tests_e2e.wire_helpers import (
    build_approval_response,
    collect_until_response,
    make_home_dir,
    make_work_dir,
    send_initialize,
    share_dir,
    start_wire,
    summarize_messages,
    write_scripted_config,
)


def _session_dir(home_dir: Path, work_dir: Path, session_id: str) -> Path:
    digest = hashlib.md5(str(work_dir).encode("utf-8"), usedforsecurity=False).hexdigest()
    return share_dir(home_dir) / "sessions" / digest / session_id


def _read_user_texts(context_file: Path) -> list[str]:
    texts: list[str] = []
    for line in context_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("role") != "user":
            continue
        content = payload.get("content", "")
        if isinstance(content, str):
            texts.append(content)
            continue
        if isinstance(content, list):
            text = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
            texts.append(text)
    return texts


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def test_skill_prompt_injects_skill_text(tmp_path) -> None:
    skill_dir = tmp_path / "skills"
    skill_path = skill_dir / "test-skill"
    skill_path.mkdir(parents=True)
    skill_text = "\n".join(
        [
            "---",
            "name: test",
            "description: Test skill",
            "---",
            "",
            "Use this skill in wire tests.",
        ]
    )
    skill_path.joinpath("SKILL.md").write_text(skill_text + "\n", encoding="utf-8")

    config_path = write_scripted_config(tmp_path, ["text: skill ok"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    session_id = "skill-session"

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        skills_dirs=[skill_dir],
        extra_args=["--session", session_id],
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "/skill:test"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {
                    "method": "event",
                    "type": "TurnBegin",
                    "payload": {"user_input": "/skill:test"},
                },
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "skill ok"},
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

    context_file = _session_dir(home_dir, work_dir, session_id) / "context.jsonl"
    user_texts = _read_user_texts(context_file)
    assert user_texts
    normalized_skill = _normalize_newlines(skill_text.strip())
    assert any(_normalize_newlines(t) == normalized_skill for t in user_texts)


def test_flow_skill(tmp_path) -> None:
    skill_dir = tmp_path / "skills"
    flow_dir = skill_dir / "test-flow"
    flow_dir.mkdir(parents=True)
    flow_dir.joinpath("SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: test-flow",
                "description: Test flow",
                "type: flow",
                "---",
                "",
                "```mermaid",
                "flowchart TD",
                "A([BEGIN]) --> B[Say hello]",
                "B --> C([END])",
                "```",
            ]
        ),
        encoding="utf-8",
    )

    config_path = write_scripted_config(tmp_path, ["text: flow done"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        skills_dirs=[skill_dir],
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "/flow:test-flow"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {
                    "method": "event",
                    "type": "TurnBegin",
                    "payload": {"user_input": "/flow:test-flow"},
                },
                {
                    "method": "event",
                    "type": "TurnBegin",
                    "payload": {"user_input": "Say hello"},
                },
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "flow done"},
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
                {"method": "event", "type": "TurnEnd", "payload": {}},
            ]
        )
    finally:
        wire.close()


def test_mcp_tool_call(tmp_path) -> None:
    server_path = tmp_path / "mcp_server.py"
    server_path.write_text(
        textwrap.dedent(
            """
            from fastmcp.server import FastMCP

            server = FastMCP("test-mcp")

            @server.tool
            def ping(text: str) -> str:
                return f"pong:{text}"

            if __name__ == "__main__":
                server.run(transport="stdio", show_banner=False)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    mcp_config = {
        "mcpServers": {
            "test": {
                "command": sys.executable,
                "args": [str(server_path)],
            }
        }
    }
    mcp_config_path = tmp_path / "mcp.json"
    mcp_config_path.write_text(json.dumps(mcp_config), encoding="utf-8")

    tool_args = json.dumps({"text": "hi"})
    tool_call = json.dumps({"id": "tc-1", "name": "ping", "arguments": tool_args})
    scripts = [
        "\n".join(
            [
                "text: call mcp",
                f"tool_call: {tool_call}",
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
        mcp_config_path=mcp_config_path,
        yolo=False,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "call mcp"},
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
                {
                    "method": "event",
                    "type": "TurnBegin",
                    "payload": {"user_input": "call mcp"},
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
                        "model_name": None,
                        "provider_key": None,
                        "plan_mode": None,
                        "mcp_status": {
                            "loading": True,
                            "connected": 0,
                            "total": 1,
                            "tools": 0,
                            "servers": [
                                {"name": "test", "status": "connecting", "tools": [], "error": None}
                            ],
                        },
                    },
                },
                {"method": "event", "type": "MCPLoadingBegin", "payload": {}},
                {
                    "method": "event",
                    "type": "StatusUpdate",
                    "payload": {
                        "context_usage": None,
                        "context_tokens": None,
                        "max_context_tokens": None,
                        "token_usage": None,
                        "message_id": None,
                        "model_name": None,
                        "provider_key": None,
                        "plan_mode": None,
                        "mcp_status": {
                            "loading": False,
                            "connected": 1,
                            "total": 1,
                            "tools": 1,
                            "servers": [
                                {
                                    "name": "test",
                                    "status": "connected",
                                    "tools": ["ping"],
                                    "error": None,
                                }
                            ],
                        },
                    },
                },
                {"method": "event", "type": "MCPLoadingEnd", "payload": {}},
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "call mcp"},
                },
                {
                    "method": "event",
                    "type": "ToolCall",
                    "payload": {
                        "type": "function",
                        "id": "tc-1",
                        "function": {"name": "ping", "arguments": '{"text": "hi"}'},
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
                        "sender": "ping",
                        "action": "mcp:ping",
                        "description": "Call MCP tool `ping`.",
                        "source_kind": "foreground_turn",
                        "source_id": "<uuid>",
                        "agent_id": None,
                        "subagent_type": None,
                        "source_description": None,
                        "display": [],
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
                    "type": "ToolResult",
                    "payload": {
                        "tool_call_id": "tc-1",
                        "return_value": {
                            "is_error": False,
                            "output": [{"type": "text", "text": "pong:hi"}],
                            "message": "",
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
