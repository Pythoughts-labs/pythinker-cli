import pytest

from pythinker_code.tools.workflow.engine import (
    WorkflowScriptError,
    parse_workflow_script,
)

GOOD = '''meta = {"name": "inspect", "description": "Inspect repo", "phases": [{"title": "Scan"}]}
phase("Scan")
inventory = await agent("Inspect the repository.", {"label": "repo inventory"})
return {"inventory": inventory}
'''


def test_parse_accepts_valid_script():
    meta, body = parse_workflow_script(GOOD)
    assert meta.name == "inspect"
    assert meta.description == "Inspect repo"
    assert meta.phases[0].title == "Scan"
    # meta statement is stripped; body keeps the rest.
    assert len(body) == 3


@pytest.mark.parametrize(
    "script, fragment",
    [
        ('phase("x")\n', "first statement"),
        ('meta = compute()\nawait agent("x")\n', "literal dict"),
        ('meta = {"name": "", "description": "d"}\nawait agent("x")\n', "name"),
        ('meta = {"name": "n", "description": ""}\nawait agent("x")\n', "description"),
        ('meta = {"name": "n", "description": "d"}\nimport os\n', "not allowed"),
        ('meta = {"name": "n", "description": "d"}\nx = random.random()\n', "deterministic"),
        ('meta = {"name": "n", "description": "d"}\nx = time.time()\n', "deterministic"),
    ],
)
def test_parse_rejects(script, fragment):
    with pytest.raises(WorkflowScriptError) as exc:
        parse_workflow_script(script)
    assert fragment in str(exc.value)
