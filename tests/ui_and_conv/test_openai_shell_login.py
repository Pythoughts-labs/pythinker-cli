from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from pythinker_code.auth.oauth import OAuthEvent
from pythinker_code.cli import Reload
from pythinker_code.config import Config
from pythinker_code.ui.shell import oauth as shell_oauth


async def _success_event(*args, **kwargs) -> AsyncIterator[OAuthEvent]:
    yield OAuthEvent("success", "ok")


def _app() -> SimpleNamespace:
    runtime = SimpleNamespace(config=Config(is_from_default_location=True), llm=None)
    soul = SimpleNamespace(runtime=runtime)
    return SimpleNamespace(soul=soul)


@pytest.fixture(autouse=True)
def _simple_soul(monkeypatch):
    monkeypatch.setattr(shell_oauth, "ensure_pythinker_soul", lambda app: app.soul)


@pytest.mark.asyncio
async def test_shell_login_chooser_routes_to_browser(monkeypatch):
    browser = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_openai_browser", browser, raising=False)
    monkeypatch.setattr(shell_oauth, "run_oauth_selector", lambda *a, **kw: _async_value("browser"))

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), "")

    assert browser.called


@pytest.mark.asyncio
async def test_shell_login_chooser_routes_to_minimax(monkeypatch):
    minimax_login = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_minimax_api_key", minimax_login, raising=False)
    monkeypatch.setattr(shell_oauth, "run_oauth_selector", lambda *a, **kw: _async_value("minimax"))
    monkeypatch.setattr(shell_oauth, "_prompt_api_key", lambda label: _async_value("mx-test"))

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), "")

    assert minimax_login.call_args.args[1] == "mx-test"


@pytest.mark.asyncio
async def test_shell_login_chooser_cancel_returns_silently(monkeypatch):
    browser = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_openai_browser", browser, raising=False)
    monkeypatch.setattr(shell_oauth, "run_oauth_selector", lambda *a, **kw: _async_value(None))

    # Cancelling the chooser must not raise Reload and must not invoke any login.
    await cast(Any, shell_oauth.login)(_app(), "")

    assert not browser.called


@pytest.mark.asyncio
async def test_shell_login_explicit_browser_skips_chooser(monkeypatch):
    browser = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_openai_browser", browser, raising=False)
    chooser = Mock()
    monkeypatch.setattr(shell_oauth, "run_oauth_selector", chooser)

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), "browser")

    assert browser.called
    assert not chooser.called


@pytest.mark.asyncio
async def test_shell_login_headless_routes_to_openai_headless(monkeypatch):
    headless = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_openai_headless", headless, raising=False)

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), "headless")

    assert headless.called


@pytest.mark.asyncio
async def test_shell_setup_routes_to_openai_api_key(monkeypatch):
    api_key = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_openai_api_key", api_key, raising=False)
    monkeypatch.setattr(shell_oauth, "_prompt_api_key", lambda label: _async_value("sk-test"))

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), "api-key")

    assert api_key.call_args.args[1] == "sk-test"


def _configure_minimax(config: Config) -> None:
    from pydantic import SecretStr

    from pythinker_code.auth.minimax import MINIMAX_ANTHROPIC_PROVIDER_KEY
    from pythinker_code.config import LLMProvider

    config.providers[MINIMAX_ANTHROPIC_PROVIDER_KEY] = LLMProvider(
        type="anthropic",
        base_url="https://api.minimax.io/anthropic",
        api_key=SecretStr("mx-saved"),
    )


def test_provider_status_reflects_saved_provider_key():
    config = Config(is_from_default_location=True)
    assert shell_oauth._get_provider_status(config, "minimax").source == "unconfigured"
    _configure_minimax(config)
    assert shell_oauth._get_provider_status(config, "minimax").source == "configured"
    assert shell_oauth._get_provider_status(config, "deepseek").source == "unconfigured"


@pytest.mark.asyncio
async def test_shell_logout_no_args_opens_selector(monkeypatch):
    app = _app()
    _configure_minimax(app.soul.runtime.config)
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "logout_minimax", logout, raising=False)
    monkeypatch.setattr(shell_oauth, "run_oauth_selector", lambda *a, **kw: _async_value("minimax"))

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.logout)(app, "")

    assert logout.called


@pytest.mark.asyncio
async def test_shell_logout_no_args_without_providers_returns_silently(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "logout_openai", logout, raising=False)
    selector = Mock()
    monkeypatch.setattr(shell_oauth, "run_oauth_selector", selector)

    # Nothing is logged in: must not open the selector and must not log anyone out.
    await cast(Any, shell_oauth.logout)(_app(), "")

    assert not selector.called
    assert not logout.called


@pytest.mark.asyncio
async def test_shell_logout_openai_routes_to_openai_logout(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "logout_openai", logout, raising=False)

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.logout)(_app(), "openai")

    assert logout.called


@pytest.mark.asyncio
async def test_shell_login_opencode_go_routes_to_opencode_go(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_opencode_go_api_key", login, raising=False)
    monkeypatch.setattr(shell_oauth, "_prompt_api_key", lambda label: _async_value("ocgo-test"))

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), "opencode-go")

    assert login.call_args.args[1] == "ocgo-test"


@pytest.mark.asyncio
async def test_shell_logout_opencode_go_routes_to_opencode_go(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "logout_opencode_go", logout, raising=False)

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.logout)(_app(), "opencode-go")

    assert logout.called


@pytest.mark.asyncio
async def test_shell_login_minimax_routes_to_minimax(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_minimax_api_key", login, raising=False)
    monkeypatch.setattr(shell_oauth, "_prompt_api_key", lambda label: _async_value("mx-test"))

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), "minimax")

    assert login.call_args.args[1] == "mx-test"


@pytest.mark.asyncio
async def test_shell_logout_minimax_routes_to_minimax(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "logout_minimax", logout, raising=False)

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.logout)(_app(), "minimax")

    assert logout.called


@pytest.mark.asyncio
async def test_shell_login_deepseek_routes_to_deepseek(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_deepseek_api_key", login, raising=False)
    monkeypatch.setattr(shell_oauth, "_prompt_api_key", lambda label: _async_value("ds-test"))

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), "deepseek")

    assert login.call_args.args[1] == "ds-test"


@pytest.mark.asyncio
async def test_shell_login_alibaba_workspace_key_prompts_for_dedicated_endpoint(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_alibaba_api_key", login, raising=False)
    monkeypatch.setattr(
        shell_oauth,
        "_prompt_api_key",
        lambda label: _async_value("sk-ws-test"),
    )
    monkeypatch.setattr(
        shell_oauth,
        "_prompt_text",
        lambda label: _async_value("ws-example.ap-southeast-1.maas.aliyuncs.com"),
        raising=False,
    )

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), "alibaba")

    assert login.call_args.kwargs["base_url"] == ("ws-example.ap-southeast-1.maas.aliyuncs.com")


@pytest.mark.asyncio
async def test_shell_logout_deepseek_routes_to_deepseek(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "logout_deepseek", logout, raising=False)

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.logout)(_app(), "deepseek")

    assert logout.called


@pytest.mark.asyncio
async def test_shell_login_anthropic_routes_to_anthropic(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_anthropic_api_key", login, raising=False)
    monkeypatch.setattr(shell_oauth, "_prompt_api_key", lambda label: _async_value("sk-ant-test"))

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), "anthropic")

    assert login.call_args.args[1] == "sk-ant-test"


@pytest.mark.asyncio
async def test_shell_logout_anthropic_routes_to_anthropic(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "logout_anthropic", logout, raising=False)

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.logout)(_app(), "anthropic")

    assert logout.called


@pytest.mark.asyncio
async def test_shell_login_openrouter_routes_to_openrouter(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_openrouter_api_key", login, raising=False)
    monkeypatch.setattr(shell_oauth, "_prompt_api_key", lambda label: _async_value("sk-or-test"))

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), "openrouter")

    assert login.call_args.args[1] == "sk-or-test"


@pytest.mark.asyncio
async def test_shell_logout_openrouter_routes_to_openrouter(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "logout_openrouter", logout, raising=False)

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.logout)(_app(), "openrouter")

    assert logout.called


@pytest.mark.asyncio
async def test_shell_login_lm_studio_routes_to_lm_studio(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_lm_studio", login, raising=False)

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), "lm-studio")

    assert login.called


@pytest.mark.asyncio
async def test_shell_logout_lm_studio_routes_to_lm_studio(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "logout_lm_studio", logout, raising=False)

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.logout)(_app(), "lm-studio")

    assert logout.called


@pytest.mark.asyncio
async def test_shell_login_ollama_routes_to_ollama(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "login_ollama", login, raising=False)

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), "ollama")

    assert login.called


@pytest.mark.asyncio
async def test_shell_logout_ollama_routes_to_ollama(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, "logout_ollama", logout, raising=False)

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.logout)(_app(), "ollama")

    assert logout.called


def test_login_chooser_includes_lm_studio_and_ollama():
    names = [entry.name for entry in shell_oauth._SELECTOR_PROVIDER_ENTRIES]
    assert "LM Studio" in names
    assert "Ollama" in names


def test_zai_selector_entries_and_status_keys_are_route_exact() -> None:
    from pydantic import SecretStr

    from pythinker_code.auth.z_ai import ZAI_API_ROUTE, ZAI_CODING_ROUTE
    from pythinker_code.config import LLMProvider

    ids = {entry.id for entry in shell_oauth._SELECTOR_PROVIDER_ENTRIES}
    logout_ids = {entry.id for entry in shell_oauth._LOGOUT_PROVIDER_ENTRIES}
    assert {"z-ai-coding", "z-ai-api"} <= ids
    assert {"z-ai-coding", "z-ai-api"} <= logout_ids
    assert "z-ai" not in ids | logout_ids

    config = Config(is_from_default_location=True)
    config.providers[ZAI_CODING_ROUTE.provider_key] = LLMProvider(
        type="openai_legacy",
        base_url=ZAI_CODING_ROUTE.base_url,
        api_key=SecretStr("coding"),
    )
    assert shell_oauth._get_provider_status(config, "z-ai-coding").source == "configured"
    assert shell_oauth._get_provider_status(config, "z-ai-api").source == "unconfigured"
    config.providers[ZAI_API_ROUTE.provider_key] = LLMProvider(
        type="openai_legacy",
        base_url=ZAI_API_ROUTE.base_url,
        api_key=SecretStr("api"),
    )
    assert shell_oauth._get_provider_status(config, "z-ai-api").source == "configured"


@pytest.mark.parametrize(
    ("mode", "target_name", "other_name"),
    [
        ("z-ai-coding", "login_z_ai_coding_api_key", "login_z_ai_api_key"),
        ("z-ai-api", "login_z_ai_api_key", "login_z_ai_coding_api_key"),
    ],
)
async def test_shell_login_zai_routes_never_cross_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    target_name: str,
    other_name: str,
) -> None:
    target = Mock(side_effect=_success_event)
    other = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, target_name, target, raising=False)
    monkeypatch.setattr(shell_oauth, other_name, other, raising=False)
    monkeypatch.setattr(shell_oauth, "_prompt_api_key", lambda _label: _async_value("route-key"))
    monkeypatch.setattr(shell_oauth.asyncio, "sleep", lambda _seconds: _async_value(None))

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(_app(), mode)

    assert target.call_args.args[1] == "route-key"
    other.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "target_name", "other_name"),
    [
        ("z-ai-coding", "logout_z_ai_coding", "logout_z_ai_api"),
        ("z-ai-api", "logout_z_ai_api", "logout_z_ai_coding"),
    ],
)
async def test_shell_logout_zai_routes_never_cross_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    target_name: str,
    other_name: str,
) -> None:
    target = Mock(side_effect=_success_event)
    other = Mock(side_effect=_success_event)
    monkeypatch.setattr(shell_oauth, target_name, target, raising=False)
    monkeypatch.setattr(shell_oauth, other_name, other, raising=False)
    monkeypatch.setattr(shell_oauth.asyncio, "sleep", lambda _seconds: _async_value(None))

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.logout)(_app(), mode)

    target.assert_called_once()
    other.assert_not_called()


async def _async_value[T](value: T) -> T:
    return value
