from __future__ import annotations

from dataclasses import fields

from pythinker_host.path import HostPath

from pythinker_code.agentspec import DEFAULT_AGENT_FILE
from pythinker_code.soul.agent import BuiltinSystemPromptArgs, _load_system_prompt
from pythinker_code.utils.artifacts import coding_artifact_contract_block


def test_leaf_prompt_renders_optional_artifact_contract() -> None:
    builtin_values = {field.name: f"<<{field.name}>>" for field in fields(BuiltinSystemPromptArgs)}
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
    prompt_path = DEFAULT_AGENT_FILE.parent / "system_leaf.md"

    without_artifact = _load_system_prompt(
        prompt_path,
        {"ROLE_ADDITIONAL": "ROLE-MARKER", "EMITS_CODING_ARTIFACT": ""},
        builtin_args,
    )
    with_artifact = _load_system_prompt(
        prompt_path,
        {"ROLE_ADDITIONAL": "ROLE-MARKER", "EMITS_CODING_ARTIFACT": "true"},
        builtin_args,
    )

    for prompt in (without_artifact, with_artifact):
        prescribed_sections = [
            "**Product identity is absolute.**",
            "You are now running as a subagent.",
            "ROLE-MARKER",
            "## 2. Core Rules",
            "## Tools",
            "**Act with tools; prose is not action.**",
            "Batch independent reads, searches, and checks into one turn",
            "**Spend context deliberately.**",
            "**Verify results you act on.**",
            builtin_values["PYTHINKER_SCRATCHPAD_SECTION"],
            "## 6. Code Standards",
            "## 7. Untrusted Content & Instruction Authority",
            "## 8. Communication & Output",
            "## 9. Definition of Done",
            "## 10. Environment",
            "## 11. Project Instructions (AGENTS.md)",
            "## 12. Skills",
        ]
        section_offsets = [prompt.index(section) for section in prescribed_sections]
        assert section_offsets == sorted(section_offsets)
        assert "## 3. Operating Loop" not in prompt
        assert "## 4. Playbooks" not in prompt
        assert "RunAgents" not in prompt

    contract = coding_artifact_contract_block()
    assert "## Artifact Contract" not in without_artifact
    assert contract not in without_artifact
    assert "## Artifact Contract" in with_artifact
    assert contract in with_artifact
