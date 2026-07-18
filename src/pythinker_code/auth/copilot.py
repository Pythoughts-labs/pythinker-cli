from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

import aiohttp
from pydantic import SecretStr

from pythinker_code.auth import GITHUB_COPILOT_PLATFORM_ID
from pythinker_code.auth.oauth import (
    OAuthError,
    OAuthEvent,
    OAuthToken,
    OAuthUnauthorized,
    delete_tokens,
    save_tokens,
)
from pythinker_code.auth.oauth_flows import poll_device_token, request_device_code
from pythinker_code.auth.platforms import managed_model_key, managed_provider_key
from pythinker_code.config import Config, LLMModel, LLMProvider, OAuthRef, save_config
from pythinker_code.thinking import apply_login_thinking_defaults
from pythinker_code.utils.aiohttp import new_client_session
from pythinker_code.utils.logging import logger

GITHUB_COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
GITHUB_COPILOT_SCOPE = "read:user"
GITHUB_DEVICE_CODE_ENDPOINT = "https://github.com/login/device/code"
GITHUB_TOKEN_ENDPOINT = "https://github.com/login/oauth/access_token"
GITHUB_COPILOT_TOKEN_ENDPOINT = "https://api.github.com/copilot_internal/v2/token"
GITHUB_COPILOT_OAUTH_KEY = "oauth/github-copilot"
GITHUB_COPILOT_PROVIDER_KEY = managed_provider_key(GITHUB_COPILOT_PLATFORM_ID)
COPILOT_BASE_URL = "https://api.githubcopilot.com"

GITHUB_JSON_HEADERS = {"Accept": "application/json"}
_EDITOR_VERSION = "vscode/1.99.0"
_EDITOR_PLUGIN_VERSION = "copilot-chat/0.26.7"
_USER_AGENT = "GitHubCopilotChat/0.26.7"
_GITHUB_API_VERSION = "2025-04-01"


def build_copilot_headers() -> dict[str, str]:
    return {
        "Copilot-Integration-Id": "vscode-chat",
        "Editor-Version": _EDITOR_VERSION,
        "Editor-Plugin-Version": _EDITOR_PLUGIN_VERSION,
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "Openai-Intent": "conversation-panel",
    }


def _build_exchange_headers(github_token: str) -> dict[str, str]:
    return {
        "Authorization": f"token {github_token}",
        "Editor-Version": _EDITOR_VERSION,
        "Editor-Plugin-Version": _EDITOR_PLUGIN_VERSION,
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
    }


@dataclass(frozen=True, slots=True)
class GitHubCopilotModel:
    model_id: str
    display_name: str
    max_context_size: int

    @property
    def alias(self) -> str:
        return managed_model_key(GITHUB_COPILOT_PLATFORM_ID, self.model_id)


GITHUB_COPILOT_MODELS: tuple[GitHubCopilotModel, ...] = (
    GitHubCopilotModel("gpt-4.1", "GPT-4.1", 1_000_000),
    GitHubCopilotModel("gpt-4o", "GPT-4o", 128_000),
    GitHubCopilotModel("o4-mini", "o4-mini", 200_000),
)


def _copilot_oauth_ref() -> OAuthRef:
    return OAuthRef(storage="file", key=GITHUB_COPILOT_OAUTH_KEY)


def _apply_copilot_config(config: Config) -> None:
    oauth_ref = _copilot_oauth_ref()
    config.providers[GITHUB_COPILOT_PROVIDER_KEY] = LLMProvider(
        type="openai_legacy",
        base_url=COPILOT_BASE_URL,
        api_key=SecretStr(""),
        oauth=oauth_ref,
        custom_headers=build_copilot_headers(),
    )

    for alias, model in list(config.models.items()):
        if model.provider == GITHUB_COPILOT_PROVIDER_KEY:
            del config.models[alias]

    for model in GITHUB_COPILOT_MODELS:
        config.models[model.alias] = LLMModel(
            provider=GITHUB_COPILOT_PROVIDER_KEY,
            model=model.model_id,
            max_context_size=model.max_context_size,
            display_name=model.display_name,
        )

    config.default_model = GITHUB_COPILOT_MODELS[0].alias
    apply_login_thinking_defaults(config, thinking=False, effort="off")


async def refresh_copilot_token(github_token: str) -> OAuthToken:
    try:
        async with (
            new_client_session() as session,
            session.get(
                GITHUB_COPILOT_TOKEN_ENDPOINT,
                headers=_build_exchange_headers(github_token),
            ) as response,
        ):
            status = response.status
            try:
                payload_any: Any = await response.json(content_type=None)
            except ValueError:
                payload_any = None
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        raise OAuthError("GitHub Copilot token exchange request failed.") from exc

    if status in {401, 403}:
        raise OAuthUnauthorized("GitHub Copilot token exchange was unauthorized.")
    if status != 200:
        raise OAuthError(f"GitHub Copilot token exchange failed (HTTP {status}).")
    if not isinstance(payload_any, dict):
        raise OAuthError("GitHub Copilot token exchange returned an invalid response.")

    payload = cast(dict[str, Any], payload_any)
    bearer = payload.get("token")
    expires_at = payload.get("expires_at")
    refresh_in = payload.get("refresh_in")
    if (
        not isinstance(bearer, str)
        or not bearer
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at <= 0
        or isinstance(refresh_in, bool)
        or not isinstance(refresh_in, int)
        or refresh_in <= 0
    ):
        raise OAuthError("GitHub Copilot token exchange returned an incomplete response.")

    return OAuthToken(
        access_token=bearer,
        refresh_token=github_token,
        expires_at=float(expires_at),
        scope=GITHUB_COPILOT_SCOPE,
        token_type="Bearer",
        expires_in=float(refresh_in),
    )


async def login_copilot(config: Config, *, open_browser: bool = True) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Login requires the default config file; restart without --config/--config-file.",
        )
        return

    try:
        device_code = await request_device_code(
            device_authorization_endpoint=GITHUB_DEVICE_CODE_ENDPOINT,
            client_id=GITHUB_COPILOT_CLIENT_ID,
            scope=GITHUB_COPILOT_SCOPE,
            headers=GITHUB_JSON_HEADERS,
        )
    except OAuthError as exc:
        yield OAuthEvent("error", f"Failed to start GitHub Copilot login: {exc}")
        return

    yield OAuthEvent(
        "verification_url",
        f"Open {device_code.verification_uri} and enter code {device_code.user_code}.",
        data={
            "verification_url": device_code.verification_uri,
            "user_code": device_code.user_code,
        },
    )
    if open_browser:
        try:
            from pythinker_code.utils.term import open_url_in_browser

            open_url_in_browser(device_code.verification_uri)
        except Exception as exc:
            logger.warning("Failed to open browser: {error}", error=exc)
    yield OAuthEvent("waiting", "Waiting for GitHub Copilot authorization...")

    try:
        payload = await poll_device_token(
            token_endpoint=GITHUB_TOKEN_ENDPOINT,
            client_id=GITHUB_COPILOT_CLIENT_ID,
            device_code=device_code,
            headers=GITHUB_JSON_HEADERS,
        )
        github_token = payload.get("access_token")
        if not isinstance(github_token, str) or not github_token:
            raise OAuthError("GitHub device token response was incomplete.")
        copilot_token = await refresh_copilot_token(github_token)
    except OAuthError as exc:
        yield OAuthEvent("error", f"GitHub Copilot login failed: {exc}")
        return

    token = OAuthToken(
        access_token=copilot_token.access_token,
        refresh_token=github_token,
        expires_at=copilot_token.expires_at,
        scope=copilot_token.scope,
        token_type=copilot_token.token_type,
        expires_in=copilot_token.expires_in,
    )
    save_tokens(_copilot_oauth_ref(), token)
    _apply_copilot_config(config)
    save_config(config)
    yield OAuthEvent(
        "success",
        f"GitHub Copilot configured with model {config.default_model}.",
    )


async def logout_copilot(config: Config) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Logout requires the default config file; restart without --config/--config-file.",
        )
        return

    delete_tokens(_copilot_oauth_ref())
    config.providers.pop(GITHUB_COPILOT_PROVIDER_KEY, None)
    for alias, model in list(config.models.items()):
        if model.provider == GITHUB_COPILOT_PROVIDER_KEY:
            del config.models[alias]

    if config.default_model not in config.models:
        config.default_model = next(iter(config.models), "")
    save_config(config)
    yield OAuthEvent("success", "Logged out of GitHub Copilot successfully.")
