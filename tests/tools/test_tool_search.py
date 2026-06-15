from __future__ import annotations

from pydantic import BaseModel
from pythinker_core.tooling import CallableTool2, ToolOk, ToolReturnValue

from pythinker_code.soul.toolset import PythinkerToolset
from pythinker_code.tools.tool_search import Params, ToolSearch


class _Params(BaseModel):
    value: str = ""


class ReadProjectFiles(CallableTool2[_Params]):
    name: str = "ReadProjectFiles"
    description: str = "Read files from the current workspace"
    params: type[_Params] = _Params

    async def __call__(self, params: _Params) -> ToolReturnValue:
        return ToolOk(output=params.value)


class RunProjectCommand(CallableTool2[_Params]):
    name: str = "RunProjectCommand"
    description: str = "Execute a command in the current workspace"
    params: type[_Params] = _Params

    async def __call__(self, params: _Params) -> ToolReturnValue:
        return ToolOk(output=params.value)


def _tool_search(toolset: PythinkerToolset) -> ToolSearch:
    return ToolSearch(toolset)


async def test_tool_search_matches_tool_name() -> None:
    toolset = PythinkerToolset()
    toolset.add(ReadProjectFiles())
    toolset.add(RunProjectCommand())

    result = await _tool_search(toolset)(Params(query="read"))

    assert not result.is_error
    assert "ReadProjectFiles - Read files from the current workspace" in result.output
    assert "RunProjectCommand" not in result.output


async def test_tool_search_matches_description() -> None:
    toolset = PythinkerToolset()
    toolset.add(ReadProjectFiles())
    toolset.add(RunProjectCommand())

    result = await _tool_search(toolset)(Params(query="execute command"))

    assert not result.is_error
    assert "RunProjectCommand - Execute a command in the current workspace" in result.output


async def test_tool_search_reports_no_matches() -> None:
    toolset = PythinkerToolset()
    toolset.add(ReadProjectFiles())

    result = await _tool_search(toolset)(Params(query="browser"))

    assert not result.is_error
    assert result.output == "No visible tools matched `browser`."


async def test_tool_search_excludes_hidden_tools() -> None:
    toolset = PythinkerToolset()
    toolset.add(ReadProjectFiles())
    toolset.add(RunProjectCommand())
    toolset.hide("ReadProjectFiles")

    result = await _tool_search(toolset)(Params(query="read files"))

    assert not result.is_error
    assert "ReadProjectFiles" not in result.output
    assert result.output == "No visible tools matched `read files`."
