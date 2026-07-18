"""Prompt-level invariants for the typed coding-artifact contract."""

from __future__ import annotations

import dataclasses
import textwrap
from pathlib import Path

import pytest

from pythinker_code.utils.artifacts import CodingArtifact, coding_artifact_contract_block

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AGENT_DIRECTORY = REPOSITORY_ROOT / "src" / "pythinker_code" / "agents" / "default"


@pytest.mark.parametrize("agent_filename", ["implementer.yaml", "coder.yaml"])
def test_coding_roles_embed_generated_contract_verbatim(agent_filename: str) -> None:
    agent_text = (DEFAULT_AGENT_DIRECTORY / agent_filename).read_text(encoding="utf-8")
    role_additional_source = agent_text.split("    ROLE_ADDITIONAL: |\n", maxsplit=1)[1].split(
        "\n  when_to_use:", maxsplit=1
    )[0]
    indented_contract = textwrap.indent(coding_artifact_contract_block(), "      ")

    assert indented_contract in role_additional_source


def test_verifier_names_every_coding_artifact_field() -> None:
    verifier_text = (DEFAULT_AGENT_DIRECTORY / "verifier.yaml").read_text(encoding="utf-8")

    for artifact_field in dataclasses.fields(CodingArtifact):
        assert artifact_field.name in verifier_text
