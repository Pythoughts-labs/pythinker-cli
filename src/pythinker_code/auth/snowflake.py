from __future__ import annotations

import base64
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

import aiohttp
from pydantic import SecretStr

from pythinker_code.auth import SNOWFLAKE_CORTEX_PLATFORM_ID
from pythinker_code.auth.oauth import (
    OAuthError,
    OAuthEvent,
    OAuthToken,
    OAuthUnauthorized,
    delete_tokens,
    save_tokens,
)
from pythinker_code.auth.oauth_flows import run_loopback_pkce_flow
from pythinker_code.auth.platforms import managed_model_key, managed_provider_key
from pythinker_code.config import Config, LLMModel, LLMProvider, OAuthRef, save_config
from pythinker_code.thinking import apply_login_thinking_defaults
from pythinker_code.utils.aiohttp import new_client_session

SNOWFLAKE_CLIENT_ID = "LOCAL_APPLICATION"
SNOWFLAKE_REDIRECT_PATH = "/"
SNOWFLAKE_PROVIDER_KEY = managed_provider_key(SNOWFLAKE_CORTEX_PLATFORM_ID)
SNOWFLAKE_OAUTH_KEY_PREFIX = "oauth/snowflake-cortex/"
SNOWFLAKE_JSON_HEADERS = {"Accept": "application/json"}
_ROLE_SIMPLE = re.compile(r"^[-_A-Za-z0-9]+$")
_DEFAULT_EXPIRES_IN = 600  # Snowflake access tokens are short-lived; PENDING-LIVE


def _skip_browser_open(_url: str) -> None:
    return None


def normalize_account(raw: str) -> str:
    account = raw.strip()
    account = re.sub(r"^https?://", "", account)
    account = re.sub(r"\.snowflakecomputing\.com/?$", "", account)
    return account.rstrip("/")


def _authorize_url(account: str) -> str:
    return f"https://{account}.snowflakecomputing.com/oauth/authorize"


def _token_url(account: str) -> str:
    return f"https://{account}.snowflakecomputing.com/oauth/token-request"


def _cortex_base_url(account: str) -> str:
    return f"https://{account}.snowflakecomputing.com/api/v2/cortex/v1"


def _oauth_ref(account: str) -> OAuthRef:
    return OAuthRef(storage="file", key=f"{SNOWFLAKE_OAUTH_KEY_PREFIX}{account}")


def _scope(role: str | None) -> str:
    if not role:
        return "refresh_token"
    if _ROLE_SIMPLE.match(role):
        return f"refresh_token session:role:{role}"
    return f"refresh_token session:role-encoded:{quote(role, safe='')}"


def _basic_header() -> str:
    raw = f"{SNOWFLAKE_CLIENT_ID}:{SNOWFLAKE_CLIENT_ID}"
    encoded = base64.b64encode(raw.encode(encoding="utf-8")).decode(encoding="utf-8")
    return f"Basic {encoded}"


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Authorization": _basic_header(),
    }


def _inject_default_expiry(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload.get("expires_in"):
        payload["expires_in"] = _DEFAULT_EXPIRES_IN
    return payload


@dataclass(frozen=True, slots=True)
class SnowflakeModel:
    model_id: str
    display_name: str
    max_context_size: int  # PENDING-LIVE: Cortex does not publish context windows

    @property
    def alias(self) -> str:
        return managed_model_key(SNOWFLAKE_CORTEX_PLATFORM_ID, self.model_id)


SNOWFLAKE_MODELS: tuple[SnowflakeModel, ...] = (
    SnowflakeModel("claude-sonnet-4-5", "Claude Sonnet 4.5 (Cortex)", 200_000),
    SnowflakeModel("openai-gpt-5", "OpenAI GPT-5 (Cortex)", 128_000),
    SnowflakeModel("llama3.1-405b", "Llama 3.1 405B (Cortex)", 128_000),
)


async def _post_form(
    endpoint: str,
    data: dict[str, str],
    *,
    operation: str,
    headers: dict[str, str],
) -> tuple[int, dict[str, Any]]:
    try:
        async with (
            new_client_session() as session,
            session.post(endpoint, data=dict(data), headers=headers) as response,
        ):
            status = response.status
            payload_any: Any = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
        raise OAuthError(f"{operation} request failed.") from exc

    if not isinstance(payload_any, dict):
        raise OAuthError(f"{operation} returned an invalid response.")
    return status, cast(dict[str, Any], payload_any)


async def _post_token(account: str, data: dict[str, str], *, operation: str) -> dict[str, Any]:
    status, payload = await _post_form(
        _token_url(account),
        data,
        operation=operation,
        headers=_headers(),
    )
    if status in {401, 403}:
        raise OAuthUnauthorized(f"{operation} was unauthorized.")
    if not 200 <= status < 300:
        raise OAuthError(f"{operation} failed (HTTP {status}).")
    return _inject_default_expiry(payload)


async def _exchange_code_for_tokens(
    account: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    return await _post_token(
        account,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": SNOWFLAKE_CLIENT_ID,
            "code_verifier": code_verifier,
        },
        operation="Snowflake authorization code exchange",
    )


async def refresh_snowflake_cortex_token(account: str, refresh_token: str) -> OAuthToken:
    payload = await _post_token(
        account,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": SNOWFLAKE_CLIENT_ID,
        },
        operation="Snowflake token refresh",
    )
    return OAuthToken.from_response(payload)


def _apply_snowflake_config(
    config: Config,
    account: str,
    models: tuple[SnowflakeModel, ...] = SNOWFLAKE_MODELS,
) -> None:
    config.providers[SNOWFLAKE_PROVIDER_KEY] = LLMProvider(
        type="openai_legacy",
        base_url=_cortex_base_url(account),
        api_key=SecretStr(""),
        oauth=_oauth_ref(account),
    )

    for alias, model in list(config.models.items()):
        if model.provider == SNOWFLAKE_PROVIDER_KEY:
            del config.models[alias]

    for model in models:
        config.models[model.alias] = LLMModel(
            provider=SNOWFLAKE_PROVIDER_KEY,
            model=model.model_id,
            max_context_size=model.max_context_size,
            display_name=model.display_name,
        )

    config.default_model = models[0].alias
    apply_login_thinking_defaults(config, thinking=False, effort="off")


async def login_snowflake(
    config: Config,
    account: str,
    role: str | None = None,
    *,
    open_browser: bool = True,
) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Login requires the default config file; restart without --config/--config-file.",
        )
        return

    account = normalize_account(account)
    if not account:
        yield OAuthEvent("error", "Snowflake account identifier is required.")
        return

    yield OAuthEvent("waiting", "Waiting for Snowflake browser authorization...")
    browser_open = None if open_browser else _skip_browser_open
    try:
        auth = await run_loopback_pkce_flow(
            authorize_endpoint=_authorize_url(account),
            client_id=SNOWFLAKE_CLIENT_ID,
            scope=_scope(role),
            redirect_path=SNOWFLAKE_REDIRECT_PATH,
            port=0,
            extra_authorize_params=None,
            browser_open=browser_open,
        )
        payload = await _exchange_code_for_tokens(
            account,
            auth.authorization_code,
            auth.code_verifier,
            auth.redirect_uri,
        )
    except OAuthError as exc:
        yield OAuthEvent("error", f"Snowflake Cortex browser login failed: {exc}")
        return

    if not payload.get("refresh_token"):
        yield OAuthEvent(
            "error",
            "Snowflake did not return a refresh token; "
            "ensure the integration issues refresh tokens.",
        )
        return

    save_tokens(_oauth_ref(account), OAuthToken.from_response(payload))
    _apply_snowflake_config(config, account)
    save_config(config)
    yield OAuthEvent("success", f"Snowflake Cortex configured with model {config.default_model}.")


async def logout_snowflake(config: Config) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Logout requires the default config file; restart without --config/--config-file.",
        )
        return

    provider = config.providers.get(SNOWFLAKE_PROVIDER_KEY)
    if provider and provider.oauth:
        delete_tokens(provider.oauth)
    config.providers.pop(SNOWFLAKE_PROVIDER_KEY, None)
    for alias, model in list(config.models.items()):
        if model.provider == SNOWFLAKE_PROVIDER_KEY:
            del config.models[alias]

    if config.default_model not in config.models:
        config.default_model = next(iter(config.models), "")
    save_config(config)
    yield OAuthEvent("success", "Logged out of Snowflake Cortex successfully.")
