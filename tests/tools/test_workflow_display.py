from pythinker_code.tools.workflow.display import WorkflowSnapshot, render_progress


def test_snapshot_lifecycle_and_render():
    snap = WorkflowSnapshot(name="inspect", description="d")
    snap.add_phase("Scan")
    a = snap.start_agent(1, "repo inventory", "Scan")
    assert a.status == "running"
    assert snap.running_count == 1
    snap.end_agent(1)
    assert snap.done_count == 1 and snap.running_count == 0

    snap.add_phase("Analyze")
    snap.start_agent(2, "modules", "Analyze")
    text = render_progress(snap)
    assert "Workflow: inspect" in text
    assert "Scan" in text and "Analyze" in text
    assert "repo inventory" in text


def test_mark_running_skipped():
    snap = WorkflowSnapshot(name="n", description="d")
    snap.start_agent(1, "a", None)
    snap.start_agent(2, "b", None)
    snap.end_agent(1)
    snap.mark_running_skipped()
    assert snap.done_count == 1
    assert snap.skipped_count == 1


def test_end_agent_does_not_swap_status_on_duplicate_labels():
    # Regression guard for CodeRabbit finding: two concurrent agents sharing the
    # same explicit label must not have their completion statuses swapped when
    # the SECOND-started one finishes (errors) before the FIRST-started one.
    snap = WorkflowSnapshot(name="n", description="d")
    first = snap.start_agent(1, "scan", None)
    second = snap.start_agent(2, "scan", None)
    snap.end_agent(2, error="boom")  # the second-started agent fails first
    assert first.status == "running"
    assert second.status == "error"
    snap.end_agent(1)  # the first-started agent finishes after
    assert first.status == "done"
    assert second.status == "error"


def test_render_progress_shows_recent_log_messages():
    snap = WorkflowSnapshot(name="n", description="d")
    snap.logs.append("first checkpoint")
    snap.logs.append("second checkpoint")
    text = render_progress(snap)
    assert "first checkpoint" in text
    assert "second checkpoint" in text


def test_render_progress_truncated_phase_agents_are_not_duplicated():
    # Regression guard: agents cut by the per-phase max_agents tail must stay
    # truncated, not reappear at the bottom as "unphased" rows.
    snap = WorkflowSnapshot(name="n", description="d")
    for i in range(1, 9):
        snap.start_agent(i, f"scan {i}", "Scan")
        snap.end_agent(i)
    text = render_progress(snap, max_agents=6)
    assert "#1 " not in text
    assert "#2 " not in text
    assert text.count("#8 ") == 1


def test_render_progress_still_shows_phaseless_agents():
    snap = WorkflowSnapshot(name="n", description="d")
    snap.start_agent(1, "loner", None)
    text = render_progress(snap)
    assert "loner" in text


def test_render_progress_truncates_to_max_logs():
    snap = WorkflowSnapshot(name="n", description="d")
    for i in range(5):
        snap.logs.append(f"log {i}")
    text = render_progress(snap, max_logs=2)
    assert "log 3" in text
    assert "log 4" in text
    assert "log 0" not in text
