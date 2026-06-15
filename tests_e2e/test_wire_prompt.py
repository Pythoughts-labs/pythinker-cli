from __future__ import annotations

from typing import Any

from inline_snapshot import snapshot

from tests_e2e.wire_helpers import (
    build_approval_response,
    build_set_todo_call,
    build_shell_tool_call,
    collect_until_request,
    collect_until_response,
    make_home_dir,
    make_work_dir,
    normalize_response,
    read_response,
    send_initialize,
    start_wire,
    summarize_messages,
    write_scripted_config,
)


def _find_event(messages: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    for msg in messages:
        if msg.get("method") != "event":
            continue
        params = msg.get("params")
        if isinstance(params, dict) and params.get("type") == event_type:
            return params
    raise AssertionError(f"Missing event {event_type}")


def test_basic_prompt_events(tmp_path) -> None:
    script = "\n".join(
        [
            "id: scripted-1",
            'usage: {"input_other": 5, "output": 2}',
            "text: Hello wire",
        ]
    )
    config_path = write_scripted_config(tmp_path, [script])
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
                "params": {"user_input": "hi"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {
                    "method": "event",
                    "type": "TurnBegin",
                    "payload": {"user_input": "hi"},
                },
                {"method": "event", "type": "StepBegin", "payload": {"n": 1}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "Hello wire"},
                },
                {
                    "method": "event",
                    "type": "StatusUpdate",
                    "payload": {
                        "context_usage": 5e-05,
                        "context_tokens": 5,
                        "max_context_tokens": 100000,
                        "token_usage": {
                            "input_other": 5,
                            "output": 2,
                            "input_cache_read": 0,
                            "input_cache_creation": 0,
                        },
                        "message_id": "scripted-1",
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


def test_multiline_prompt(tmp_path) -> None:
    config_path = write_scripted_config(tmp_path, ["text: ok"])
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
        user_input = "line1\nline2"
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": user_input},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        turn_begin = _find_event(messages, "TurnBegin")
        payload = turn_begin.get("payload")
        assert isinstance(payload, dict)
        assert payload.get("user_input") == user_input
        assert turn_begin == snapshot(
            {
                "type": "TurnBegin",
                "payload": {
                    "user_input": """\
line1
line2\
"""
                },
            }
        )
    finally:
        wire.close()


def test_content_part_prompt(tmp_path) -> None:
    config_path = write_scripted_config(
        tmp_path,
        ["text: ok"],
        capabilities=["image_in", "video_in"],
    )
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    content_parts = [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "audio_url", "audio_url": {"url": "data:audio/aac;base64,AAA"}},
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAA"}},
    ]
    expected_parts = [
        {"type": "text", "text": "hello"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAA", "id": None},
        },
        {
            "type": "audio_url",
            "audio_url": {"url": "data:audio/aac;base64,AAA", "id": None},
        },
        {
            "type": "video_url",
            "video_url": {"url": "data:video/mp4;base64,AAA", "id": None},
        },
    ]

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
                "params": {"user_input": content_parts},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
        turn_begin = _find_event(messages, "TurnBegin")
        payload = turn_begin.get("payload")
        assert isinstance(payload, dict)
        assert payload.get("user_input") == expected_parts
        assert turn_begin == snapshot(
            {
                "type": "TurnBegin",
                "payload": {
                    "user_input": [
                        {"type": "text", "text": "hello"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAA", "id": None},
                        },
                        {
                            "type": "audio_url",
                            "audio_url": {"url": "data:audio/aac;base64,AAA", "id": None},
                        },
                        {
                            "type": "video_url",
                            "video_url": {"url": "data:video/mp4;base64,AAA", "id": None},
                        },
                    ]
                },
            }
        )
    finally:
        wire.close()


def test_max_steps_reached(tmp_path) -> None:
    todo_line = build_set_todo_call("tc-1", [{"title": "x", "status": "pending"}])
    script = "\n".join(
        [
            "text: start",
            todo_line,
        ]
    )
    config_path = write_scripted_config(tmp_path, [script])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        extra_args=["--max-steps-per-turn", "1"],
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "run"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "max_steps_reached"
        assert normalize_response(resp) == snapshot(
            {"result": {"status": "max_steps_reached", "steps": 1}}
        )
        assert summarize_messages(messages) == snapshot(
            [
                {
                    "method": "event",
                    "type": "TurnBegin",
                    "payload": {"user_input": "run"},
                },
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
                        "function": {
                            "name": "SetTodoList",
                            "arguments": '{"todos": [{"title": "x", "status": "pending"}]}',
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
                                {"type": "todo", "items": [{"title": "x", "status": "pending"}]}
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
                    "payload": {"items": [["x", "pending"]], "complete": False, "source": "tool"},
                },
                {
                    "method": "event",
                    "type": "TurnEnd",
                    "payload": {},
                },
            ]
        )
    finally:
        wire.close()


def test_status_update_fields(tmp_path) -> None:
    script = "\n".join(
        [
            "id: scripted-1",
            'usage: {"input_other": 5, "output": 2}',
            "text: hello",
        ]
    )
    config_path = write_scripted_config(tmp_path, [script])
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
                "params": {"user_input": "hi"},
            }
        )
        _, messages = collect_until_response(wire, "prompt-1")
        status = _find_event(messages, "StatusUpdate")
        payload = status.get("payload")
        assert isinstance(payload, dict)
        assert isinstance(payload.get("token_usage"), dict)
        assert status == snapshot(
            {
                "type": "StatusUpdate",
                "payload": {
                    "context_usage": 5e-05,
                    "context_tokens": 5,
                    "max_context_tokens": 100000,
                    "token_usage": {
                        "input_other": 5,
                        "output": 2,
                        "input_cache_read": 0,
                        "input_cache_creation": 0,
                    },
                    "message_id": "scripted-1",
                    "model_name": "scripted_echo",
                    "provider_key": "scripted_provider",
                    "plan_mode": False,
                    "mcp_status": None,
                },
            }
        )
    finally:
        wire.close()


def test_concurrent_prompt_error(tmp_path) -> None:
    scripts = [
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-1", "echo hi"),
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
                "params": {"user_input": "run"},
            }
        )
        request_msg, messages = collect_until_request(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "second"},
            }
        )
        prompt2_resp = normalize_response(read_response(wire, "prompt-2"))
        assert prompt2_resp == snapshot(
            {
                "error": {
                    "code": -32000,
                    "message": "An agent turn is already in progress",
                    "data": None,
                }
            }
        )

        wire.send_json(build_approval_response(request_msg, "approve"))
        prompt1_resp, messages_after = collect_until_response(wire, "prompt-1")
        assert prompt1_resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages + messages_after) == snapshot(
            [
                {
                    "method": "event",
                    "type": "TurnBegin",
                    "payload": {"user_input": "run"},
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
                        "id": "tc-1",
                        "function": {
                            "name": "Shell",
                            "arguments": '{"command": "echo hi"}',
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
                    "method": "request",
                    "type": "ApprovalRequest",
                    "payload": {
                        "id": "<uuid>",
                        "tool_call_id": "tc-1",
                        "sender": "Shell",
                        "action": "run command",
                        "description": "Run command `echo hi`",
                        "source_kind": "foreground_turn",
                        "source_id": "<uuid>",
                        "agent_id": None,
                        "subagent_type": None,
                        "source_description": None,
                        "display": [{"type": "shell", "language": "bash", "command": "echo hi"}],
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
                    "payload": {"tool_call_id": "tc-1", "stream": "stdout", "text": "hi\n"},
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
hi

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
