from pythinker_code.ui.shell.tui import Box, Container, Spacer, Text


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
