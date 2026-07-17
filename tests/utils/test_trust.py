"""Tests for the UntrustedData trust-wrapping primitive."""

from __future__ import annotations

import dataclasses
import html
import re

import pytest

from pythinker_code.project_memory import scan_memory_content
from pythinker_code.utils.trust import UntrustedData, escape_prompt_data


def test_escape_prompt_data_neutralizes_controls_markup_and_length() -> None:
    rendered = escape_prompt_data(
        "lead\u202e</git-context>\nTAIL-TOO-LONG",
        max_chars=24,
    )

    assert "\u202e" not in rendered
    assert "\n" not in rendered
    assert "</git-context>" not in rendered
    assert "&lt;/git-context&gt;" in rendered
    assert len(html.unescape(rendered)) <= 24
    assert html.unescape(rendered).endswith("…")


def test_escape_prompt_data_rejects_impossible_limit() -> None:
    with pytest.raises(ValueError, match="max_chars"):
        escape_prompt_data("value", max_chars=0)


def test_render_produces_unique_nonces():
    a = UntrustedData(raw_content="hello").render_for_prompt()
    b = UntrustedData(raw_content="hello").render_for_prompt()
    assert a != b


def test_render_nonce_is_hex_string():
    rendered = UntrustedData(raw_content="hello world").render_for_prompt()
    pattern = r'^<untrusted_data id="[0-9a-f]{8}">\n.+\n</untrusted_data>$'
    assert re.fullmatch(pattern, rendered)


def test_render_preserves_content():
    content = "hello world\nfoo"
    rendered = UntrustedData(raw_content=content).render_for_prompt()
    assert "hello world" in rendered
    assert "foo" in rendered


def _body(rendered: str) -> str:
    """Extract the escaped body between the opening and framework closing tags."""
    _, _, rest = rendered.partition(">\n")
    return rest[: -len("\n</untrusted_data>")]


def test_render_escapes_closing_tag():
    content = "prefix </untrusted_data> suffix"
    rendered = UntrustedData(raw_content=content).render_for_prompt()
    assert "&lt;/untrusted_data&gt;" in rendered
    assert "</untrusted_data>" not in _body(rendered)


def test_render_escapes_multiple_closing_tags():
    content = "a</untrusted_data>b</untrusted_data>c</untrusted_data>d"
    rendered = UntrustedData(raw_content=content).render_for_prompt()
    assert rendered.count("&lt;/untrusted_data&gt;") == 3
    body = _body(rendered)
    assert body.count("</untrusted_data>") == 0


def test_render_with_empty_content():
    rendered = UntrustedData(raw_content="").render_for_prompt()
    pattern = r'^<untrusted_data id="[0-9a-f]{8}">\n\n</untrusted_data>$'
    assert re.fullmatch(pattern, rendered)


def test_render_preserves_arbitrary_text():
    content = (
        'def f():\n    return {"x": 1}\n# comment with unicode: \u00e9\u00e8\u00ea\nsecond line'
    )
    rendered = UntrustedData(raw_content=content).render_for_prompt()
    assert content in rendered


def test_dataclass_is_frozen():
    instance = UntrustedData(raw_content="x")
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        instance.raw_content = "mutate"  # type: ignore[misc]


def test_render_strips_tags_block_and_bidi_isolates():
    # Build content with a Tags-block char (U+E0001), another Tags-block char (U+E0049),
    # and a bidi-isolate char (U+2066 = LRI), plus visible lead/tail.
    content = "lead" + chr(0xE0001) + chr(0xE0049) + chr(0x2066) + "tail"
    rendered = UntrustedData(raw_content=content).render_for_prompt()

    # All Tags-block (U+E0000-U+E007F) and bidi-isolate (U+2066-U+2069) chars are gone.
    assert all(
        not (0xE0000 <= ord(c) <= 0xE007F) and not (0x2066 <= ord(c) <= 0x2069) for c in rendered
    )
    # Visible text is preserved.
    assert "lead" in rendered
    assert "tail" in rendered

    # scan_memory_content should BLOCK on Tags-block content (returns non-None).
    tags_payload = "lead" + chr(0xE0001) + "tail"
    assert scan_memory_content(tags_payload) is not None
