from pathlib import Path
from typing import override

from pydantic import BaseModel, Field
from pythinker_core.tooling import CallableTool2, ToolError, ToolReturnValue

from pythinker_code.skill import read_skill_text_with_local_specialization
from pythinker_code.soul.agent import Runtime
from pythinker_code.tools.skill._mcp_bridge import (
    find_mcp_server_for_skill_name,
    mcp_skill_bridge_content,
    skill_lookup_keys,
)
from pythinker_code.tools.utils import load_desc

NAME = "ReadSkill"


class Params(BaseModel):
    skill_name: str = Field(description="Name of the skill to read, for example review-pr")


class ReadSkillTool(CallableTool2[Params]):
    name: str = NAME
    params: type[Params] = Params

    def __init__(self, runtime: Runtime):
        super().__init__(description=load_desc(Path(__file__).parent / "description.md"))
        self._runtime = runtime

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        skill_name = params.skill_name.strip()
        if not skill_name:
            return ToolError(message="Skill name is required.", brief="Missing skill name")

        lookup_keys = skill_lookup_keys(skill_name)
        skill = self._runtime.skill_catalog.resolve(skill_name)
        if skill is None:
            for key in lookup_keys:
                skill = self._runtime.skills.get(key)
                if skill is not None:
                    break

        mcp_match = find_mcp_server_for_skill_name(skill_name, self._runtime.mcp_tools)

        if skill is None:
            diagnostic = self._runtime.skill_catalog.unavailable_diagnostic(skill_name)
            if diagnostic is not None:
                return ToolError(
                    message=(
                        f"status: unavailable\nSkill unavailable: {skill_name}. "
                        f"{diagnostic.safe_reason}"
                    ),
                    brief="Skill unavailable",
                )
            if mcp_match is not None:
                server, tools = mcp_match
                content = mcp_skill_bridge_content(server, tools)
                return ToolReturnValue(
                    is_error=False,
                    output=f"status: mcp_fallback\nskill: {server} (MCP bridge)\n\n{content}",
                    message=f"Resolved {skill_name} to MCP server {server}.",
                    display=[],
                )

            suggestions = self._runtime.skill_catalog.search(skill_name, limit=5)
            suggestion_text = ", ".join(match.skill.name for match in suggestions) or "(none)"
            mcp_hint = ""
            if mcp_match is None and self._runtime.mcp_tools:
                servers = sorted(
                    {
                        key.split("__", 2)[1]
                        for key in self._runtime.mcp_tools
                        if key.startswith("mcp__") and key.count("__") >= 2
                    }
                )
                if servers:
                    mcp_hint = f" Connected MCP servers: {', '.join(servers)}."
            return ToolError(
                message=(
                    f"status: not_found\nSkill not found: {skill_name}. "
                    f"Suggestions: {suggestion_text}.{mcp_hint}"
                ),
                brief="Skill not found",
            )

        content = await read_skill_text_with_local_specialization(skill, self._runtime.skills)
        if content is None:
            return ToolError(
                message=f"Failed to read skill: {skill.name}", brief="Skill read failed"
            )

        return ToolReturnValue(
            is_error=False,
            output=f"skill: {skill.name}\npath: {skill.skill_md_file}\n\n{content}",
            message=f"Read skill {skill.name}.",
            display=[],
        )


ReadSkill = ReadSkillTool
