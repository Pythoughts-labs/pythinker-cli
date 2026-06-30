from pythinker_code.tools.workflow.display import WorkflowSnapshot, render_progress


def test_snapshot_lifecycle_and_render():
    snap = WorkflowSnapshot(name="inspect", description="d")
    snap.add_phase("Scan")
    a = snap.start_agent("repo inventory", "Scan")
    assert a.status == "running"
    assert snap.running_count == 1
    snap.end_agent("repo inventory")
    assert snap.done_count == 1 and snap.running_count == 0

    snap.add_phase("Analyze")
    snap.start_agent("modules", "Analyze")
    text = render_progress(snap)
    assert "Workflow: inspect" in text
    assert "Scan" in text and "Analyze" in text
    assert "repo inventory" in text


def test_mark_running_skipped():
    snap = WorkflowSnapshot(name="n", description="d")
    snap.start_agent("a", None)
    snap.start_agent("b", None)
    snap.end_agent("a")
    snap.mark_running_skipped()
    assert snap.done_count == 1
    assert snap.skipped_count == 1
