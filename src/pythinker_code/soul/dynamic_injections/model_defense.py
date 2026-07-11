"""Model-keyed protocol-defense injection provider (sysprompt-1).

Some models carry quirks that warrant a short, targeted reminder — e.g.
Qwen-family models drifting into Chinese. Rather than bloating the shared,
cache-stable system prompt for *every* model (or cloning the agent per model),
this provider emits a family-matched defense fragment once per session via the
existing dynamic-injection channel, so only the affected models pay for it.

Scope note: general product behavior (the Pythinker identity override, the
output-language rule) stays in the canonical system prompt — it applies to all
models and is not a per-model quirk. This channel is for *model-specific*
reinforcement only. Wire/protocol quirks (tool-schema serialization, empty
content with tool calls) belong at the provider-adapter layer, not here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pythinker_core.message import Message

from pythinker_code.soul.dynamic_injection import (
    DynamicInjection,
    DynamicInjectionProvider,
    PreparedInjection,
)

if TYPE_CHECKING:
    from pythinker_code.soul.pythinkersoul import PythinkerSoul

_MODEL_DEFENSE_TYPE = "model_defense"


@dataclass(frozen=True)
class ModelDefenseFragment:
    """A model-family-keyed defense fragment.

    ``patterns`` and ``excludes`` are case-insensitive substrings matched against
    the model name (``excludes`` veto a match). Keep ``content`` short; it is
    wrapped in a ``<system-reminder>``.
    """

    name: str
    patterns: tuple[str, ...]
    content: str
    excludes: tuple[str, ...] = ()

    def matches(self, model_name: str) -> bool:
        lowered = model_name.lower()
        if any(exclude.lower() in lowered for exclude in self.excludes):
            return False
        return any(pattern.lower() in lowered for pattern in self.patterns)


# Registry of model-specific defenses. Add entries here for new model quirks;
# keep each fragment minimal and genuinely model-specific.
MODEL_DEFENSE_FRAGMENTS: tuple[ModelDefenseFragment, ...] = (
    ModelDefenseFragment(
        name="qwen-language",
        patterns=("qwen",),
        content=(
            "Model-specific reminder: Qwen-family models tend to drift into Chinese. "
            "Regardless of the model's own defaults, write ALL natural-language output "
            "in the language of the user's latest request (per the Output Language "
            "rule). Never switch to Chinese unless the user themselves wrote in Chinese."
        ),
    ),
)


class ModelDefenseInjectionProvider(DynamicInjectionProvider):
    """Emits model-family-keyed defense fragments once per session for the active
    model, via the dynamic-injection channel (keeps the static prompt cache-stable).
    """

    def __init__(self, fragments: Sequence[ModelDefenseFragment] = MODEL_DEFENSE_FRAGMENTS) -> None:
        self._fragments = tuple(fragments)
        # Legacy direct-call one-shot guard. Request assembly serializes preparation
        # per provider in RequestLifecycle, including concurrent main and /btw requests.
        self._injected = False

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        injections = self._candidate_injections(history, soul)
        if injections:
            self._injected = True
        return injections

    async def prepare_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[PreparedInjection]:
        if self._prepared_injections:
            return list(self._prepared_injections)
        injections = self._matching_injections(history, soul)
        if injections:
            self._prepared_injections = tuple(
                PreparedInjection(injection.type, injection) for injection in injections
            )
        return list(self._prepared_injections)

    def _candidate_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        _ = history
        if self._injected or not soul.model_name:
            return []
        return self._matching_injections(history, soul)

    def _matching_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        _ = history
        if not soul.model_name:
            return []
        matched = [fragment for fragment in self._fragments if fragment.matches(soul.model_name)]
        return [
            DynamicInjection(
                type=f"{_MODEL_DEFENSE_TYPE}:{fragment.name}", content=fragment.content
            )
            for fragment in matched
        ]

    def _on_injections_acknowledged(self, injections: Sequence[DynamicInjection]) -> None:
        if injections:
            self._injected = True

    async def on_context_compacted(self) -> None:
        # Compaction rewrites history; the prior defense reminder may have been
        # summarized away, so re-arm for the next step.
        self._injected = False
        self._prepared_injections = ()
