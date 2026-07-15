from __future__ import annotations

from pythinker_code.ui.shell.usage_adapters.alibaba import AlibabaAdapter
from pythinker_code.ui.shell.usage_adapters.anthropic_admin import AnthropicAdminAdapter
from pythinker_code.ui.shell.usage_adapters.base import (
    UsageAdapter,
    UsageReport,
    UsageRow,
)
from pythinker_code.ui.shell.usage_adapters.deepseek import DeepSeekAdapter
from pythinker_code.ui.shell.usage_adapters.minimax import MiniMaxAdapter
from pythinker_code.ui.shell.usage_adapters.openai_admin import OpenAIAdminAdapter
from pythinker_code.ui.shell.usage_adapters.openai_chatgpt import OpenAIChatGPTAdapter
from pythinker_code.ui.shell.usage_adapters.opencode_go import OpenCodeGoAdapter
from pythinker_code.ui.shell.usage_adapters.openrouter import OpenRouterAdapter
from pythinker_code.ui.shell.usage_adapters.pythinker import PythinkerAdapter
from pythinker_code.ui.shell.usage_adapters.pythinker_ai import PythinkerAIAdapter
from pythinker_code.ui.shell.usage_adapters.z_ai import ZaiUsageAdapter

_pythinker_ai_adapter = PythinkerAIAdapter()
_minimax_adapter = MiniMaxAdapter()
_opencode_go_adapter = OpenCodeGoAdapter()
_zai_coding_adapter = ZaiUsageAdapter("z-ai-coding", "Z.AI Coding Plan")
_zai_api_adapter = ZaiUsageAdapter("z-ai-api", "Z.AI API")

# A single provider can be registered under several `managed:<platform_id>`
# keys when the chat path is exposed through both Anthropic-compat and
# OpenAI-compat shapes (e.g. `managed:minimax-anthropic`,
# `managed:opencode-go-openai`, `managed:opencode-go-anthropic`). The active
# model's `provider` field carries one of these specific keys, so the adapter
# registry has to cover every variant — otherwise `_select_providers` filters
# the active provider out and `/usage` falls into the no-adapter branch.
ADAPTERS: dict[str, UsageAdapter] = {
    AlibabaAdapter.platform_id: AlibabaAdapter(),
    AnthropicAdminAdapter.platform_id: AnthropicAdminAdapter(),
    DeepSeekAdapter.platform_id: DeepSeekAdapter(),
    OpenAIAdminAdapter.platform_id: OpenAIAdminAdapter(),
    OpenAIChatGPTAdapter.platform_id: OpenAIChatGPTAdapter(),
    OpenRouterAdapter.platform_id: OpenRouterAdapter(),
    PythinkerAdapter.platform_id: PythinkerAdapter(),
    # MiniMax — only the anthropic-compat variant exists today; the bare
    # `minimax` key is kept for a possible future openai-compat variant.
    MiniMaxAdapter.platform_id: _minimax_adapter,
    "minimax-anthropic": _minimax_adapter,
    # OpenCode Go — both compat surfaces in use today.
    OpenCodeGoAdapter.platform_id: _opencode_go_adapter,
    "opencode-go-openai": _opencode_go_adapter,
    "opencode-go-anthropic": _opencode_go_adapter,
    # Pythinker AI — global and China regions share the adapter (same shape,
    # different host).
    PythinkerAIAdapter.platform_id: _pythinker_ai_adapter,
    "pythinker_ai-cn": _pythinker_ai_adapter,
    "z-ai-coding": _zai_coding_adapter,
    "z-ai-api": _zai_api_adapter,
}

__all__ = ["ADAPTERS", "UsageAdapter", "UsageReport", "UsageRow"]
