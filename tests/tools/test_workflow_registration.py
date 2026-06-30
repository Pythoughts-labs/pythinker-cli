import importlib


def test_workflow_tool_string_resolves():
    mod = importlib.import_module("pythinker_code.tools.workflow")
    assert hasattr(mod, "Workflow")
    assert mod.Workflow.name == "Workflow"


def test_workflow_registered_in_default_spec():
    from pathlib import Path

    import yaml

    spec_path = (
        Path(__file__).resolve().parents[2]
        / "src/pythinker_code/agents/default/agent.yaml"
    )
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    tools = spec["agent"]["tools"]
    assert "pythinker_code.tools.workflow:Workflow" in tools
    # Must NOT be wired into child specs (no nesting).
    coder = yaml.safe_load(
        (spec_path.parent / "coder.yaml").read_text(encoding="utf-8")
    )
    coder_tools = coder.get("agent", {}).get("tools", []) if isinstance(coder, dict) else []
    assert "pythinker_code.tools.workflow:Workflow" not in coder_tools
