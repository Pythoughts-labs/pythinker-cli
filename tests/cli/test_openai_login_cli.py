from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import Mock

from typer.testing import CliRunner

from pythinker_code.auth.oauth import OAuthEvent
from pythinker_code.cli import cli
from pythinker_code.config import Config

runner = CliRunner()


async def _success_event(*args, **kwargs) -> AsyncIterator[OAuthEvent]:
    yield OAuthEvent("success", "ok")


def test_cli_login_defaults_to_openai_browser(monkeypatch):
    browser = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.login_openai_browser", browser, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["login"])

    assert result.exit_code == 0
    assert "ok" in result.output
    assert browser.called


def test_cli_login_headless_routes_to_openai_headless(monkeypatch):
    headless = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.login_openai_headless", headless, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["login", "--headless"])

    assert result.exit_code == 0
    assert headless.called


def test_cli_login_copilot_routes_to_copilot(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.login_copilot", login, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["login", "--copilot"])

    assert result.exit_code == 0
    assert login.called


def test_cli_login_xai_routes_to_browser(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.login_xai_browser", login, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["login", "--xai"])

    assert result.exit_code == 0
    assert login.called


def test_cli_login_xai_device_routes_to_headless(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.login_xai_headless", login, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["login", "--xai-device"])

    assert result.exit_code == 0
    assert login.called


def test_cli_login_rejects_multiple_xai_modes(monkeypatch):
    result = runner.invoke(cli, ["login", "--xai", "--xai-device"])

    assert result.exit_code == 1
    assert "Choose only one" in result.output


def test_cli_login_api_key_routes_to_openai_api_key(monkeypatch):
    api_key = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.login_openai_api_key", api_key, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["login", "--api-key"], input="sk-test\n")

    assert result.exit_code == 0
    assert api_key.call_args.args[1] == "sk-test"


def test_cli_logout_routes_to_openai_logout(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.logout_openai", logout, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["logout"])

    assert result.exit_code == 0
    assert logout.called


def test_cli_logout_copilot_routes_to_copilot(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.logout_copilot", logout, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["logout", "--copilot"])

    assert result.exit_code == 0
    assert logout.called


def test_cli_logout_xai_routes_to_xai(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.logout_xai", logout, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["logout", "--xai"])

    assert result.exit_code == 0
    assert logout.called


def test_cli_logout_rejects_xai_with_other_mode(monkeypatch):
    result = runner.invoke(cli, ["logout", "--xai", "--copilot"])

    assert result.exit_code == 1
    assert "Choose only one" in result.output


def test_cli_login_opencode_go_routes_to_opencode_go(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.login_opencode_go_api_key", login, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["login", "--opencode-go"], input="ocgo-test\n")

    assert result.exit_code == 0
    assert login.call_args.args[1] == "ocgo-test"


def test_cli_login_rejects_opencode_go_with_openai_mode(monkeypatch):
    result = runner.invoke(cli, ["login", "--opencode-go", "--api-key"])

    assert result.exit_code == 1
    assert "Choose only one" in result.output


def test_cli_logout_opencode_go_routes_to_opencode_go_logout(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.logout_opencode_go", logout, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["logout", "--opencode-go"])

    assert result.exit_code == 0
    assert logout.called


def test_cli_login_minimax_routes_to_minimax(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.login_minimax_api_key", login, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["login", "--minimax"], input="mx-test\n")

    assert result.exit_code == 0
    assert login.call_args.args[1] == "mx-test"


def test_cli_login_rejects_minimax_with_other_modes(monkeypatch):
    result = runner.invoke(cli, ["login", "--minimax", "--api-key"])

    assert result.exit_code == 1
    assert "Choose only one" in result.output

    result_two = runner.invoke(cli, ["login", "--minimax", "--opencode-go"])

    assert result_two.exit_code == 1
    assert "Choose only one" in result_two.output


def test_cli_logout_minimax_routes_to_minimax_logout(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.logout_minimax", logout, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["logout", "--minimax"])

    assert result.exit_code == 0
    assert logout.called


def test_cli_login_deepseek_routes_to_deepseek(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.login_deepseek_api_key", login, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["login", "--deepseek"], input="ds-test\n")

    assert result.exit_code == 0
    assert login.call_args.args[1] == "ds-test"


def test_cli_login_rejects_deepseek_with_other_modes(monkeypatch):
    result = runner.invoke(cli, ["login", "--deepseek", "--api-key"])

    assert result.exit_code == 1
    assert "Choose only one" in result.output

    result_two = runner.invoke(cli, ["login", "--deepseek", "--minimax"])

    assert result_two.exit_code == 1
    assert "Choose only one" in result_two.output


def test_cli_logout_deepseek_routes_to_deepseek_logout(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.logout_deepseek", logout, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["logout", "--deepseek"])

    assert result.exit_code == 0
    assert logout.called


def test_cli_login_anthropic_routes_to_anthropic(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.login_anthropic_api_key", login, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["login", "--anthropic"], input="sk-ant-test\n")

    assert result.exit_code == 0
    assert login.call_args.args[1] == "sk-ant-test"


def test_cli_login_rejects_anthropic_with_other_modes(monkeypatch):
    result = runner.invoke(cli, ["login", "--anthropic", "--api-key"])

    assert result.exit_code == 1
    assert "Choose only one" in result.output


def test_cli_logout_anthropic_routes_to_anthropic_logout(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.logout_anthropic", logout, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["logout", "--anthropic"])

    assert result.exit_code == 0
    assert logout.called


def test_cli_login_openrouter_routes_to_openrouter(monkeypatch):
    login = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.login_openrouter_api_key", login, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["login", "--openrouter"], input="sk-or-test\n")

    assert result.exit_code == 0
    assert login.call_args.args[1] == "sk-or-test"


def test_cli_login_rejects_openrouter_with_other_modes(monkeypatch):
    result = runner.invoke(cli, ["login", "--openrouter", "--api-key"])

    assert result.exit_code == 1
    assert "Choose only one" in result.output


def test_cli_logout_openrouter_routes_to_openrouter_logout(monkeypatch):
    logout = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.logout_openrouter", logout, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["logout", "--openrouter"])

    assert result.exit_code == 0
    assert logout.called


def test_cli_login_lm_studio_routes_to_login_lm_studio(monkeypatch):
    captured: dict[str, object] = {}

    async def login(config, base_url=None):
        captured["config"] = config
        captured["base_url"] = base_url
        if False:
            yield  # pragma: no cover - empty async generator

    monkeypatch.setattr("pythinker_code.cli.login_lm_studio", login, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["login", "--lm-studio"])
    assert result.exit_code == 0, result.output
    assert "config" in captured
    assert captured["base_url"] is None


def test_cli_login_lm_studio_passes_base_url(monkeypatch):
    captured: dict[str, object] = {}

    async def login(config, base_url=None):
        captured["base_url"] = base_url
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr("pythinker_code.cli.login_lm_studio", login, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(
        cli,
        ["login", "--lm-studio", "--base-url", "http://10.0.0.5:1234/v1"],
    )
    assert result.exit_code == 0, result.output
    assert captured["base_url"] == "http://10.0.0.5:1234/v1"


def test_cli_login_ollama_routes_to_login_ollama(monkeypatch):
    captured: dict[str, object] = {}

    async def login(config, base_url=None):
        captured["base_url"] = base_url
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr("pythinker_code.cli.login_ollama", login, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["login", "--ollama"])
    assert result.exit_code == 0, result.output
    assert captured["base_url"] is None


def test_cli_login_rejects_lm_studio_with_ollama():
    result = runner.invoke(cli, ["login", "--lm-studio", "--ollama"])
    assert result.exit_code == 1
    assert "Choose only one" in result.output


def test_cli_logout_lm_studio_routes_to_logout_lm_studio(monkeypatch):
    captured: dict[str, object] = {}

    async def logout(config):
        captured["config"] = config
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr("pythinker_code.cli.logout_lm_studio", logout, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["logout", "--lm-studio"])
    assert result.exit_code == 0, result.output
    assert "config" in captured


def test_cli_logout_ollama_routes_to_logout_ollama(monkeypatch):
    captured: dict[str, object] = {}

    async def logout(config):
        captured["config"] = config
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr("pythinker_code.cli.logout_ollama", logout, raising=False)
    monkeypatch.setattr(
        "pythinker_code.cli.load_config",
        lambda: Config(is_from_default_location=True),
        raising=False,
    )

    result = runner.invoke(cli, ["logout", "--ollama"])
    assert result.exit_code == 0, result.output
