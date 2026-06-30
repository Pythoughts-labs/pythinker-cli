# tests/tools/test_workflow_engine.py
import asyncio

import pytest

from pythinker_code.tools.workflow.engine import (
    AgentOptions,
    RunWorkflowHooks,
    WorkflowRuntimeError,
    _estimate_tokens,
    run_workflow,
)


def make_runner(delay: float = 0.0):
    calls: list[str] = []

    async def runner(prompt: str, opts: AgentOptions):
        calls.append(prompt)
        if delay:
            await asyncio.sleep(delay)
        return f"result:{prompt}"

    return runner, calls


@pytest.mark.asyncio
async def test_single_agent_and_return():
    runner, calls = make_runner()
    script = (
        'meta = {"name": "n", "description": "d"}\n'
        'r = await agent("hello", {"label": "L"})\n'
        'return {"r": r}\n'
    )
    out = await run_workflow(script, agent_runner=runner, cwd=".")
    assert out.result == {"r": "result:hello"}
    assert out.agent_count == 1
    assert calls == ["hello"]


@pytest.mark.asyncio
async def test_multiline_prompt_string_preserved():
    # Regression guard: the AST-wrap must NOT alter triple-quoted strings the way
    # textwrap.indent would (it would inject leading spaces on continuation lines).
    seen: list[str] = []

    async def runner(prompt: str, opts):
        seen.append(prompt)
        return "ok"

    script = (
        'meta = {"name": "n", "description": "d"}\n'
        'p = """line one\n'
        "line two\n"
        '    indented body"""\n'
        "await agent(p)\n"
        "return None\n"
    )
    await run_workflow(script, agent_runner=runner, cwd=".")
    assert seen == ["line one\nline two\n    indented body"]


@pytest.mark.asyncio
async def test_parallel_preserves_order():
    runner, _ = make_runner()
    script = (
        'meta = {"name": "n", "description": "d"}\n'
        'rs = await parallel([agent("a"), agent("b"), agent("c")])\n'
        "return rs\n"
    )
    out = await run_workflow(script, agent_runner=runner, cwd=".")
    assert out.result == ["result:a", "result:b", "result:c"]
    assert out.agent_count == 3


@pytest.mark.asyncio
async def test_pipeline_stages_receive_prev_original_index():
    async def runner(prompt, opts):
        return prompt.upper()

    script = (
        'meta = {"name": "n", "description": "d"}\n'
        "rs = await pipeline(\n"
        '    ["x", "y"],\n'
        "    lambda prev, orig, i: agent(prev),\n"
        '    lambda prev, orig, i: prev + ":" + orig + ":" + str(i),\n'
        ")\n"
        "return rs\n"
    )
    out = await run_workflow(script, agent_runner=runner, cwd=".")
    assert out.result == ["X:x:0", "Y:y:1"]


@pytest.mark.asyncio
async def test_failed_branch_returns_none_and_logs():
    async def runner(prompt, opts):
        raise RuntimeError("boom")

    logs: list[str] = []
    script = (
        'meta = {"name": "n", "description": "d"}\n'
        'r = await agent("x", {"label": "bad"})\n'
        'return {"r": r}\n'
    )
    out = await run_workflow(
        script,
        agent_runner=runner,
        cwd=".",
        hooks=RunWorkflowHooks(on_log=logs.append),
    )
    assert out.result == {"r": None}
    assert any("bad" in m and "boom" in m for m in logs)


@pytest.mark.asyncio
async def test_unawaited_coroutine_in_result_raises():
    runner, _ = make_runner()
    script = (
        'meta = {"name": "n", "description": "d"}\n'
        'return {"r": agent("x")}\n'  # not awaited
    )
    with pytest.raises(WorkflowRuntimeError) as exc:
        await run_workflow(script, agent_runner=runner, cwd=".")
    assert "await" in str(exc.value)


@pytest.mark.asyncio
async def test_budget_check_does_not_race_past_semaphore():
    # Regression guard: agent() calls dispatched together (via parallel()) all
    # reach the budget check before any of them has recorded real spend. If the
    # check runs before the semaphore is acquired, every dispatched agent sees
    # the same stale state["spent"] and passes, regardless of `concurrency` —
    # so a 6-item parallel() with a budget for ~1 result would let all 6 run.
    # The check must be re-evaluated once a semaphore slot is actually granted,
    # bounding the overshoot to one concurrency batch instead of every item.
    result_payload = "x" * 200
    one_result_cost = _estimate_tokens(result_payload)

    async def runner(prompt: str, opts: AgentOptions) -> str:
        await asyncio.sleep(0.01)  # force a real suspension so calls overlap
        return result_payload

    script = (
        'meta = {"name": "n", "description": "d"}\n'
        'rs = await parallel([agent("a"), agent("b"), agent("c"), '
        'agent("d"), agent("e"), agent("f")])\n'
        "return rs\n"
    )
    out = await run_workflow(
        script,
        agent_runner=runner,
        cwd=".",
        concurrency=2,
        token_budget=one_result_cost + 1,
    )
    succeeded = [r for r in out.result if r == result_payload]
    assert len(succeeded) <= 2  # bounded by concurrency, not by the 6 dispatched items
    assert len(succeeded) >= 1  # the first batch must still get through


@pytest.mark.asyncio
async def test_cancellation_marks_running_cancelled_and_reraises():
    runner, _ = make_runner(delay=10.0)
    skipped: list[str] = []
    started: list[str] = []
    script = (
        'meta = {"name": "n", "description": "d"}\n'
        'rs = await parallel([agent("a", {"label": "a"}), agent("b", {"label": "b"})])\n'
        "return rs\n"
    )
    hooks = RunWorkflowHooks(
        on_agent_start=lambda e: started.append(e.label),
        on_agent_end=lambda e: skipped.append(e.label) if e.error == "cancelled" else None,
    )
    task = asyncio.create_task(
        run_workflow(script, agent_runner=runner, cwd=".", concurrency=4, hooks=hooks)
    )
    await asyncio.sleep(0.05)  # let both agents start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert set(started) == {"a", "b"}
    assert set(skipped) == {"a", "b"}  # in-flight agents reported as cancelled, none completed
