from pathlib import Path
from typing import override

from pydantic import BaseModel, Field
from pythinker_core.tooling import CallableTool2, ToolOk, ToolReturnValue

from pythinker_code.soul.toolset import PythinkerToolset
from pythinker_code.tools.utils import load_desc


class Params(BaseModel):
    query: str = Field(description="Keywords to search for in visible tool names and descriptions.")
    max_results: int = Field(
        default=8,
        ge=1,
        le=25,
        description="Maximum number of matching tools to return.",
    )


def _score_tool(name: str, description: str, terms: list[str]) -> int:
    haystack_name = name.lower()
    haystack_desc = description.lower()
    score = 0
    for term in terms:
        if term in haystack_name:
            score += 4
        if term in haystack_desc:
            score += 1
    return score


def _short_description(description: str, *, limit: int = 160) -> str:
    compact = " ".join(description.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


class ToolSearch(CallableTool2[Params]):
    name: str = "ToolSearch"
    description: str = load_desc(Path(__file__).parent / "tool_search.md")
    params: type[Params] = Params
    supports_parallel: bool = True

    def __init__(self, toolset: PythinkerToolset) -> None:
        super().__init__()
        self._toolset = toolset

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        query = " ".join(params.query.split())
        if not query:
            return ToolOk(output="No visible tools matched an empty query.")

        terms = [term.lower() for term in query.split()]
        matches: list[tuple[int, str, str]] = []
        for tool in self._toolset.tools:
            score = _score_tool(tool.name, tool.description or "", terms)
            if score:
                matches.append((score, tool.name, tool.description or "No description provided."))

        if not matches:
            return ToolOk(output=f"No visible tools matched `{query}`.")

        matches.sort(key=lambda item: (-item[0], item[1].lower()))
        lines = [
            f"- {name} - {_short_description(description)}"
            for _, name, description in matches[: params.max_results]
        ]
        return ToolOk(
            output="\n".join(lines),
            message=f"Found {len(lines)} visible tool match(es) for `{query}`.",
        )
