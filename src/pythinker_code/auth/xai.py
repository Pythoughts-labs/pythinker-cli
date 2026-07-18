from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

import aiohttp
from pydantic import SecretStr

from pythinker_code.auth import XAI_PLATFORM_ID
from pythinker_code.auth.oauth import (
    OAuthError,
    OAuthEvent,
    OAuthToken,
    OAuthUnauthorized,
    delete_tokens,
    save_tokens,
)
from pythinker_code.auth.oauth_flows import (
    generate_state,
    poll_device_token,
    request_device_code,
    run_loopback_pkce_flow,
)
from pythinker_code.auth.platforms import managed_model_key, managed_provider_key
from pythinker_code.config import Config, LLMModel, LLMProvider, OAuthRef, save_config
from pythinker_code.thinking import apply_login_thinking_defaults
from pythinker_code.utils.aiohttp import new_client_session

XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_AUTHORIZE_URL = "https://auth.x.ai/oauth2/authorize"
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
XAI_DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
XAI_BASE_URL = "https://api.x.ai/v1"
XAI_REDIRECT_PORT = 56121
XAI_REDIRECT_PATH = "/callback"
XAI_OAUTH_KEY = "oauth/xai"
XAI_PROVIDER_KEY = managed_provider_key(XAI_PLATFORM_ID)
XAI_JSON_HEADERS = {"Accept": "application/json"}


@dataclass(frozen=True, slots=True)
class XAIModel:
    model_id: str
    display_name: str
    max_context_size: int

    @property
    def alias(self) -> str:
        return managed_model_key(XAI_PLATFORM_ID, self.model_id)


XAI_MODELS: tuple[XAIModel, ...] = (
    XAIModel("grok-4", "Grok 4", 256_000),
    XAIModel("grok-3", "Grok 3", 131_072),
    XAIModel("grok-3-mini", "Grok 3 Mini", 131_072),
)


def _skip_browser_open(_url: str) -> None:
    return None


def _xai_oauth_ref() -> OAuthRef:
    return OAuthRef(storage="file", key=XAI_OAUTH_KEY)


def _apply_xai_config(config: Config) -> None:
    config.providers[XAI_PROVIDER_KEY] = LLMProvider(
        type="openai_legacy",
        base_url=XAI_BASE_URL,
        api_key=SecretStr(""),
        oauth=_xai_oauth_ref(),
    )

    for alias, model in list(config.models.items()):
        if model.provider == XAI_PROVIDER_KEY:
            del config.models[alias]

    for model in XAI_MODELS:
        config.models[model.alias] = LLMModel(
            provider=XAI_PROVIDER_KEY,
            model=model.model_id,
            max_context_size=model.max_context_size,
            display_name=model.display_name,
        )

    config.default_model = XAI_MODELS[0].alias
    apply_login_thinking_defaults(config, thinking=False, effort="off")


def _error_description(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    value = cast(dict[str, Any], payload).get("error_description")
    return str(value) if value else ""


async def _post_token(data: dict[str, str], *, operation: str) -> dict[str, Any]:
    try:
        async with (
            new_client_session() as session,
            session.post(XAI_TOKEN_URL, data=data) as response,
        ):
            status = response.status
            try:
                payload_any: Any = await response.json(content_type=None)
            except ValueError:
                payload_any = None
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        raise OAuthError(f"{operation} request failed.") from exc

    if status in {401, 403}:
        raise OAuthUnauthorized(_error_description(payload_any) or f"{operation} was unauthorized.")
    if status != 200:
        raise OAuthError(_error_description(payload_any) or f"{operation} failed (HTTP {status}).")
    if not isinstance(payload_any, dict):
        raise OAuthError(f"{operation} returned an invalid response.")
    payload = cast(dict[str, Any], payload_any)
    return payload


async def _exchange_code_for_tokens(
    code: str, code_verifier: str, redirect_uri: str
) -> dict[str, Any]:
    return await _post_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": XAI_CLIENT_ID,
            "code_verifier": code_verifier,
        },
        operation="xAI authorization code exchange",
    )


async def refresh_xai_token(refresh_token: str) -> OAuthToken:
    payload = await _post_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": XAI_CLIENT_ID,
        },
        operation="xAI token refresh",
    )
    return OAuthToken.from_response(payload)


async def login_xai_browser(
    config: Config, *, open_browser: bool = True
) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Login requires the default config file; restart without --config/--config-file.",
        )
        return

    yield OAuthEvent("waiting", "Waiting for xAI Grok browser authorization...")
    nonce = generate_state()
    browser_open = None if open_browser else _skip_browser_open
    try:
        auth = await run_loopback_pkce_flow(
            authorize_endpoint=XAI_AUTHORIZE_URL,
            client_id=XAI_CLIENT_ID,
            scope=XAI_SCOPE,
            redirect_path=XAI_REDIRECT_PATH,
            port=XAI_REDIRECT_PORT,
            extra_authorize_params={"plan": "generic", "nonce": nonce},
            browser_open=browser_open,
        )
        payload = await _exchange_code_for_tokens(
            auth.authorization_code,
            auth.code_verifier,
            auth.redirect_uri,
        )
    except OAuthError as exc:
        yield OAuthEvent("error", f"xAI Grok browser login failed: {exc}")
        return

    save_tokens(_xai_oauth_ref(), OAuthToken.from_response(payload))
    _apply_xai_config(config)
    save_config(config)
    yield OAuthEvent("success", f"xAI Grok configured with model {config.default_model}.")


async def login_xai_headless(config: Config) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Login requires the default config file; restart without --config/--config-file.",
        )
        return

    try:
        device_code = await request_device_code(
            device_authorization_endpoint=XAI_DEVICE_CODE_URL,
            client_id=XAI_CLIENT_ID,
            scope=XAI_SCOPE,
            headers=XAI_JSON_HEADERS,
        )
    except OAuthError as exc:
        yield OAuthEvent("error", f"Failed to start xAI Grok device login: {exc}")
        return

    yield OAuthEvent(
        "verification_url",
        f"Open {device_code.verification_uri} and enter code {device_code.user_code}.",
        data={
            "verification_url": device_code.verification_uri,
            "user_code": device_code.user_code,
        },
    )
    yield OAuthEvent("waiting", "Waiting for xAI Grok device authorization...")

    try:
        payload = await poll_device_token(
            token_endpoint=XAI_TOKEN_URL,
            client_id=XAI_CLIENT_ID,
            device_code=device_code,
            headers=XAI_JSON_HEADERS,
        )
        token = OAuthToken.from_response(payload)
    except OAuthError as exc:
        yield OAuthEvent("error", f"xAI Grok device login failed: {exc}")
        return

    save_tokens(_xai_oauth_ref(), token)
    _apply_xai_config(config)
    save_config(config)
    yield OAuthEvent("success", f"xAI Grok configured with model {config.default_model}.")


async def logout_xai(config: Config) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Logout requires the default config file; restart without --config/--config-file.",
        )
        return

    delete_tokens(_xai_oauth_ref())
    config.providers.pop(XAI_PROVIDER_KEY, None)
    for alias, model in list(config.models.items()):
        if model.provider == XAI_PROVIDER_KEY:
            del config.models[alias]

    if config.default_model not in config.models:
        config.default_model = next(iter(config.models), "")
    save_config(config)
    yield OAuthEvent("success", "Logged out of xAI Grok successfully.")
