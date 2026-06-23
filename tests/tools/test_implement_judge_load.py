"""Verify `ImplementAndJudgeTool` loads through the same path as `AgentTool`.

This is the regression test for the CLI startup error caused by adding the
new tool to the default agent's `tools:` list — the loader would reject
`ImplementAndJudge` because the class could not be instantiated through
the toolset's `_load_tool` dependency-injection path.
"""

from __future__ import annotations

from pythinker_code.soul.agent import Runtime
from pythinker_code.soul.toolset import PythinkerToolset
from pythinker_code.tools.agent import AgentTool, ImplementAndJudgeTool


def test_implement_judge_loads_via_toolset(runtime: Runtime) -> None:
    """Same loader path AgentTool uses — must succeed end-to-end."""
    toolset = PythinkerToolset()
    tool_deps = {PythinkerToolset: toolset, Runtime: runtime}
    toolset.load_tools(
        [
            "pythinker_code.tools.agent:Agent",
            "pythinker_code.tools.agent:RunAgents",
            "pythinker_code.tools.agent:ImplementAndJudge",
        ],
        tool_deps,
    )
    types = [type(t) for t in toolset._tool_dict.values()]
    assert AgentTool in types
    assert ImplementAndJudgeTool in types
