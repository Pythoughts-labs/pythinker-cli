"""Prompt-level invariants for the typed coding-artifact contract."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from pythinker_host.path import HostPath

from pythinker_code.agentspec import load_agent_spec
from pythinker_code.soul.agent import BuiltinSystemPromptArgs, _load_system_prompt
from pythinker_code.utils.artifacts import CodingArtifact, coding_artifact_contract_block

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AGENT_DIRECTORY = REPOSITORY_ROOT / "src" / "pythinker_code" / "agents" / "default"
SUBAGENT_PREAMBLE = """\
You are now running as a subagent. All the `user` messages are sent by the main agent. The main agent cannot see your context, it can only see your last message when you finish the task. You must treat the parent agent as your caller. Do not directly ask the end user questions. If something is unclear, explain the ambiguity in your final summary to the parent agent."""


@pytest.mark.parametrize("agent_filename", ["implementer.yaml", "coder.yaml"])
def test_coding_roles_render_generated_contract(agent_filename: str) -> None:
    spec = load_agent_spec(DEFAULT_AGENT_DIRECTORY / agent_filename)
    builtin_values = {
        field.name: f"<<{field.name}>>" for field in dataclasses.fields(BuiltinSystemPromptArgs)
    }
    builtin_values["PYTHINKER_OS"] = "macOS"
    builtin_args = BuiltinSystemPromptArgs(
        PYTHINKER_NOW=builtin_values["PYTHINKER_NOW"],
        PYTHINKER_WORK_DIR=HostPath(builtin_values["PYTHINKER_WORK_DIR"]),
        PYTHINKER_WORK_DIR_LS=builtin_values["PYTHINKER_WORK_DIR_LS"],
        PYTHINKER_AGENTS_MD=builtin_values["PYTHINKER_AGENTS_MD"],
        PYTHINKER_SKILLS=builtin_values["PYTHINKER_SKILLS"],
        PYTHINKER_ADDITIONAL_DIRS_INFO=builtin_values["PYTHINKER_ADDITIONAL_DIRS_INFO"],
        PYTHINKER_OS=builtin_values["PYTHINKER_OS"],
        PYTHINKER_SHELL=builtin_values["PYTHINKER_SHELL"],
        PYTHINKER_SCRATCHPAD_SECTION=builtin_values["PYTHINKER_SCRATCHPAD_SECTION"],
        PYTHINKER_AGENTS_MD_FENCE=builtin_values["PYTHINKER_AGENTS_MD_FENCE"],
    )

    assert spec.system_prompt_path.name == "system_leaf.md"
    assert spec.system_prompt_args["EMITS_CODING_ARTIFACT"]

    prompt = _load_system_prompt(spec.system_prompt_path, spec.system_prompt_args, builtin_args)

    assert prompt.count(coding_artifact_contract_block()) == 1
    assert prompt.count(SUBAGENT_PREAMBLE) == 1


def test_verifier_names_every_coding_artifact_field() -> None:
    verifier_text = (DEFAULT_AGENT_DIRECTORY / "verifier.yaml").read_text(encoding="utf-8")

    for artifact_field in dataclasses.fields(CodingArtifact):
        assert artifact_field.name in verifier_text
