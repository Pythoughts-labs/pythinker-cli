"""Trust-wrapping primitive for external, untrusted data entering LLM prompts."""

from __future__ import annotations

import html
import re
import unicodedata
import uuid
from dataclasses import dataclass

# Zero-width and bidi-override characters used to smuggle hidden instructions past a
# human reviewer (the highest-confidence injection signal). Stripped from all untrusted
# content before it reaches the model. Shared with the memory scanner, which BLOCKS on
# them when deciding what to persist; tool-output ingress only STRIPS them.
INVISIBLE_CHARS = frozenset(
    chr(c)
    for c in (
        0x200B,
        0x200C,
        0x200D,
        0x2060,
        0x2061,
        0x2062,
        0x2063,
        0x2064,
        0xFEFF,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
        *range(0xE0000, 0xE0080),  # Unicode Tags block (U+E0000-U+E007F)
    )
)
_INVISIBLE_TRANSLATION = dict.fromkeys((ord(c) for c in INVISIBLE_CHARS), None)


def strip_invisible_chars(text: str) -> str:
    """Remove zero-width / bidi-override characters (the invisible injection vector).

    Used both by the untrusted-data wrapper and by trusted-but-injected surfaces
    such as the merged AGENTS.md, which lands in the system prompt verbatim.
    """
    return text.translate(_INVISIBLE_TRANSLATION)


def escape_prompt_data(text: str, *, max_chars: int) -> str:
    """Return bounded, visible, markup-safe data for a structured prompt block."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    cleaned = strip_invisible_chars(text)
    visible = "".join(
        " " if char.isspace() or unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char
        for char in cleaned
    )
    if len(visible) > max_chars:
        visible = visible[: max_chars - 1] + "…"
    return html.escape(visible, quote=True)


@dataclass(frozen=True)
class UntrustedData:
    """Marks a string as originating from an external, untrusted source.

    Call render_for_prompt() before injecting the content into any LLM prompt.
    The runtime nonce prevents the model from constructing a matching opening
    tag. The closing-tag escape prevents content from breaking out of the block.
    """

    raw_content: str

    def render_for_prompt(self) -> str:
        nonce = uuid.uuid4().hex[:8]
        # Neutralize invisible/bidi unicode before wrapping. Strip, do not block:
        # legitimate external content (security advisories, this repo's own test
        # fixtures) may contain visible "injection-like" prose, which the wrapper
        # marks as data — only the invisible smuggling vector is removed outright.
        cleaned = strip_invisible_chars(self.raw_content)
        safe_content = cleaned.replace("</untrusted_data>", "&lt;/untrusted_data&gt;")
        return f'<untrusted_data id="{nonce}">\n{safe_content}\n</untrusted_data>'


_UNTRUSTED_OPEN_RE = re.compile(r'^<untrusted_data id="[0-9a-f]+">\n')
_UNTRUSTED_CLOSE = "\n</untrusted_data>"


def strip_untrusted_envelope(text: str) -> str:
    """Inverse of :meth:`UntrustedData.render_for_prompt`, for display surfaces.

    The wrapper is model-facing only: it must never reach the TUI or ACP/IDE
    clients. Apply this at the single render boundary so renderers receive clean
    content while the model still gets the wrapped form. Removes the
    ``<untrusted_data id="...">`` envelope and restores the escaped inner closing
    tag. A no-op when the envelope is absent (most tool output is not wrapped).
    """
    open_match = _UNTRUSTED_OPEN_RE.match(text)
    if open_match is None or not text.endswith(_UNTRUSTED_CLOSE):
        return text
    inner = text[open_match.end() : -len(_UNTRUSTED_CLOSE)]
    return inner.replace("&lt;/untrusted_data&gt;", "</untrusted_data>")
