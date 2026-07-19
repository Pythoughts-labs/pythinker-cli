from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from rich.status import Status

from pythinker_code.auth import (
    ALIBABA_PLATFORM_ID,
    ANTHROPIC_PLATFORM_ID,
    DEEPSEEK_PLATFORM_ID,
    DIGITALOCEAN_PLATFORM_ID,
    GITHUB_COPILOT_PLATFORM_ID,
    KIMI_PLATFORM_ID,
    LM_STUDIO_PLATFORM_ID,
    MINIMAX_PLATFORM_ID,
    MOONSHOT_PLATFORM_ID,
    OLLAMA_PLATFORM_ID,
    OPENAI_API_PLATFORM_ID,
    OPENAI_CHATGPT_PLATFORM_ID,
    OPENCODE_GO_PLATFORM_ID,
    OPENROUTER_PLATFORM_ID,
    SNOWFLAKE_CORTEX_PLATFORM_ID,
    XAI_PLATFORM_ID,
    ZAI_API_PLATFORM_ID,
    ZAI_CODING_PLATFORM_ID,
)
from pythinker_code.auth.alibaba import (
    ALIBABA_PROVIDER_KEY,
    login_alibaba_api_key,
    logout_alibaba,
)
from pythinker_code.auth.anthropic_direct import (
    ANTHROPIC_PROVIDER_KEY,
    login_anthropic_api_key,
    logout_anthropic,
)
from pythinker_code.auth.copilot import (
    GITHUB_COPILOT_PROVIDER_KEY,
    login_copilot,
    logout_copilot,
)
from pythinker_code.auth.deepseek import (
    DEEPSEEK_PROVIDER_KEY,
    login_deepseek_api_key,
    logout_deepseek,
)
from pythinker_code.auth.digitalocean import (
    DIGITALOCEAN_PROVIDER_KEY,
    login_digitalocean,
    logout_digitalocean,
)
from pythinker_code.auth.kimi import (
    KIMI_PROVIDER_KEY,
    login_kimi_api_key,
    logout_kimi,
)
from pythinker_code.auth.lm_studio import (
    LM_STUDIO_PROVIDER_KEY,
    login_lm_studio,
    logout_lm_studio,
)
from pythinker_code.auth.minimax import (
    MINIMAX_ANTHROPIC_PROVIDER_KEY,
    login_minimax_api_key,
    logout_minimax,
)
from pythinker_code.auth.moonshot import (
    MOONSHOT_PROVIDER_KEY,
    login_moonshot_api_key,
    logout_moonshot,
)
from pythinker_code.auth.oauth import OAuthEvent
from pythinker_code.auth.ollama import (
    OLLAMA_PROVIDER_KEY,
    login_ollama,
    logout_ollama,
)
from pythinker_code.auth.openai import (
    login_openai_api_key,
    login_openai_browser,
    login_openai_headless,
    logout_openai,
)
from pythinker_code.auth.opencode_go import (
    OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
    OPENCODE_GO_OPENAI_PROVIDER_KEY,
    login_opencode_go_api_key,
    logout_opencode_go,
)
from pythinker_code.auth.openrouter import (
    OPENROUTER_PROVIDER_KEY,
    login_openrouter_api_key,
    logout_openrouter,
)
from pythinker_code.auth.platforms import managed_provider_key
from pythinker_code.auth.snowflake import (
    SNOWFLAKE_PROVIDER_KEY,
    login_snowflake,
    logout_snowflake,
)
from pythinker_code.auth.xai import (
    XAI_PROVIDER_KEY,
    login_xai_browser,
    login_xai_headless,
    logout_xai,
)
from pythinker_code.auth.z_ai import (
    ZAI_API_ROUTE,
    ZAI_CODING_ROUTE,
    login_z_ai_api_key,
    login_z_ai_coding_api_key,
    logout_z_ai_api,
    logout_z_ai_coding,
)
from pythinker_code.cli import Reload
from pythinker_code.ui.shell.console import console
from pythinker_code.ui.shell.selectors.oauth import (
    OAuthProviderEntry,
    OAuthProviderStatus,
    run_oauth_selector,
)
from pythinker_code.ui.shell.slash import ensure_pythinker_soul, registry
from pythinker_code.ui.theme import get_tui_tokens as _get_tui_tokens

if TYPE_CHECKING:
    from pythinker_code.config import Config
    from pythinker_code.soul.pythinkersoul import PythinkerSoul
    from pythinker_code.ui.shell import Shell


async def _render_oauth_events(events: AsyncIterator[OAuthEvent]) -> bool:
    _t = _get_tui_tokens()
    status: Status | None = None
    ok = True
    try:
        async for event in events:
            if event.type == "waiting":
                if status is None:
                    status = console.status(f"[{_t.info}]Waiting for authorization.[/]")
                    status.start()
                continue
            if status is not None:
                status.stop()
                status = None
            from rich.style import Style as _RStyle

            match event.type:
                case "error":
                    _style: _RStyle | None = _RStyle(color=_t.error)
                case "success":
                    _style = _RStyle(color=_t.success)
                case _:
                    _style = None
            console.print(event.message, markup=False, style=_style)
            if event.type == "error":
                ok = False
    finally:
        if status is not None:
            status.stop()
    return ok


async def _prompt_api_key(label: str) -> str | None:
    session = PromptSession[str]()
    try:
        value = await session.prompt_async(f" {label} API key: ", is_password=True)
    except (EOFError, KeyboardInterrupt):
        return None
    return value.strip() or None


async def _prompt_text(label: str) -> str | None:
    session = PromptSession[str]()
    try:
        value = await session.prompt_async(f" {label}: ")
    except (EOFError, KeyboardInterrupt):
        return None
    return value.strip() or None


_SELECTOR_PROVIDER_ENTRIES: list[OAuthProviderEntry] = [
    OAuthProviderEntry(id="browser", name="OpenAI ChatGPT (browser)", auth_type="oauth"),
    OAuthProviderEntry(id="headless", name="OpenAI ChatGPT (device code)", auth_type="oauth"),
    OAuthProviderEntry(id="copilot", name="GitHub Copilot", auth_type="oauth"),
    OAuthProviderEntry(id="digitalocean", name="DigitalOcean", auth_type="oauth"),
    OAuthProviderEntry(id="snowflake", name="Snowflake Cortex", auth_type="oauth"),
    OAuthProviderEntry(id="xai", name="xAI Grok (browser)", auth_type="oauth"),
    OAuthProviderEntry(id="xai-device", name="xAI Grok (device code)", auth_type="oauth"),
    OAuthProviderEntry(id="api-key", name="OpenAI API key", auth_type="api_key"),
    OAuthProviderEntry(id="opencode-go", name="OpenCode Go", auth_type="api_key"),
    OAuthProviderEntry(id="minimax", name="MiniMax", auth_type="api_key"),
    OAuthProviderEntry(id="deepseek", name="DeepSeek", auth_type="api_key"),
    OAuthProviderEntry(id="z-ai-coding", name="Z.AI Coding Plan", auth_type="api_key"),
    OAuthProviderEntry(id="z-ai-api", name="Z.AI API", auth_type="api_key"),
    OAuthProviderEntry(id="moonshot", name="Moonshot", auth_type="api_key"),
    OAuthProviderEntry(id="kimi", name="Kimi Coding Plan", auth_type="api_key"),
    OAuthProviderEntry(id="alibaba", name="Alibaba (DashScope)", auth_type="api_key"),
    OAuthProviderEntry(id="anthropic", name="Anthropic", auth_type="api_key"),
    OAuthProviderEntry(id="openrouter", name="OpenRouter", auth_type="api_key"),
    OAuthProviderEntry(id="lm-studio", name="LM Studio", auth_type="api_key"),
    OAuthProviderEntry(id="ollama", name="Ollama", auth_type="api_key"),
]


# Selector/command id -> the managed provider keys whose presence in the config
# means that provider is logged in. The OpenAI ChatGPT OAuth login (browser and
# device-code) and the OpenAI API-key login store distinct provider keys; the
# "openai" logout entry covers both.
_PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "browser": (managed_provider_key(OPENAI_CHATGPT_PLATFORM_ID),),
    "headless": (managed_provider_key(OPENAI_CHATGPT_PLATFORM_ID),),
    "api-key": (managed_provider_key(OPENAI_API_PLATFORM_ID),),
    "openai": (
        managed_provider_key(OPENAI_API_PLATFORM_ID),
        managed_provider_key(OPENAI_CHATGPT_PLATFORM_ID),
    ),
    "copilot": (GITHUB_COPILOT_PROVIDER_KEY,),
    "digitalocean": (DIGITALOCEAN_PROVIDER_KEY,),
    "snowflake": (SNOWFLAKE_PROVIDER_KEY,),
    "xai": (XAI_PROVIDER_KEY,),
    "xai-device": (XAI_PROVIDER_KEY,),
    "opencode-go": (OPENCODE_GO_OPENAI_PROVIDER_KEY, OPENCODE_GO_ANTHROPIC_PROVIDER_KEY),
    "minimax": (MINIMAX_ANTHROPIC_PROVIDER_KEY,),
    "deepseek": (DEEPSEEK_PROVIDER_KEY,),
    "z-ai-coding": (ZAI_CODING_ROUTE.provider_key,),
    "z-ai-api": (ZAI_API_ROUTE.provider_key,),
    "moonshot": (MOONSHOT_PROVIDER_KEY,),
    "kimi": (KIMI_PROVIDER_KEY,),
    "alibaba": (ALIBABA_PROVIDER_KEY,),
    "anthropic": (ANTHROPIC_PROVIDER_KEY,),
    "openrouter": (OPENROUTER_PROVIDER_KEY,),
    "lm-studio": (LM_STUDIO_PROVIDER_KEY,),
    "ollama": (OLLAMA_PROVIDER_KEY,),
}

# Providers offered by the no-argument /logout selector, one entry per provider
# (a single OpenAI entry that clears both OpenAI credentials).
_LOGOUT_PROVIDER_ENTRIES: list[OAuthProviderEntry] = [
    OAuthProviderEntry(id="openai", name="OpenAI", auth_type="oauth"),
    OAuthProviderEntry(id="copilot", name="GitHub Copilot", auth_type="oauth"),
    OAuthProviderEntry(id="digitalocean", name="DigitalOcean", auth_type="oauth"),
    OAuthProviderEntry(id="snowflake", name="Snowflake Cortex", auth_type="oauth"),
    OAuthProviderEntry(id="xai", name="xAI Grok", auth_type="oauth"),
    OAuthProviderEntry(id="opencode-go", name="OpenCode Go", auth_type="api_key"),
    OAuthProviderEntry(id="minimax", name="MiniMax", auth_type="api_key"),
    OAuthProviderEntry(id="deepseek", name="DeepSeek", auth_type="api_key"),
    OAuthProviderEntry(id="z-ai-coding", name="Z.AI Coding Plan", auth_type="api_key"),
    OAuthProviderEntry(id="z-ai-api", name="Z.AI API", auth_type="api_key"),
    OAuthProviderEntry(id="moonshot", name="Moonshot", auth_type="api_key"),
    OAuthProviderEntry(id="kimi", name="Kimi Coding Plan", auth_type="api_key"),
    OAuthProviderEntry(id="alibaba", name="Alibaba (DashScope)", auth_type="api_key"),
    OAuthProviderEntry(id="anthropic", name="Anthropic", auth_type="api_key"),
    OAuthProviderEntry(id="openrouter", name="OpenRouter", auth_type="api_key"),
    OAuthProviderEntry(id="lm-studio", name="LM Studio", auth_type="api_key"),
    OAuthProviderEntry(id="ollama", name="Ollama", auth_type="api_key"),
]


def _get_provider_status(config: Config, provider_id: str) -> OAuthProviderStatus:
    keys = _PROVIDER_KEYS.get(provider_id, ())
    if any(key in config.providers for key in keys):
        return OAuthProviderStatus(source="configured")
    return OAuthProviderStatus(source="unconfigured")


def current_model_key(soul: PythinkerSoul) -> str | None:
    config = soul.runtime.config
    curr_model_cfg = soul.runtime.llm.model_config if soul.runtime.llm else None
    if curr_model_cfg is not None:
        for name, model_cfg in config.models.items():
            if model_cfg == curr_model_cfg:
                return name
    return config.default_model or None


@registry.command(aliases=["setup"])
async def login(app: Shell, args: str) -> None:
    """Login with OpenAI, GitHub Copilot, DigitalOcean, Snowflake, xAI, or API keys."""
    soul = ensure_pythinker_soul(app)
    if soul is None:
        return
    config = soul.runtime.config
    mode = args.strip().lower()
    if mode == "":
        chosen = await run_oauth_selector(
            _SELECTOR_PROVIDER_ENTRIES,
            lambda provider_id: _get_provider_status(config, provider_id),
            action="login",
        )
        if chosen is None:
            return
        mode = chosen

    _t = _get_tui_tokens()
    if mode == "browser":
        ok = await _render_oauth_events(login_openai_browser(soul.runtime.config))
        provider = "openai-chatgpt"
    elif mode in ("headless", "device", "device-code"):
        ok = await _render_oauth_events(login_openai_headless(soul.runtime.config))
        provider = "openai-chatgpt"
    elif mode in ("copilot", "github-copilot"):
        ok = await _render_oauth_events(login_copilot(soul.runtime.config))
        provider = GITHUB_COPILOT_PLATFORM_ID
    elif mode == "digitalocean":
        ok = await _render_oauth_events(login_digitalocean(soul.runtime.config))
        provider = DIGITALOCEAN_PLATFORM_ID
    elif mode == "snowflake":
        account = await _prompt_text("Snowflake account identifier")
        if not account:
            console.print(f"[{_t.error}]Snowflake account identifier is required.[/]")
            return
        role = await _prompt_text("Snowflake role (optional)")
        ok = await _render_oauth_events(login_snowflake(soul.runtime.config, account, role or None))
        provider = SNOWFLAKE_CORTEX_PLATFORM_ID
    elif mode == "xai":
        ok = await _render_oauth_events(login_xai_browser(soul.runtime.config))
        provider = XAI_PLATFORM_ID
    elif mode in ("xai-device", "xai-headless"):
        ok = await _render_oauth_events(login_xai_headless(soul.runtime.config))
        provider = XAI_PLATFORM_ID
    elif mode in ("api-key", "apikey", "api"):
        api_key = await _prompt_api_key("OpenAI")
        if not api_key:
            console.print(f"[{_t.error}]No OpenAI API key entered.[/]")
            return
        ok = await _render_oauth_events(login_openai_api_key(soul.runtime.config, api_key))
        provider = "openai"
    elif mode in ("opencode-go", "opencode", "go"):
        api_key = await _prompt_api_key("OpenCode Go")
        if not api_key:
            console.print(f"[{_t.error}]No OpenCode Go API key entered.[/]")
            return
        ok = await _render_oauth_events(login_opencode_go_api_key(soul.runtime.config, api_key))
        provider = OPENCODE_GO_PLATFORM_ID
    elif mode == "minimax":
        api_key = await _prompt_api_key("MiniMax")
        if not api_key:
            console.print(f"[{_t.error}]No MiniMax API key entered.[/]")
            return
        ok = await _render_oauth_events(login_minimax_api_key(soul.runtime.config, api_key))
        provider = MINIMAX_PLATFORM_ID
    elif mode == "deepseek":
        api_key = await _prompt_api_key("DeepSeek")
        if not api_key:
            console.print(f"[{_t.error}]No DeepSeek API key entered.[/]")
            return
        ok = await _render_oauth_events(login_deepseek_api_key(soul.runtime.config, api_key))
        provider = DEEPSEEK_PLATFORM_ID
    elif mode == "z-ai-coding":
        api_key = await _prompt_api_key("Z.AI Coding Plan")
        if not api_key:
            console.print(f"[{_t.error}]No Z.AI Coding Plan API key entered.[/]")
            return
        ok = await _render_oauth_events(login_z_ai_coding_api_key(soul.runtime.config, api_key))
        provider = ZAI_CODING_PLATFORM_ID
    elif mode == "z-ai-api":
        api_key = await _prompt_api_key("Z.AI API")
        if not api_key:
            console.print(f"[{_t.error}]No Z.AI API key entered.[/]")
            return
        ok = await _render_oauth_events(login_z_ai_api_key(soul.runtime.config, api_key))
        provider = ZAI_API_PLATFORM_ID
    elif mode == "moonshot":
        api_key = await _prompt_api_key("Moonshot")
        if not api_key:
            console.print(f"[{_t.error}]No Moonshot API key entered.[/]")
            return
        ok = await _render_oauth_events(login_moonshot_api_key(soul.runtime.config, api_key))
        provider = MOONSHOT_PLATFORM_ID
    elif mode == "kimi":
        api_key = await _prompt_api_key("Kimi")
        if not api_key:
            console.print(f"[{_t.error}]No Kimi API key entered.[/]")
            return
        ok = await _render_oauth_events(login_kimi_api_key(soul.runtime.config, api_key))
        provider = KIMI_PLATFORM_ID
    elif mode == "alibaba":
        api_key = await _prompt_api_key("Alibaba (DashScope)")
        if not api_key:
            console.print(f"[{_t.error}]No Alibaba API key entered.[/]")
            return
        base_url: str | None = None
        if api_key.startswith("sk-ws-"):
            base_url = await _prompt_text("Token Plan OpenAI-compatible endpoint")
            if not base_url:
                console.print(f"[{_t.error}]No Token Plan endpoint entered.[/]")
                return
        ok = await _render_oauth_events(
            login_alibaba_api_key(soul.runtime.config, api_key, base_url=base_url)
        )
        provider = ALIBABA_PLATFORM_ID
    elif mode == "anthropic":
        api_key = await _prompt_api_key("Anthropic")
        if not api_key:
            console.print(f"[{_t.error}]No Anthropic API key entered.[/]")
            return
        ok = await _render_oauth_events(login_anthropic_api_key(soul.runtime.config, api_key))
        provider = ANTHROPIC_PLATFORM_ID
    elif mode == "openrouter":
        api_key = await _prompt_api_key("OpenRouter")
        if not api_key:
            console.print(f"[{_t.error}]No OpenRouter API key entered.[/]")
            return
        ok = await _render_oauth_events(login_openrouter_api_key(soul.runtime.config, api_key))
        provider = OPENROUTER_PLATFORM_ID
    elif mode in ("lm-studio", "lmstudio"):
        ok = await _render_oauth_events(login_lm_studio(soul.runtime.config))
        provider = LM_STUDIO_PLATFORM_ID
    elif mode == "ollama":
        ok = await _render_oauth_events(login_ollama(soul.runtime.config))
        provider = OLLAMA_PLATFORM_ID
    else:
        console.print(
            f"[{_t.error}]Usage: /login "
            "[browser|headless|copilot|digitalocean|snowflake|xai|xai-device|api-key|"
            "opencode-go|"
            "minimax|deepseek|z-ai-coding|z-ai-api|moonshot|kimi|alibaba|anthropic|"
            "openrouter|lm-studio|ollama][/]"
        )
        return
    if not ok:
        return
    from pythinker_code.telemetry import track

    track("login", provider=provider)
    await asyncio.sleep(1)
    console.clear()
    raise Reload


@registry.command
async def logout(app: Shell, args: str) -> None:
    """Logout from OpenAI, GitHub Copilot, DigitalOcean, Snowflake, xAI, or API keys."""
    soul = ensure_pythinker_soul(app)
    if soul is None:
        return
    config = soul.runtime.config
    _t = _get_tui_tokens()
    if not config.is_from_default_location:
        console.print(
            f"[{_t.error}]Logout requires the default config file; "
            f"restart without --config/--config-file.[/]"
        )
        return
    mode = args.strip().lower()
    if mode == "":
        configured = [
            entry
            for entry in _LOGOUT_PROVIDER_ENTRIES
            if _get_provider_status(config, entry.id).source == "configured"
        ]
        if not configured:
            console.print(f"[{_t.info}]No providers are logged in.[/]")
            return
        chosen = await run_oauth_selector(
            configured,
            lambda provider_id: _get_provider_status(config, provider_id),
            action="logout",
        )
        if chosen is None:
            return
        mode = chosen

    if mode == "openai":
        ok = await _render_oauth_events(logout_openai(config))
    elif mode in ("copilot", "github-copilot"):
        ok = await _render_oauth_events(logout_copilot(config))
    elif mode == "digitalocean":
        ok = await _render_oauth_events(logout_digitalocean(config))
    elif mode == "snowflake":
        ok = await _render_oauth_events(logout_snowflake(config))
    elif mode == "xai":
        ok = await _render_oauth_events(logout_xai(config))
    elif mode == "openrouter":
        ok = await _render_oauth_events(logout_openrouter(config))
    elif mode == "anthropic":
        ok = await _render_oauth_events(logout_anthropic(config))
    elif mode == "deepseek":
        ok = await _render_oauth_events(logout_deepseek(config))
    elif mode == "z-ai-coding":
        ok = await _render_oauth_events(logout_z_ai_coding(config))
    elif mode == "z-ai-api":
        ok = await _render_oauth_events(logout_z_ai_api(config))
    elif mode == "moonshot":
        ok = await _render_oauth_events(logout_moonshot(config))
    elif mode == "kimi":
        ok = await _render_oauth_events(logout_kimi(config))
    elif mode == "alibaba":
        ok = await _render_oauth_events(logout_alibaba(config))
    elif mode == "minimax":
        ok = await _render_oauth_events(logout_minimax(config))
    elif mode in ("opencode-go", "opencode", "go"):
        ok = await _render_oauth_events(logout_opencode_go(config))
    elif mode in ("lm-studio", "lmstudio"):
        ok = await _render_oauth_events(logout_lm_studio(config))
    elif mode == "ollama":
        ok = await _render_oauth_events(logout_ollama(config))
    elif mode in ("github-feedback", "github"):
        from pythinker_code.auth.github_feedback import delete_github_feedback_token

        delete_github_feedback_token()
        console.print(f"[{_t.success}]Logged out of GitHub feedback.[/]")
        ok = True
    else:
        console.print(
            f"[{_t.error}]Usage: /logout "
            "[openai|copilot|digitalocean|snowflake|xai|opencode-go|minimax|deepseek|"
            "z-ai-coding|"
            "z-ai-api|moonshot|kimi|alibaba|anthropic|openrouter|lm-studio|ollama|"
            "github-feedback][/]"
        )
        return
    if not ok:
        return

    from pythinker_code.telemetry import track

    track("logout")
    await asyncio.sleep(1)
    console.clear()
    raise Reload
