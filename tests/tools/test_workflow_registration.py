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
    # Must NOT be wired into child specs (no nesting). Resolve the coder subagent
    # spec through the real agentspec loader (extend + allowed_tools applied),
    # not raw YAML — coder.yaml has no literal `tools:` key, so a raw-YAML
    # check would vacuously pass regardless of what allowed_tools contains.
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE, load_agent_spec

    root_spec = load_agent_spec(DEFAULT_AGENT_FILE)
    coder_spec = load_agent_spec(root_spec.subagents["coder"].path)
    effective_coder_tools = (
        coder_spec.allowed_tools if coder_spec.allowed_tools is not None else coder_spec.tools
    )
    assert "pythinker_code.tools.workflow:Workflow" not in effective_coder_tools
