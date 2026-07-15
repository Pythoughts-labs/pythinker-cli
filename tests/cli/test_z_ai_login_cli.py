from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from pythinker_code.auth.oauth import OAuthEvent
from pythinker_code.cli import cli
from pythinker_code.config import Config

runner = CliRunner()


async def _success_event(*_args: object, **_kwargs: object) -> AsyncIterator[OAuthEvent]:
    yield OAuthEvent("success", "ok")


def _patch_config(monkeypatch: pytest.MonkeyPatch) -> Config:
    config = Config(is_from_default_location=True)
    monkeypatch.setattr("pythinker_code.cli.load_config", lambda: config, raising=False)
    return config


@pytest.mark.parametrize(
    ("flag", "target_name", "other_name"),
    [
        ("--z-ai-coding", "login_z_ai_coding_api_key", "login_z_ai_api_key"),
        ("--z-ai-api", "login_z_ai_api_key", "login_z_ai_coding_api_key"),
    ],
)
def test_cli_login_zai_routes_dispatch_only_named_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    target_name: str,
    other_name: str,
) -> None:
    config = _patch_config(monkeypatch)
    target = Mock(side_effect=_success_event)
    other = Mock(side_effect=_success_event)
    monkeypatch.setattr(f"pythinker_code.cli.{target_name}", target, raising=False)
    monkeypatch.setattr(f"pythinker_code.cli.{other_name}", other, raising=False)

    result = runner.invoke(cli, ["login", flag], input="route-key\n")

    assert result.exit_code == 0, result.output
    assert target.call_args.args == (config, "route-key")
    other.assert_not_called()


@pytest.mark.parametrize(
    ("flag", "target_name", "other_name"),
    [
        ("--z-ai-coding", "logout_z_ai_coding", "logout_z_ai_api"),
        ("--z-ai-api", "logout_z_ai_api", "logout_z_ai_coding"),
    ],
)
def test_cli_logout_zai_routes_dispatch_only_named_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    target_name: str,
    other_name: str,
) -> None:
    config = _patch_config(monkeypatch)
    target = Mock(side_effect=_success_event)
    other = Mock(side_effect=_success_event)
    monkeypatch.setattr(f"pythinker_code.cli.{target_name}", target, raising=False)
    monkeypatch.setattr(f"pythinker_code.cli.{other_name}", other, raising=False)

    result = runner.invoke(cli, ["logout", flag])

    assert result.exit_code == 0, result.output
    assert target.call_args.args == (config,)
    other.assert_not_called()


def test_cli_login_zai_json_emits_route_events(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    target = Mock(side_effect=_success_event)
    monkeypatch.setattr(
        "pythinker_code.cli.login_z_ai_coding_api_key",
        target,
        raising=False,
    )

    result = runner.invoke(
        cli,
        ["login", "--z-ai-coding", "--json"],
        input="route-key\n",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[-1])
    assert payload["type"] == "success"


def test_cli_login_zai_flags_are_mutually_exclusive() -> None:
    result = runner.invoke(cli, ["login", "--z-ai-coding", "--z-ai-api"])
    assert result.exit_code == 1
    assert "Choose only one" in result.output

    with_openai = runner.invoke(cli, ["login", "--z-ai-coding", "--api-key"])
    assert with_openai.exit_code == 1
    assert "Choose only one" in with_openai.output


def test_cli_logout_zai_flags_are_mutually_exclusive() -> None:
    result = runner.invoke(cli, ["logout", "--z-ai-coding", "--z-ai-api"])
    assert result.exit_code == 1
    assert "Choose only one" in result.output


def test_cli_login_blank_zai_key_fails_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    target = Mock(side_effect=_success_event)
    monkeypatch.setattr("pythinker_code.cli.login_z_ai_api_key", target, raising=False)

    result = runner.invoke(cli, ["login", "--z-ai-api"], input="\n")

    assert result.exit_code == 1
    assert "required" in result.output.lower()
    target.assert_not_called()
