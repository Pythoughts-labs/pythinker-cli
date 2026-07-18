from __future__ import annotations

import re
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from inline_snapshot import snapshot

from pythinker_code.agentspec import DEFAULT_AGENT_FILE, load_agent_spec
from pythinker_code.exception import AgentSpecError


def test_load_default_agent_spec():
    """Test loading the default agent specification."""
    spec = load_agent_spec(DEFAULT_AGENT_FILE)

    assert spec.name == snapshot("")
    assert spec.system_prompt_path == DEFAULT_AGENT_FILE.parent / "system.md"
    assert spec.system_prompt_args == snapshot({"ROLE_ADDITIONAL": "", "EMITS_CODING_ARTIFACT": ""})
    assert spec.when_to_use == snapshot("")
    assert spec.model == snapshot(None)
    assert spec.mode == snapshot("primary")
    assert spec.hidden == snapshot(False)
    assert spec.steps == snapshot(None)
    assert spec.temperature == snapshot(None)
    assert spec.top_p == snapshot(None)
    assert spec.allowed_tools == snapshot(None)
    assert spec.exclude_tools == snapshot([])
    assert spec.tools == snapshot(
        [
            "pythinker_code.tools.agent:Agent",
            "pythinker_code.tools.agent:RunAgents",
            "pythinker_code.tools.agent:ImplementAndJudge",
            "pythinker_code.tools.skill:ReadSkill",
            "pythinker_code.tools.ask_user:AskUserQuestion",
            "pythinker_code.tools.todo:SetTodoList",
            "pythinker_code.tools.tool_search:ToolSearch",
            "pythinker_code.tools.worktree:EnterWorktree",
            "pythinker_code.tools.worktree:ExitWorktree",
            "pythinker_code.tools.goal:UpdateGoal",
            "pythinker_code.tools.progress:Progress",
            "pythinker_code.tools.workflow:Workflow",
            "pythinker_code.tools.suggest:Suggest",
            "pythinker_code.tools.memory:Memory",
            "pythinker_code.tools.recall:Recall",
            "pythinker_code.tools.scratchpad:Scratchpad",
            "pythinker_code.tools.shell:Shell",
            "pythinker_code.tools.background:TaskList",
            "pythinker_code.tools.background:TaskOutput",
            "pythinker_code.tools.background:TaskInput",
            "pythinker_code.tools.background:TaskHandoff",
            "pythinker_code.tools.background:TaskStop",
            "pythinker_code.tools.file:ReadFile",
            "pythinker_code.tools.file:ReadMediaFile",
            "pythinker_code.tools.file:Glob",
            "pythinker_code.tools.file:Grep",
            "pythinker_code.tools.file:SmartSearch",
            "pythinker_code.tools.lsp:Lsp",
            "pythinker_code.tools.file:WriteFile",
            "pythinker_code.tools.file:StrReplaceFile",
            "pythinker_code.tools.web:SearchWeb",
            "pythinker_code.tools.web:FetchURL",
            "pythinker_code.tools.mcp_resource:ListMcpResources",
            "pythinker_code.tools.mcp_resource:ReadMcpResource",
            "pythinker_code.tools.mcp_resource:InvokeMcpPrompt",
            "pythinker_code.tools.plan:ExitPlanMode",
            "pythinker_code.tools.plan.enter:EnterPlanMode",
        ]
    )
    subagents = {
        name: (spec.path.relative_to(DEFAULT_AGENT_FILE.parent).as_posix(), spec.description)
        for name, spec in spec.subagents.items()
    }
    assert subagents == snapshot(
        {
            "coder": ("coder.yaml", "Good at general software engineering tasks."),
            "code-reviewer": (
                "code_reviewer.yaml",
                "Diff-focused code review with severity-scored findings.",
            ),
            "debugger": (
                "debugger.yaml",
                "Failure/log/stack-trace root-cause analysis with reproduction evidence.",
            ),
            "explore": (
                "explore.yaml",
                "Fast codebase exploration with prompt-enforced read-only behavior.",
            ),
            "plan": ("plan.yaml", "Read-only implementation planning and architecture design."),
            "planner": (
                "planner.yaml",
                "Read-only recon planner that decomposes tasks into distinct parallel seeds.",
            ),
            "scout": (
                "scout.yaml",
                "Read-only external docs, dependency-source, and API freshness researcher.",
            ),
            "review": ("review.yaml", "Read-only code review with severity-scored findings."),
            "security-reviewer": (
                "security_reviewer.yaml",
                "Diff-focused security review with validated findings.",
            ),
            "implementer": (
                "implementer.yaml",
                "Scoped implementation with minimal edits and verification.",
            ),
            "judge": (
                "judge.yaml",
                "Independent final quality gate for answers, reports, and code-change summaries.",
            ),
            "verifier": (
                "verifier.yaml",
                "Read-only validation runner for tests, lint, and builds.",
            ),
        }
    )

    subagent_specs = {name: load_agent_spec(spec.path) for name, spec in spec.subagents.items()}

    leaf_subagent_names = (
        "verifier",
        "judge",
        "explore",
        "plan",
        "planner",
        "scout",
        "review",
        "code-reviewer",
        "security-reviewer",
        "debugger",
    )
    for name in leaf_subagent_names:
        assert (
            subagent_specs[name].system_prompt_path == DEFAULT_AGENT_FILE.parent / "system_leaf.md"
        )
        assert subagent_specs[name].system_prompt_args["EMITS_CODING_ARTIFACT"] == ""
        role_additional = subagent_specs[name].system_prompt_args["ROLE_ADDITIONAL"]
        assert role_additional.startswith("## Mission\n")
    for subagent_spec in subagent_specs.values():
        role_additional = subagent_spec.system_prompt_args["ROLE_ADDITIONAL"]
        assert "You are now running as a subagent" not in role_additional
        assert "Artifact contract:" not in role_additional
    # The implementer must stay on the leaf profile with the artifact block
    # enabled — the implementer-to-judge handoff parses <coding_artifact>.
    assert (
        subagent_specs["implementer"].system_prompt_path
        == DEFAULT_AGENT_FILE.parent / "system_leaf.md"
    )
    assert subagent_specs["implementer"].system_prompt_args["EMITS_CODING_ARTIFACT"] == "true"
    assert subagent_specs["coder"].name == snapshot("")
    assert (
        subagent_specs["coder"].system_prompt_path == DEFAULT_AGENT_FILE.parent / "system_leaf.md"
    )
    coder_prompt_args = subagent_specs["coder"].system_prompt_args
    assert coder_prompt_args["EMITS_CODING_ARTIFACT"] == "true"
    coder_role = coder_prompt_args["ROLE_ADDITIONAL"]
    assert [line for line in coder_role.splitlines() if line.startswith("## ")] == [
        "## Mission",
        "## Hard Constraints",
        "## Code Quality Standard",
        "## Language Adaptability",
        "## Context Gate",
        "## Workflow",
        "## Untrusted Content",
        "## Role Exit Checklist",
        "## Output Contract",
        "## Escalation",
    ]
    assert {
        (
            "You are the general engineering subagent: you take a scoped brief from the parent and "
            "deliver clean, well-structured, production-ready code — verified, idiomatic to the "
            "project's language and conventions, and complete. You read, edit, and run code. You "
            "never expand into adjacent cleanup, refactors, or improvements the brief did not ask "
            "for."
        ),
        (
            "- Stay tightly scoped to exactly what the parent assigned; surface related work under "
            "RISKS or BLOCKERS rather than doing it."
        ),
        (
            "- Never report success without naming the verification command you ran and the result "
            "you observed."
        ),
    } <= set(coder_role.splitlines())
    assert subagent_specs["coder"].when_to_use == snapshot(
        "Use this agent for non-trivial software engineering work that may require reading files, editing code, running commands, and returning a compact but technically complete summary to the parent agent. It delivers production-ready, idiomatic, verified changes in any language the project uses, with current-docs verification for third-party APIs, and never expands beyond its brief.\n"
    )
    assert subagent_specs["coder"].model == snapshot(None)
    assert subagent_specs["coder"].allowed_tools == snapshot(
        [
            "pythinker_code.tools.shell:Shell",
            "pythinker_code.tools.todo:SetTodoList",
            "pythinker_code.tools.file:ReadFile",
            "pythinker_code.tools.file:ReadMediaFile",
            "pythinker_code.tools.file:Glob",
            "pythinker_code.tools.file:Grep",
            "pythinker_code.tools.file:SmartSearch",
            "pythinker_code.tools.lsp:Lsp",
            "pythinker_code.tools.file:WriteFile",
            "pythinker_code.tools.file:StrReplaceFile",
            "pythinker_code.tools.skill:ReadSkill",
            "pythinker_code.tools.web:SearchWeb",
            "pythinker_code.tools.web:FetchURL",
            "mcp__context7__resolve-library-id",
            "mcp__context7__query-docs",
        ]
    )
    assert subagent_specs["coder"].exclude_tools == snapshot(
        [
            "pythinker_code.tools.agent:Agent",
            "pythinker_code.tools.ask_user:AskUserQuestion",
            "pythinker_code.tools.plan:ExitPlanMode",
            "pythinker_code.tools.plan.enter:EnterPlanMode",
        ]
    )
    assert subagent_specs["coder"].tools == snapshot(
        [
            "pythinker_code.tools.agent:Agent",
            "pythinker_code.tools.agent:RunAgents",
            "pythinker_code.tools.agent:ImplementAndJudge",
            "pythinker_code.tools.skill:ReadSkill",
            "pythinker_code.tools.ask_user:AskUserQuestion",
            "pythinker_code.tools.todo:SetTodoList",
            "pythinker_code.tools.tool_search:ToolSearch",
            "pythinker_code.tools.worktree:EnterWorktree",
            "pythinker_code.tools.worktree:ExitWorktree",
            "pythinker_code.tools.goal:UpdateGoal",
            "pythinker_code.tools.progress:Progress",
            "pythinker_code.tools.workflow:Workflow",
            "pythinker_code.tools.suggest:Suggest",
            "pythinker_code.tools.memory:Memory",
            "pythinker_code.tools.recall:Recall",
            "pythinker_code.tools.scratchpad:Scratchpad",
            "pythinker_code.tools.shell:Shell",
            "pythinker_code.tools.background:TaskList",
            "pythinker_code.tools.background:TaskOutput",
            "pythinker_code.tools.background:TaskInput",
            "pythinker_code.tools.background:TaskHandoff",
            "pythinker_code.tools.background:TaskStop",
            "pythinker_code.tools.file:ReadFile",
            "pythinker_code.tools.file:ReadMediaFile",
            "pythinker_code.tools.file:Glob",
            "pythinker_code.tools.file:Grep",
            "pythinker_code.tools.file:SmartSearch",
            "pythinker_code.tools.lsp:Lsp",
            "pythinker_code.tools.file:WriteFile",
            "pythinker_code.tools.file:StrReplaceFile",
            "pythinker_code.tools.web:SearchWeb",
            "pythinker_code.tools.web:FetchURL",
            "pythinker_code.tools.mcp_resource:ListMcpResources",
            "pythinker_code.tools.mcp_resource:ReadMcpResource",
            "pythinker_code.tools.mcp_resource:InvokeMcpPrompt",
            "pythinker_code.tools.plan:ExitPlanMode",
            "pythinker_code.tools.plan.enter:EnterPlanMode",
        ]
    )
    sub_subagents = {
        name: (spec.path.relative_to(DEFAULT_AGENT_FILE.parent).as_posix(), spec.description)
        for name, spec in subagent_specs["coder"].subagents.items()
    }
    assert sub_subagents == snapshot({})

    assert subagent_specs["explore"].name == snapshot("")
    assert (
        subagent_specs["explore"].system_prompt_path == DEFAULT_AGENT_FILE.parent / "system_leaf.md"
    )
    explore_prompt_args = subagent_specs["explore"].system_prompt_args
    assert explore_prompt_args["EMITS_CODING_ARTIFACT"] == ""
    explore_role = explore_prompt_args["ROLE_ADDITIONAL"]
    assert [line for line in explore_role.splitlines() if line.startswith("## ")] == [
        "## Mission",
        "## Hard Constraints",
        "## Context Gate",
        "## Workflow",
        "## Untrusted Content",
        "## Role Exit Checklist",
        "## Output Contract",
        "## Escalation",
    ]
    assert {
        (
            "You are a codebase exploration specialist. Your role is EXCLUSIVELY to search, read, "
            "and analyze existing code and resources. You are meant to be fast: complete the search "
            "request efficiently and stop once the parent has enough evidence rather than "
            "exhaustively reading the whole repository."
        ),
        (
            "- You cannot edit files; report proposed changes, never claim to have made them. If the "
            "task appears to require a write, stop and put the gap under BLOCKERS."
        ),
        (
            "- Distinguish CONFIRMED facts from LIKELY inferences. Put unknowns and missing evidence "
            "under RISKS or BLOCKERS."
        ),
    } <= set(explore_role.splitlines())
    assert subagent_specs["explore"].when_to_use == snapshot(
        'Fast agent specialized for exploring codebases. Use this when you need to quickly find files by patterns (e.g. "src/**/*.yaml"), search code for keywords (e.g. "database connection"), or answer questions about the codebase (e.g. "how does the auth module work?"). When calling this agent, specify the desired thoroughness level: "quick" for basic searches, "medium" for moderate exploration, or "thorough" for comprehensive analysis across multiple locations and naming conventions. Use this agent for any read-only exploration that will clearly require more than 3 tool calls. Prefer launching multiple explore agents concurrently when investigating independent questions. Absence claims come with the searches that back them.\n'
    )
    assert subagent_specs["explore"].model == snapshot(None)
    assert subagent_specs["explore"].allowed_tools == snapshot(
        [
            "pythinker_code.tools.shell:Shell",
            "pythinker_code.tools.todo:SetTodoList",
            "pythinker_code.tools.file:ReadFile",
            "pythinker_code.tools.file:ReadMediaFile",
            "pythinker_code.tools.file:Glob",
            "pythinker_code.tools.file:Grep",
            "pythinker_code.tools.file:SmartSearch",
            "pythinker_code.tools.skill:ReadSkill",
        ]
    )
    assert subagent_specs["explore"].exclude_tools == snapshot(
        [
            "pythinker_code.tools.agent:Agent",
            "pythinker_code.tools.ask_user:AskUserQuestion",
            "pythinker_code.tools.plan:ExitPlanMode",
            "pythinker_code.tools.plan.enter:EnterPlanMode",
            "pythinker_code.tools.file:WriteFile",
            "pythinker_code.tools.file:StrReplaceFile",
        ]
    )
    assert subagent_specs["explore"].tools == snapshot(
        [
            "pythinker_code.tools.agent:Agent",
            "pythinker_code.tools.agent:RunAgents",
            "pythinker_code.tools.agent:ImplementAndJudge",
            "pythinker_code.tools.skill:ReadSkill",
            "pythinker_code.tools.ask_user:AskUserQuestion",
            "pythinker_code.tools.todo:SetTodoList",
            "pythinker_code.tools.tool_search:ToolSearch",
            "pythinker_code.tools.worktree:EnterWorktree",
            "pythinker_code.tools.worktree:ExitWorktree",
            "pythinker_code.tools.goal:UpdateGoal",
            "pythinker_code.tools.progress:Progress",
            "pythinker_code.tools.workflow:Workflow",
            "pythinker_code.tools.suggest:Suggest",
            "pythinker_code.tools.memory:Memory",
            "pythinker_code.tools.recall:Recall",
            "pythinker_code.tools.scratchpad:Scratchpad",
            "pythinker_code.tools.shell:Shell",
            "pythinker_code.tools.background:TaskList",
            "pythinker_code.tools.background:TaskOutput",
            "pythinker_code.tools.background:TaskInput",
            "pythinker_code.tools.background:TaskHandoff",
            "pythinker_code.tools.background:TaskStop",
            "pythinker_code.tools.file:ReadFile",
            "pythinker_code.tools.file:ReadMediaFile",
            "pythinker_code.tools.file:Glob",
            "pythinker_code.tools.file:Grep",
            "pythinker_code.tools.file:SmartSearch",
            "pythinker_code.tools.lsp:Lsp",
            "pythinker_code.tools.file:WriteFile",
            "pythinker_code.tools.file:StrReplaceFile",
            "pythinker_code.tools.web:SearchWeb",
            "pythinker_code.tools.web:FetchURL",
            "pythinker_code.tools.mcp_resource:ListMcpResources",
            "pythinker_code.tools.mcp_resource:ReadMcpResource",
            "pythinker_code.tools.mcp_resource:InvokeMcpPrompt",
            "pythinker_code.tools.plan:ExitPlanMode",
            "pythinker_code.tools.plan.enter:EnterPlanMode",
        ]
    )
    sub_subagents = {
        name: (spec.path.relative_to(DEFAULT_AGENT_FILE.parent).as_posix(), spec.description)
        for name, spec in subagent_specs["explore"].subagents.items()
    }
    assert sub_subagents == snapshot({})

    assert subagent_specs["plan"].name == snapshot("")
    assert subagent_specs["plan"].system_prompt_path == DEFAULT_AGENT_FILE.parent / "system_leaf.md"
    plan_prompt_args = subagent_specs["plan"].system_prompt_args
    assert plan_prompt_args["EMITS_CODING_ARTIFACT"] == ""
    plan_role = plan_prompt_args["ROLE_ADDITIONAL"]
    assert [line for line in plan_role.splitlines() if line.startswith("## ")] == [
        "## Mission",
        "## Hard Constraints",
        "## Context Gate",
        "## Workflow",
        "## Untrusted Content",
        "## Role Exit Checklist",
        "## Output Contract",
        "## Escalation",
    ]
    assert {
        (
            "You are a read-only planning and architecture specialist. Your output is an "
            "evidence-backed execution plan — the smallest set of tasks that fully achieves the "
            "stated goal, each executable as written — not a guess and not an implementation."
        ),
        "- You cannot edit files; report the plan, never apply it.",
        "- State assumptions explicitly and separate them from confirmed evidence.",
    } <= set(plan_role.splitlines())
    assert subagent_specs["plan"].when_to_use == snapshot(
        "Use this agent when the parent agent needs a step-by-step implementation plan, key file identification, and architectural trade-off analysis before code changes are made. It returns dependency-ordered, wave-parallelized tasks — each with artifacts, acceptance criteria, a specialist recommendation, and a proving verification — grounded in repository evidence and current third-party documentation.\n"
    )
    assert subagent_specs["plan"].model == snapshot(None)
    assert subagent_specs["plan"].allowed_tools == snapshot(
        [
            "pythinker_code.tools.todo:SetTodoList",
            "pythinker_code.tools.file:ReadFile",
            "pythinker_code.tools.file:ReadMediaFile",
            "pythinker_code.tools.file:Glob",
            "pythinker_code.tools.file:Grep",
            "pythinker_code.tools.file:SmartSearch",
            "pythinker_code.tools.skill:ReadSkill",
            "pythinker_code.tools.web:SearchWeb",
            "pythinker_code.tools.web:FetchURL",
        ]
    )
    assert subagent_specs["plan"].exclude_tools == snapshot(
        [
            "pythinker_code.tools.agent:Agent",
            "pythinker_code.tools.ask_user:AskUserQuestion",
            "pythinker_code.tools.plan:ExitPlanMode",
            "pythinker_code.tools.plan.enter:EnterPlanMode",
            "pythinker_code.tools.shell:Shell",
            "pythinker_code.tools.file:WriteFile",
            "pythinker_code.tools.file:StrReplaceFile",
        ]
    )
    assert subagent_specs["plan"].tools == snapshot(
        [
            "pythinker_code.tools.agent:Agent",
            "pythinker_code.tools.agent:RunAgents",
            "pythinker_code.tools.agent:ImplementAndJudge",
            "pythinker_code.tools.skill:ReadSkill",
            "pythinker_code.tools.ask_user:AskUserQuestion",
            "pythinker_code.tools.todo:SetTodoList",
            "pythinker_code.tools.tool_search:ToolSearch",
            "pythinker_code.tools.worktree:EnterWorktree",
            "pythinker_code.tools.worktree:ExitWorktree",
            "pythinker_code.tools.goal:UpdateGoal",
            "pythinker_code.tools.progress:Progress",
            "pythinker_code.tools.workflow:Workflow",
            "pythinker_code.tools.suggest:Suggest",
            "pythinker_code.tools.memory:Memory",
            "pythinker_code.tools.recall:Recall",
            "pythinker_code.tools.scratchpad:Scratchpad",
            "pythinker_code.tools.shell:Shell",
            "pythinker_code.tools.background:TaskList",
            "pythinker_code.tools.background:TaskOutput",
            "pythinker_code.tools.background:TaskInput",
            "pythinker_code.tools.background:TaskHandoff",
            "pythinker_code.tools.background:TaskStop",
            "pythinker_code.tools.file:ReadFile",
            "pythinker_code.tools.file:ReadMediaFile",
            "pythinker_code.tools.file:Glob",
            "pythinker_code.tools.file:Grep",
            "pythinker_code.tools.file:SmartSearch",
            "pythinker_code.tools.lsp:Lsp",
            "pythinker_code.tools.file:WriteFile",
            "pythinker_code.tools.file:StrReplaceFile",
            "pythinker_code.tools.web:SearchWeb",
            "pythinker_code.tools.web:FetchURL",
            "pythinker_code.tools.mcp_resource:ListMcpResources",
            "pythinker_code.tools.mcp_resource:ReadMcpResource",
            "pythinker_code.tools.mcp_resource:InvokeMcpPrompt",
            "pythinker_code.tools.plan:ExitPlanMode",
            "pythinker_code.tools.plan.enter:EnterPlanMode",
        ]
    )
    sub_subagents = {
        name: (spec.path.relative_to(DEFAULT_AGENT_FILE.parent).as_posix(), spec.description)
        for name, spec in subagent_specs["plan"].subagents.items()
    }
    assert sub_subagents == snapshot({})

    assert subagent_specs["planner"].name == snapshot("")
    assert (
        subagent_specs["planner"].system_prompt_path == DEFAULT_AGENT_FILE.parent / "system_leaf.md"
    )
    planner_prompt_args = subagent_specs["planner"].system_prompt_args
    assert planner_prompt_args["EMITS_CODING_ARTIFACT"] == ""
    planner_role = planner_prompt_args["ROLE_ADDITIONAL"]
    assert [line for line in planner_role.splitlines() if line.startswith("## ")] == [
        "## Mission",
        "## Hard Constraints",
        "## Partitioning Method",
        "## Self-Check Before Emitting",
        "## Untrusted Content",
        "## Output Contract",
    ]
    assert {
        (
            "You are a Reconnaissance Planner. Your single objective is to analyze the request, "
            "scout the repository just enough to partition it honestly, and break it down into N "
            "distinct, non-overlapping task seeds for parallel workers."
        ),
        "- Do not solve the problem. Do not write code. Do not fix anything.",
    } <= set(planner_role.splitlines())
    # Semantic invariants for the recon_seeds protocol contract.
    # Semantic invariants for the recon_seeds protocol contract.
    _planner_role = subagent_specs["planner"].system_prompt_args["ROLE_ADDITIONAL"]
    assert "<recon_seeds>" in _planner_role
    assert "ONLY" in _planner_role or "no preamble" in _planner_role.lower()
    assert "distinct" in _planner_role.lower() and "non-overlapping" in _planner_role.lower()
    assert subagent_specs["planner"].when_to_use == snapshot(
        """\
Use this agent before spawning N parallel workers on a large or open-ended task.
It scouts the repository cheaply, partitions the problem space along one decomposition
axis, and returns distinct, self-contained seeds so workers start from non-overlapping
vantage points. A single-seed result signals the task is not worth parallelizing.
"""
    )
    assert subagent_specs["planner"].model == snapshot(None)
    assert subagent_specs["planner"].allowed_tools == snapshot(
        [
            "pythinker_code.tools.shell:Shell",
            "pythinker_code.tools.file:ReadFile",
            "pythinker_code.tools.file:Glob",
            "pythinker_code.tools.file:Grep",
            "pythinker_code.tools.file:SmartSearch",
        ]
    )
    assert subagent_specs["planner"].exclude_tools == snapshot(
        [
            "pythinker_code.tools.agent:Agent",
            "pythinker_code.tools.ask_user:AskUserQuestion",
            "pythinker_code.tools.plan:ExitPlanMode",
            "pythinker_code.tools.plan.enter:EnterPlanMode",
            "pythinker_code.tools.file:WriteFile",
            "pythinker_code.tools.file:StrReplaceFile",
            "pythinker_code.tools.web:SearchWeb",
            "pythinker_code.tools.web:FetchURL",
        ]
    )
    assert subagent_specs["planner"].tools == snapshot(
        [
            "pythinker_code.tools.agent:Agent",
            "pythinker_code.tools.agent:RunAgents",
            "pythinker_code.tools.agent:ImplementAndJudge",
            "pythinker_code.tools.skill:ReadSkill",
            "pythinker_code.tools.ask_user:AskUserQuestion",
            "pythinker_code.tools.todo:SetTodoList",
            "pythinker_code.tools.tool_search:ToolSearch",
            "pythinker_code.tools.worktree:EnterWorktree",
            "pythinker_code.tools.worktree:ExitWorktree",
            "pythinker_code.tools.goal:UpdateGoal",
            "pythinker_code.tools.progress:Progress",
            "pythinker_code.tools.workflow:Workflow",
            "pythinker_code.tools.suggest:Suggest",
            "pythinker_code.tools.memory:Memory",
            "pythinker_code.tools.recall:Recall",
            "pythinker_code.tools.scratchpad:Scratchpad",
            "pythinker_code.tools.shell:Shell",
            "pythinker_code.tools.background:TaskList",
            "pythinker_code.tools.background:TaskOutput",
            "pythinker_code.tools.background:TaskInput",
            "pythinker_code.tools.background:TaskHandoff",
            "pythinker_code.tools.background:TaskStop",
            "pythinker_code.tools.file:ReadFile",
            "pythinker_code.tools.file:ReadMediaFile",
            "pythinker_code.tools.file:Glob",
            "pythinker_code.tools.file:Grep",
            "pythinker_code.tools.file:SmartSearch",
            "pythinker_code.tools.lsp:Lsp",
            "pythinker_code.tools.file:WriteFile",
            "pythinker_code.tools.file:StrReplaceFile",
            "pythinker_code.tools.web:SearchWeb",
            "pythinker_code.tools.web:FetchURL",
            "pythinker_code.tools.mcp_resource:ListMcpResources",
            "pythinker_code.tools.mcp_resource:ReadMcpResource",
            "pythinker_code.tools.mcp_resource:InvokeMcpPrompt",
            "pythinker_code.tools.plan:ExitPlanMode",
            "pythinker_code.tools.plan.enter:EnterPlanMode",
        ]
    )
    planner_sub = {
        name: (spec.path.relative_to(DEFAULT_AGENT_FILE.parent).as_posix(), spec.description)
        for name, spec in subagent_specs["planner"].subagents.items()
    }
    assert planner_sub == snapshot({})


def test_default_subagents_include_production_guardrail_gate():
    subagent_specs = {
        name: load_agent_spec(spec.path)
        for name, spec in load_agent_spec(DEFAULT_AGENT_FILE).subagents.items()
    }

    assert (
        "production guardrail gate"
        in subagent_specs["review"].system_prompt_args["ROLE_ADDITIONAL"]
    )
    assert (
        "cache stampedes" in subagent_specs["code-reviewer"].system_prompt_args["ROLE_ADDITIONAL"]
    )
    assert (
        "IDOR/tenant-scope mistakes"
        in subagent_specs["security-reviewer"].system_prompt_args["ROLE_ADDITIONAL"]
    )
    assert "Production guardrails" in subagent_specs["judge"].system_prompt_args["ROLE_ADDITIONAL"]


def test_load_agent_spec_basic(agent_file: Path):
    """Test loading a basic agent specification."""
    spec = load_agent_spec(agent_file)

    assert spec.name == snapshot("Test Agent")
    assert spec.system_prompt_path == agent_file.parent / "system.md"
    assert spec.tools == snapshot(["pythinker_code.tools.think:Think"])


def test_load_agent_spec_missing_name(agent_file_no_name: Path):
    """Test missing agent name raises AgentSpecError."""
    with pytest.raises(AgentSpecError, match="Agent name is required"):
        load_agent_spec(agent_file_no_name)


def test_load_agent_spec_missing_system_prompt(agent_file_no_prompt: Path):
    """Test missing system prompt path raises AgentSpecError."""
    with pytest.raises(AgentSpecError, match="System prompt path is required"):
        load_agent_spec(agent_file_no_prompt)


def test_load_agent_spec_missing_tools(agent_file_no_tools: Path):
    """Test missing tools raises AgentSpecError."""
    with pytest.raises(AgentSpecError, match="Tools are required"):
        load_agent_spec(agent_file_no_tools)


def test_load_agent_spec_with_exclude_tools(agent_file_with_tools: Path):
    """Test loading agent spec with excluded tools."""
    spec = load_agent_spec(agent_file_with_tools)

    assert spec.tools == snapshot(
        ["pythinker_code.tools.think:Think", "pythinker_code.tools.shell:Shell"]
    )
    assert spec.exclude_tools == snapshot(["pythinker_code.tools.shell:Shell"])


def test_load_agent_spec_extension(agent_file_extending: Path):
    """Test loading agent spec with extension."""
    spec = load_agent_spec(agent_file_extending)

    assert spec.name == snapshot("Extended Agent")
    assert spec.tools == snapshot(["pythinker_code.tools.think:Think"])


def test_load_agent_spec_metadata_inherits_and_overrides(tmp_path: Path):
    (tmp_path / "system.md").write_text("Base system prompt")
    base = tmp_path / "base.yaml"
    base.write_text(
        """
version: 1
agent:
  name: "Base Agent"
  system_prompt_path: ./system.md
  tools: ["pythinker_code.tools.think:Think"]
  mode: subagent
  hidden: true
  steps: 7
  temperature: 0.2
  top_p: 0.8
""".strip()
    )
    child = tmp_path / "child.yaml"
    child.write_text(
        """
version: 1
agent:
  extend: ./base.yaml
  name: "Child Agent"
  mode: all
  hidden: false
  steps: 3
""".strip()
    )

    spec = load_agent_spec(child)

    assert spec.name == "Child Agent"
    assert spec.mode == "all"
    assert spec.hidden is False
    assert spec.steps == 3
    assert spec.temperature == 0.2
    assert spec.top_p == 0.8


def test_load_agent_spec_default_extension():
    """Test loading agent spec with default extension."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create extending agent
        extending_agent = tmpdir / "extending.yaml"
        extending_agent.write_text("""
version: 1
agent:
  extend: default
  system_prompt_args:
    CUSTOM_ARG: "custom_value"
  exclude_tools:
    - "pythinker_code.tools.web:SearchWeb"
    - "pythinker_code.tools.web:FetchURL"
""")

        spec = load_agent_spec(extending_agent)

        assert spec.name == snapshot("")
        assert spec.system_prompt_path == DEFAULT_AGENT_FILE.parent / "system.md"
        assert spec.system_prompt_args == snapshot(
            {"ROLE_ADDITIONAL": "", "EMITS_CODING_ARTIFACT": "", "CUSTOM_ARG": "custom_value"}
        )
        assert spec.tools == snapshot(
            [
                "pythinker_code.tools.agent:Agent",
                "pythinker_code.tools.agent:RunAgents",
                "pythinker_code.tools.agent:ImplementAndJudge",
                "pythinker_code.tools.skill:ReadSkill",
                "pythinker_code.tools.ask_user:AskUserQuestion",
                "pythinker_code.tools.todo:SetTodoList",
                "pythinker_code.tools.tool_search:ToolSearch",
                "pythinker_code.tools.worktree:EnterWorktree",
                "pythinker_code.tools.worktree:ExitWorktree",
                "pythinker_code.tools.goal:UpdateGoal",
                "pythinker_code.tools.progress:Progress",
                "pythinker_code.tools.workflow:Workflow",
                "pythinker_code.tools.suggest:Suggest",
                "pythinker_code.tools.memory:Memory",
                "pythinker_code.tools.recall:Recall",
                "pythinker_code.tools.scratchpad:Scratchpad",
                "pythinker_code.tools.shell:Shell",
                "pythinker_code.tools.background:TaskList",
                "pythinker_code.tools.background:TaskOutput",
                "pythinker_code.tools.background:TaskInput",
                "pythinker_code.tools.background:TaskHandoff",
                "pythinker_code.tools.background:TaskStop",
                "pythinker_code.tools.file:ReadFile",
                "pythinker_code.tools.file:ReadMediaFile",
                "pythinker_code.tools.file:Glob",
                "pythinker_code.tools.file:Grep",
                "pythinker_code.tools.file:SmartSearch",
                "pythinker_code.tools.lsp:Lsp",
                "pythinker_code.tools.file:WriteFile",
                "pythinker_code.tools.file:StrReplaceFile",
                "pythinker_code.tools.web:SearchWeb",
                "pythinker_code.tools.web:FetchURL",
                "pythinker_code.tools.mcp_resource:ListMcpResources",
                "pythinker_code.tools.mcp_resource:ReadMcpResource",
                "pythinker_code.tools.mcp_resource:InvokeMcpPrompt",
                "pythinker_code.tools.plan:ExitPlanMode",
                "pythinker_code.tools.plan.enter:EnterPlanMode",
            ]
        )
        assert spec.exclude_tools == snapshot(
            ["pythinker_code.tools.web:SearchWeb", "pythinker_code.tools.web:FetchURL"]
        )
        assert "coder" in spec.subagents


def test_load_agent_spec_unsupported_version():
    """Test loading agent spec with unsupported version raises ValueError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        agent_yaml = tmpdir / "agent.yaml"
        agent_yaml.write_text("""
version: 2
agent:
  name: "Test Agent"
  system_prompt_path: ./system.md
  tools: ["pythinker_code.tools.think:Think"]
""")

        with pytest.raises(AgentSpecError, match="Unsupported agent spec version: 2"):
            load_agent_spec(agent_yaml)


def test_load_agent_spec_nonexistent_file():
    """Test loading nonexistent agent spec file raises AssertionError."""
    nonexistent = Path("/nonexistent/agent.yaml")
    with pytest.raises(
        AgentSpecError,
        match=re.compile(r"Agent spec file not found: [\\/]nonexistent[\\/]agent.yaml"),
    ):
        load_agent_spec(nonexistent)


def test_load_agent_spec_empty_yaml_raises_agent_spec_error(tmp_path: Path):
    agent_yaml = tmp_path / "agent.yaml"
    agent_yaml.write_text("")

    with pytest.raises(AgentSpecError, match="Agent spec file must contain a mapping"):
        load_agent_spec(agent_yaml)


def test_load_agent_spec_non_mapping_yaml_raises_agent_spec_error(tmp_path: Path):
    agent_yaml = tmp_path / "agent.yaml"
    agent_yaml.write_text("- not\n- a\n- mapping\n")

    with pytest.raises(AgentSpecError, match="Agent spec file must contain a mapping"):
        load_agent_spec(agent_yaml)


# Fixtures for test files


@pytest.fixture
def agent_file() -> Generator[Path, Any, Any]:
    """Create a basic agent configuration file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create system.md
        system_md = tmpdir / "system.md"
        system_md.write_text("You are a test agent")

        # Create agent.yaml
        agent_yaml = tmpdir / "agent.yaml"
        agent_yaml.write_text("""
version: 1
agent:
  name: "Test Agent"
  system_prompt_path: ./system.md
  tools: ["pythinker_code.tools.think:Think"]
""")

        yield agent_yaml


@pytest.fixture
def agent_file_no_name() -> Generator[Path, Any, Any]:
    """Create an agent configuration file without name."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create system.md
        system_md = tmpdir / "system.md"
        system_md.write_text("You are a test agent")

        # Create agent.yaml
        agent_yaml = tmpdir / "agent.yaml"
        agent_yaml.write_text("""
version: 1
agent:
  system_prompt_path: ./system.md
  tools: ["pythinker_code.tools.think:Think"]
""")

        yield agent_yaml


@pytest.fixture
def agent_file_no_prompt() -> Generator[Path, Any, Any]:
    """Create an agent configuration file without system prompt path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create agent.yaml
        agent_yaml = tmpdir / "agent.yaml"
        agent_yaml.write_text("""
version: 1
agent:
  name: "Test Agent"
  tools: ["pythinker_code.tools.think:Think"]
""")

        yield agent_yaml


@pytest.fixture
def agent_file_no_tools() -> Generator[Path, Any, Any]:
    """Create an agent configuration file without tools."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create system.md
        system_md = tmpdir / "system.md"
        system_md.write_text("You are a test agent")

        # Create agent.yaml
        agent_yaml = tmpdir / "agent.yaml"
        agent_yaml.write_text("""
version: 1
agent:
  name: "Test Agent"
  system_prompt_path: ./system.md
""")

        yield agent_yaml


@pytest.fixture
def agent_file_with_tools() -> Generator[Path, Any, Any]:
    """Create an agent configuration file with tools and exclude_tools."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create system.md
        system_md = tmpdir / "system.md"
        system_md.write_text("You are a test agent")

        # Create agent.yaml
        agent_yaml = tmpdir / "agent.yaml"
        agent_yaml.write_text("""
version: 1
agent:
  name: "Test Agent"
  system_prompt_path: ./system.md
  tools: ["pythinker_code.tools.think:Think", "pythinker_code.tools.shell:Shell"]
  exclude_tools: ["pythinker_code.tools.shell:Shell"]
""")

        yield agent_yaml


@pytest.fixture
def agent_file_extending() -> Generator[Path, Any, Any]:
    """Create an agent configuration file that extends another."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create base agent
        base_agent = tmpdir / "base.yaml"
        base_agent.write_text("""
version: 1
agent:
  name: "Base Agent"
  system_prompt_path: ./system.md
  tools: ["pythinker_code.tools.think:Think"]
""")

        # Create system.md
        system_md = tmpdir / "system.md"
        system_md.write_text("Base system prompt")

        # Create extending agent
        extending_agent = tmpdir / "extending.yaml"
        extending_agent.write_text("""
version: 1
agent:
  extend: ./base.yaml
  name: "Extended Agent"
  system_prompt_args:
    CUSTOM_ARG: "custom_value"
""")

        yield extending_agent


def test_subagent_extension_merges_subagents_dicts(tmp_path: Path):
    """Extending an agent with a new subagent type should ADD to, not replace, base subagents."""
    system_md = tmp_path / "system.md"
    system_md.write_text("Base system prompt")

    base_yaml = tmp_path / "base.yaml"
    base_yaml.write_text("""
version: 1
agent:
  name: "Base"
  system_prompt_path: ./system.md
  tools: ["pythinker_code.tools.think:Think"]
  subagents:
    alpha:
      path: ./alpha.yaml
      description: "Alpha agent"
    beta:
      path: ./beta.yaml
      description: "Beta agent"
""")

    (tmp_path / "alpha.yaml").write_text(
        "version: 1\nagent:\n  name: alpha\n  system_prompt_path: ./system.md\n  tools: []\n"
    )
    (tmp_path / "beta.yaml").write_text(
        "version: 1\nagent:\n  name: beta\n  system_prompt_path: ./system.md\n  tools: []\n"
    )
    (tmp_path / "gamma.yaml").write_text(
        "version: 1\nagent:\n  name: gamma\n  system_prompt_path: ./system.md\n  tools: []\n"
    )

    child_yaml = tmp_path / "child.yaml"
    child_yaml.write_text("""
version: 1
agent:
  extend: ./base.yaml
  name: "Child"
  subagents:
    gamma:
      path: ./gamma.yaml
      description: "Gamma agent"
""")

    spec = load_agent_spec(child_yaml)

    # Child adds gamma; base alpha and beta must still be present.
    assert set(spec.subagents.keys()) == {"alpha", "beta", "gamma"}
    assert spec.subagents["gamma"].description == snapshot("Gamma agent")
    assert spec.subagents["alpha"].description == snapshot("Alpha agent")


def test_subagent_extension_child_overrides_base_entry(tmp_path: Path):
    """Child subagent with same name as base overwrites only that entry."""
    system_md = tmp_path / "system.md"
    system_md.write_text("prompt")

    base_yaml = tmp_path / "base.yaml"
    base_yaml.write_text("""
version: 1
agent:
  name: "Base"
  system_prompt_path: ./system.md
  tools: ["pythinker_code.tools.think:Think"]
  subagents:
    coder:
      path: ./coder_v1.yaml
      description: "Original coder"
    reviewer:
      path: ./reviewer.yaml
      description: "Reviewer"
""")
    for name in ("coder_v1", "coder_v2", "reviewer"):
        (tmp_path / f"{name}.yaml").write_text(
            f"version: 1\nagent:\n  name: {name}\n  system_prompt_path: ./system.md\n  tools: []\n"
        )

    child_yaml = tmp_path / "child.yaml"
    child_yaml.write_text("""
version: 1
agent:
  extend: ./base.yaml
  name: "Child"
  subagents:
    coder:
      path: ./coder_v2.yaml
      description: "Upgraded coder"
""")

    spec = load_agent_spec(child_yaml)

    assert set(spec.subagents.keys()) == {"coder", "reviewer"}
    assert spec.subagents["coder"].description == snapshot("Upgraded coder")
    assert spec.subagents["reviewer"].description == snapshot("Reviewer")


def test_cyclic_extend_chain_raises_agent_spec_error(tmp_path: Path) -> None:
    """Self-extending and mutually-extending specs must raise AgentSpecError, not RecursionError."""
    # Case 1: self-extend (a.yaml extends itself)
    self_yaml = tmp_path / "self.yaml"
    self_yaml.write_text('version: "1"\nagent:\n  extend: self.yaml\n  name: x\n')

    with pytest.raises(AgentSpecError, match="Cyclic"):
        load_agent_spec(self_yaml)

    # Case 2: mutual cycle (a.yaml extends b.yaml, b.yaml extends a.yaml)
    a_yaml = tmp_path / "a.yaml"
    b_yaml = tmp_path / "b.yaml"
    a_yaml.write_text('version: "1"\nagent:\n  extend: b.yaml\n  name: a\n')
    b_yaml.write_text('version: "1"\nagent:\n  extend: a.yaml\n  name: b\n')

    with pytest.raises(AgentSpecError, match="Cyclic"):
        load_agent_spec(a_yaml)
