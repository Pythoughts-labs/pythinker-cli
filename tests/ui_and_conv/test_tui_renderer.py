from pythinker_code.ui.shell.tui import (
    Box,
    Container,
    LinePatch,
    RenderScheduler,
    RunningPromptScene,
    Spacer,
    Text,
    plan_line_diff,
    synchronized_output,
)


def test_text_wraps_and_pads_to_width() -> None:
    text = Text("hello world", padding_x=1)

    assert text.render(8) == [" hello  ", " world  "]


def test_spacer_renders_blank_lines() -> None:
    assert Spacer(2).render(5) == ["     ", "     "]


def test_container_concatenates_children() -> None:
    root = Container([Text("one"), Spacer(1), Text("two")])

    assert root.render(6) == ["one   ", "      ", "two   "]


def test_box_applies_padding_and_background_function() -> None:
    box = Box(Text("run"), padding_x=1, padding_y=1, style=lambda value: f"<{value}>")

    assert box.render(7) == ["<       >", "< run   >", "<       >"]


def test_plan_line_diff_replaces_changed_middle_run() -> None:
    old = ["top", "old", "same"]
    new = ["top", "new", "same"]

    assert plan_line_diff(old, new) == [LinePatch(start=1, delete=1, insert=("new",))]


def test_plan_line_diff_handles_growth_and_shrink() -> None:
    assert plan_line_diff(["a"], ["a", "b"]) == [LinePatch(start=1, delete=0, insert=("b",))]
    assert plan_line_diff(["a", "b"], ["a"]) == [LinePatch(start=1, delete=1, insert=())]


def test_synchronized_output_wraps_payload() -> None:
    assert synchronized_output("abc") == "\x1b[?2026habc\x1b[?2026l"


def test_render_scheduler_coalesces_fast_requests() -> None:
    calls: list[str] = []
    scheduler = RenderScheduler(lambda: calls.append("invalidate"), min_interval_seconds=0.1)

    assert scheduler.request_render(now=1.0) is True
    assert scheduler.request_render(now=1.05) is False
    assert scheduler.request_render(now=1.11) is True
    assert calls == ["invalidate", "invalidate"]


def test_running_prompt_scene_keeps_input_card_after_stream_body() -> None:
    scene = RunningPromptScene(
        body="streaming\ntext", top_border="──────── ● off", prompt_symbol="❯"
    )

    assert scene.render(16) == [
        "streaming       ",
        "text            ",
        "──────── ● off  ",
        "  ❯             ",
    ]


def test_running_prompt_scene_keeps_card_when_body_empty() -> None:
    scene = RunningPromptScene(body="", top_border="──────── ● off", prompt_symbol="❯")

    assert scene.render(16) == ["──────── ● off  ", "  ❯             "]
