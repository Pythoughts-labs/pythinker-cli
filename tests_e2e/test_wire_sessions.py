from __future__ import annotations

import hashlib
import json
from pathlib import Path

from inline_snapshot import snapshot

from tests_e2e.wire_helpers import (
    build_approval_response,
    build_shell_tool_call,
    collect_until_response,
    make_home_dir,
    make_work_dir,
    send_initialize,
    share_dir,
    start_wire,
    summarize_messages,
    write_scripted_config,
)


def _session_dir(home_dir: Path, work_dir: Path) -> Path:
    digest = hashlib.md5(str(work_dir).encode("utf-8"), usedforsecurity=False).hexdigest()
    return share_dir(home_dir) / "sessions" / digest


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def _read_roles(path: Path) -> list[str]:
    if not path.exists():
        return []
    roles: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        roles.append(json.loads(line)["role"])
    return roles


def test_session_files_created(tmp_path) -> None:
    config_path = write_scripted_config(tmp_path, ["text: hello"])
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    session_id = "e2e-session"

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
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
                "params": {"user_input": "hi"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
    finally:
        wire.close()

    session_dir = _session_dir(home_dir, work_dir) / session_id
    context_file = session_dir / "context.jsonl"
    wire_file = session_dir / "wire.jsonl"
    assert context_file.exists()
    assert wire_file.exists()
    assert context_file.stat().st_size > 0
    assert wire_file.stat().st_size > 0
    assert sorted(p.name for p in session_dir.iterdir()) == snapshot(
        [".owner.lock", "context.jsonl", "external_agents", "state.json", "wire.jsonl"]
    )


def test_continue_session_appends(tmp_path) -> None:
    config_path = write_scripted_config(tmp_path, ["text: first", "text: second"])
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
                "params": {"user_input": "first"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"
    finally:
        wire.close()

    session_root = _session_dir(home_dir, work_dir)
    session_ids = [p.name for p in session_root.iterdir() if p.is_dir()]
    assert len(session_ids) == 1
    session_id = session_ids[0]
    session_dir = session_root / session_id
    context_file = session_dir / "context.jsonl"
    wire_file = session_dir / "wire.jsonl"
    context_before = _count_lines(context_file)
    wire_before = _count_lines(wire_file)

    wire = start_wire(
        config_path=config_path,
        config_text=None,
        work_dir=work_dir,
        home_dir=home_dir,
        extra_args=["--continue"],
        yolo=True,
    )
    try:
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "second"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-2")
        assert resp.get("result", {}).get("status") == "finished"
    finally:
        wire.close()

    context_after = _count_lines(context_file)
    wire_after = _count_lines(wire_file)
    assert context_after > context_before
    assert wire_after > wire_before
    assert {
        "context_before": context_before,
        "context_after": context_after,
        "wire_before": wire_before,
        "wire_after": wire_after,
    } == snapshot({"context_before": 6, "context_after": 11, "wire_before": 7, "wire_after": 13})
    assert _read_roles(context_file) == snapshot(
        [
            "_system_prompt",
            "_checkpoint",
            "user",
            "_checkpoint",
            "user",
            "assistant",
            "_checkpoint",
            "user",
            "_checkpoint",
            "user",
            "assistant",
        ]
    )


def test_clear_context_rotates(tmp_path) -> None:
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
        send_initialize(wire)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "hi"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"

        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "/clear"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-2")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {"method": "event", "type": "TurnBegin", "payload": {"user_input": "/clear"}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "The context has been cleared."},
                },
                {
                    "method": "event",
                    "type": "StatusUpdate",
                    "payload": {
                        "context_usage": 0.0,
                        "context_tokens": 0,
                        "max_context_tokens": 100000,
                        "token_usage": None,
                        "message_id": None,
                        "model_name": None,
                        "provider_key": None,
                        "plan_mode": None,
                        "mcp_status": None,
                    },
                },
                {"method": "event", "type": "TurnEnd", "payload": {}},
            ]
        )
    finally:
        wire.close()

    session_root = _session_dir(home_dir, work_dir)
    session_ids = [p.name for p in session_root.iterdir() if p.is_dir()]
    assert len(session_ids) == 1
    session_dir = session_root / session_ids[0]
    context_file = session_dir / "context.jsonl"
    assert _read_roles(context_file) == snapshot(["_system_prompt"])
    rotated = sorted(
        p.name
        for p in session_dir.iterdir()
        if p.is_file() and p.name.startswith("context_") and p.suffix == ".jsonl"
    )
    assert rotated == snapshot(["context_1.jsonl"])
    assert _read_roles(session_dir / rotated[0]) == snapshot(
        ["_system_prompt", "_checkpoint", "user", "_checkpoint", "user", "assistant"]
    )


def test_manual_compact(tmp_path) -> None:
    scripts = [
        "text: hello",
        "text: compacted summary",
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
                "params": {"user_input": "hi"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"

        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "/compact"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-2")
        assert resp.get("result", {}).get("status") == "finished"
        assert summarize_messages(messages) == snapshot(
            [
                {"method": "event", "type": "TurnBegin", "payload": {"user_input": "/compact"}},
                {"method": "event", "type": "CompactionBegin", "payload": {}},
                {"method": "event", "type": "CompactionEnd", "payload": {}},
                {
                    "method": "event",
                    "type": "ContentPart",
                    "payload": {"type": "text", "text": "Conversation compacted."},
                },
                {
                    "method": "event",
                    "type": "StatusUpdate",
                    "payload": {
                        "context_usage": 0.01913,
                        "context_tokens": 1913,
                        "max_context_tokens": 100000,
                        "token_usage": None,
                        "message_id": None,
                        "model_name": None,
                        "provider_key": None,
                        "plan_mode": None,
                        "mcp_status": None,
                    },
                },
                {"method": "event", "type": "TurnEnd", "payload": {}},
            ]
        )
    finally:
        wire.close()


def test_manual_compact_with_usage(tmp_path) -> None:
    """Compaction with enough messages to trigger an actual LLM call that returns usage."""
    scripts = [
        "text: hello\nusage: input_other=10 output=5",
        "text: I'm good\nusage: input_other=30 output=8",
        "text: compacted summary\nusage: input_other=50 output=20",
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

        # Two rounds of conversation to build up context beyond max_preserved_messages=2
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-1",
                "method": "prompt",
                "params": {"user_input": "hi"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-1")
        assert resp.get("result", {}).get("status") == "finished"

        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-2",
                "method": "prompt",
                "params": {"user_input": "how are you"},
            }
        )
        resp, _ = collect_until_response(wire, "prompt-2")
        assert resp.get("result", {}).get("status") == "finished"

        # Now compact — this triggers a real compaction LLM call (script 3)
        wire.send_json(
            {
                "jsonrpc": "2.0",
                "id": "prompt-3",
                "method": "prompt",
                "params": {"user_input": "/compact"},
            }
        )
        resp, messages = collect_until_response(wire, "prompt-3")
        assert resp.get("result", {}).get("status") == "finished"

        # Verify context_usage is non-zero (usage.output=20 + preserved text estimate)
        status_msg = [m for m in messages if m.get("params", {}).get("type") == "StatusUpdate"]
        assert len(status_msg) == 1
        context_usage = status_msg[0]["params"]["payload"]["context_usage"]
        assert context_usage > 0, "context_usage should be non-zero after compaction with usage"
    finally:
        wire.close()


def test_replay_streams_wire_history(tmp_path) -> None:
    scripts = [
        "\n".join(
            [
                "text: step1",
                build_shell_tool_call("tc-1", "echo ok"),
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
        extra_args=["--session", "replay-session"],
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
        resp, _ = collect_until_response(
            wire,
            "prompt-1",
            request_handler=lambda msg: build_approval_response(msg, "approve"),
        )
        assert resp.get("result", {}).get("status") == "finished"

        wire.send_json({"jsonrpc": "2.0", "id": "replay-1", "method": "replay"})
        resp, messages = collect_until_response(wire, "replay-1")
        assert resp.get("result") == snapshot(
            {
                "status": "finished",
                "events": 13,
                "requests": 0,
            }
        )
        assert summarize_messages(messages) == snapshot(
            [
                {
                    "method": "event",
                    "type": "TurnBegin",
                    "payload": {"user_input": "run shell"},
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
                        "function": {"name": "Shell", "arguments": '{"command": "echo ok"}'},
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
