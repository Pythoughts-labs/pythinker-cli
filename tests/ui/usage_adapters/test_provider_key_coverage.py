"""Lock in that every `managed:<platform_id>` provider key the auth/* modules
construct can be looked up in the usage ADAPTERS registry. A provider that
falls through here surfaces as "Usage tracking is not yet available for the
active provider" in /usage even when an adapter exists — that's the bug
fixed by registering MiniMaxAdapter under `minimax-anthropic` and
OpenCodeGoAdapter under both `opencode-go-openai` / `opencode-go-anthropic`.
"""

from __future__ import annotations

from pythinker_code.auth.platforms import parse_managed_provider_key
from pythinker_code.ui.shell.usage_adapters import ADAPTERS

# Hardcoded list of provider keys the codebase actually constructs today.
# Cross-checked against:
#   src/pythinker_code/auth/minimax.py  (MINIMAX_ANTHROPIC_PROVIDER_KEY)
#   src/pythinker_code/auth/opencode_go.py
#       (OPENCODE_GO_OPENAI_PROVIDER_KEY, OPENCODE_GO_ANTHROPIC_PROVIDER_KEY)
#   src/pythinker_code/auth/__init__.py  (MINIMAX_PLATFORM_ID, OPENCODE_GO_PLATFORM_ID)
ACTIVE_PROVIDER_KEYS = [
    "managed:anthropic",
    "managed:deepseek",
    "managed:minimax-anthropic",
    "managed:openai",
    "managed:openai-chatgpt",
    "managed:opencode-go-openai",
    "managed:opencode-go-anthropic",
    "managed:openrouter",
    "managed:pythinker-code",
    "managed:pythinker-ai",
    "managed:pythinker_ai-cn",
    "managed:z-ai-coding",
    "managed:z-ai-api",
]


def test_every_active_provider_key_resolves_to_an_adapter() -> None:
    missing: list[str] = []
    for provider_key in ACTIVE_PROVIDER_KEYS:
        platform_id = parse_managed_provider_key(provider_key)
        if platform_id not in ADAPTERS:
            missing.append(f"{provider_key} → platform_id={platform_id!r}")
    assert not missing, (
        "Provider keys with no usage adapter — /usage will fall into the "
        "no-adapter branch for these. Register the adapter under each variant "
        f"in usage_adapters/__init__.py: {missing}"
    )


def test_minimax_anthropic_and_bare_minimax_share_one_adapter() -> None:
    # Both keys must resolve to the *same* adapter instance — otherwise users
    # on different MiniMax compat surfaces get inconsistent panels.
    assert ADAPTERS["minimax"] is ADAPTERS["minimax-anthropic"]


def test_opencode_go_variants_share_one_adapter() -> None:
    assert ADAPTERS["opencode-go"] is ADAPTERS["opencode-go-openai"]
    assert ADAPTERS["opencode-go"] is ADAPTERS["opencode-go-anthropic"]


def test_pythinker_ai_global_and_cn_share_one_adapter() -> None:
    assert ADAPTERS["pythinker-ai"] is ADAPTERS["pythinker_ai-cn"]
