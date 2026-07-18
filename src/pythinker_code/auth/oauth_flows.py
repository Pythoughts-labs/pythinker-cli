"""Provider-neutral helpers for OAuth device-code and loopback-PKCE flows."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple, cast
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

from pythinker_code.auth.oauth import OAuthDeviceExpired, OAuthError
from pythinker_code.utils.aiohttp import new_client_session

_DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_DEFAULT_DEVICE_INTERVAL = 5
_SLOW_DOWN_INCREMENT = 5


class OAuthAccessDenied(OAuthError):
    """The resource owner denied an OAuth authorization request."""


class OAuthStateMismatch(OAuthError):
    """The loopback callback did not contain the expected OAuth state."""


@dataclass(frozen=True, slots=True)
class PkceCodes:
    code_verifier: str
    code_challenge: str


@dataclass(frozen=True, slots=True)
class DeviceCode:
    user_code: str
    verification_uri: str
    device_code: str
    interval: int
    expires_in: int
    verification_uri_complete: str | None = None


class LoopbackAuthorization(NamedTuple):
    """Successful loopback result, with named fields and tuple unpacking."""

    authorization_code: str
    code_verifier: str
    redirect_uri: str


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode(encoding="utf-8", errors="replace").rstrip("=")


def generate_pkce() -> PkceCodes:
    """Generate an RFC 7636 verifier and its S256 challenge."""
    verifier = _base64url(secrets.token_bytes(32))
    verifier_bytes = verifier.encode(encoding="utf-8")
    challenge = _base64url(hashlib.sha256(verifier_bytes).digest())
    return PkceCodes(code_verifier=verifier, code_challenge=challenge)


def generate_state() -> str:
    """Generate a cryptographically random OAuth state value."""
    return secrets.token_urlsafe(32)


async def _post_form(
    endpoint: str, data: Mapping[str, str], *, operation: str
) -> tuple[int, dict[str, Any]]:
    try:
        async with (
            new_client_session() as session,
            session.post(endpoint, data=dict(data)) as response,
        ):
            status = response.status
            payload_any: Any = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
        raise OAuthError(f"{operation} request failed.") from exc

    if not isinstance(payload_any, dict):
        raise OAuthError(f"{operation} returned an invalid response.")
    return status, cast(dict[str, Any], payload_any)


async def request_device_code(
    *,
    device_authorization_endpoint: str,
    client_id: str,
    scope: str | Sequence[str] | None = None,
    extra_params: Mapping[str, str] | None = None,
) -> DeviceCode:
    """Request an RFC 8628 device code without beginning token polling."""
    data = dict(extra_params or {})
    data["client_id"] = client_id
    if scope:
        data["scope"] = scope if isinstance(scope, str) else " ".join(scope)

    status, payload = await _post_form(
        device_authorization_endpoint,
        data,
        operation="Device authorization",
    )
    if not 200 <= status < 300:
        raise OAuthError(f"Device authorization failed (HTTP {status}).")

    required = ("user_code", "verification_uri", "device_code", "expires_in")
    if any(not payload.get(field) for field in required):
        raise OAuthError("Device authorization response was incomplete.")
    try:
        interval = int(payload.get("interval") or _DEFAULT_DEVICE_INTERVAL)
        expires_in = int(payload["expires_in"])
    except (TypeError, ValueError) as exc:
        raise OAuthError("Device authorization response contained invalid timing values.") from exc
    if interval <= 0 or expires_in <= 0:
        raise OAuthError("Device authorization response contained invalid timing values.")

    complete = payload.get("verification_uri_complete")
    return DeviceCode(
        user_code=str(payload["user_code"]),
        verification_uri=str(payload["verification_uri"]),
        verification_uri_complete=str(complete) if complete else None,
        device_code=str(payload["device_code"]),
        interval=interval,
        expires_in=expires_in,
    )


async def poll_device_token(
    *,
    token_endpoint: str,
    client_id: str,
    device_code: DeviceCode,
    extra_params: Mapping[str, str] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Poll an RFC 8628 token endpoint until authorization succeeds or terminates.

    ``deadline`` is an absolute ``time.monotonic()`` value. When omitted, the
    device authorization's ``expires_in`` value defines the deadline.
    """
    effective_deadline = deadline
    if effective_deadline is None:
        effective_deadline = time.monotonic() + device_code.expires_in
    interval = float(device_code.interval)

    data = dict(extra_params or {})
    data.update(
        {
            "client_id": client_id,
            "device_code": device_code.device_code,
            "grant_type": _DEVICE_CODE_GRANT,
        }
    )

    while True:
        remaining = effective_deadline - time.monotonic()
        if remaining <= 0:
            raise OAuthDeviceExpired("Device authorization expired before completion.")
        await asyncio.sleep(min(interval, remaining))
        if time.monotonic() >= effective_deadline:
            raise OAuthDeviceExpired("Device authorization expired before completion.")

        status, payload = await _post_form(token_endpoint, data, operation="Device token polling")
        if time.monotonic() >= effective_deadline:
            raise OAuthDeviceExpired("Device authorization expired before completion.")
        error = str(payload.get("error") or "")
        if 200 <= status < 300 and not error:
            return payload
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += _SLOW_DOWN_INCREMENT
            continue
        if error == "expired_token":
            raise OAuthDeviceExpired("Device authorization expired.")
        if error == "access_denied":
            raise OAuthAccessDenied("Device authorization was denied.")
        raise OAuthError(f"Device token polling failed (HTTP {status}).")


def _authorize_url(
    *,
    authorize_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scope: str | Sequence[str],
    pkce: PkceCodes,
    state: str,
    extra_params: Mapping[str, str] | None,
) -> str:
    params = dict(extra_params or {})
    params.update(
        {
            "client_id": client_id,
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": "S256",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope if isinstance(scope, str) else " ".join(scope),
            "state": state,
        }
    )
    separator = "&" if "?" in authorize_endpoint else "?"
    return f"{authorize_endpoint}{separator}{urlencode(params)}"


def _open_browser(url: str) -> None:
    from pythinker_code.utils.term import open_url_in_browser

    open_url_in_browser(url)


async def _write_callback_response(
    writer: asyncio.StreamWriter, *, status: str, message: str
) -> None:
    body = bytes(message, encoding="utf-8")
    headers = bytes(
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n",
        encoding="utf-8",
    )
    writer.write(headers + body)
    await writer.drain()


async def _handle_loopback_callback(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    redirect_path: str,
    expected_state: str,
    result: asyncio.Future[str],
) -> None:
    try:
        line = await reader.readline()
        parts = line.decode(encoding="utf-8", errors="replace").strip().split()
        if len(parts) < 2 or parts[0] != "GET":
            await _write_callback_response(writer, status="404 Not Found", message="Not found.")
            return

        parsed = urlsplit(parts[1])
        if parsed.path != redirect_path:
            await _write_callback_response(writer, status="404 Not Found", message="Not found.")
            return

        params = parse_qs(parsed.query)
        state = params.get("state", [None])[0]
        if state != expected_state:
            await _write_callback_response(
                writer,
                status="400 Bad Request",
                message="OAuth callback state did not match.",
            )
            if not result.done():
                result.set_exception(OAuthStateMismatch("OAuth callback state did not match."))
            return

        error = params.get("error", [None])[0]
        if error:
            await _write_callback_response(
                writer,
                status="400 Bad Request",
                message="OAuth authorization was not completed.",
            )
            if not result.done():
                exc: OAuthError
                if error == "access_denied":
                    exc = OAuthAccessDenied("OAuth authorization was denied.")
                else:
                    exc = OAuthError("OAuth authorization callback reported an error.")
                result.set_exception(exc)
            return

        code = params.get("code", [None])[0]
        if not code:
            await _write_callback_response(
                writer,
                status="400 Bad Request",
                message="OAuth callback did not include an authorization code.",
            )
            if not result.done():
                result.set_exception(
                    OAuthError("OAuth callback did not include an authorization code.")
                )
            return

        await _write_callback_response(
            writer,
            status="200 OK",
            message="Authorization complete. You can close this window.",
        )
        if not result.done():
            result.set_result(code)
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError):
        if not result.done():
            result.set_exception(OAuthError("Failed to process the OAuth callback."))
    finally:
        writer.close()
        await writer.wait_closed()


def _server_port(server: asyncio.Server) -> int:
    sockets = server.sockets
    if not sockets:
        raise OAuthError("OAuth callback server did not expose a listening socket.")
    address = sockets[0].getsockname()
    if not isinstance(address, tuple):
        raise OAuthError("OAuth callback server returned an invalid address.")
    parts = cast("tuple[object, ...]", address)
    if len(parts) < 2 or not isinstance(parts[1], int):
        raise OAuthError("OAuth callback server returned an invalid address.")
    return parts[1]


async def run_loopback_pkce_flow(
    *,
    authorize_endpoint: str,
    client_id: str,
    scope: str | Sequence[str],
    redirect_path: str,
    port: int = 0,
    timeout: float = 15 * 60,
    extra_authorize_params: Mapping[str, str] | None = None,
    browser_open: Callable[[str], object] | None = None,
) -> LoopbackAuthorization:
    """Run an OAuth authorization-code flow using a 127.0.0.1 PKCE callback."""
    if not redirect_path.startswith("/"):
        raise ValueError("redirect_path must start with '/'.")
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535.")
    if timeout <= 0:
        raise ValueError("timeout must be positive.")

    pkce = generate_pkce()
    state = generate_state()
    result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    callback_tasks: set[asyncio.Task[None]] = set()

    def on_client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(
            _handle_loopback_callback(
                reader,
                writer,
                redirect_path=redirect_path,
                expected_state=state,
                result=result,
            )
        )
        callback_tasks.add(task)
        task.add_done_callback(callback_tasks.discard)

    try:
        server = await asyncio.start_server(on_client_connected, "127.0.0.1", port)
    except OSError as exc:
        raise OAuthError("Failed to start the OAuth callback server on 127.0.0.1.") from exc

    try:
        bound_port = _server_port(server)
        redirect_uri = f"http://127.0.0.1:{bound_port}{redirect_path}"
        authorize_url = _authorize_url(
            authorize_endpoint=authorize_endpoint,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            pkce=pkce,
            state=state,
            extra_params=extra_authorize_params,
        )
        try:
            (browser_open or _open_browser)(authorize_url)
        except Exception as exc:
            raise OAuthError("Failed to open a browser for OAuth authorization.") from exc

        try:
            authorization_code = await asyncio.wait_for(result, timeout=timeout)
        except TimeoutError as exc:
            raise OAuthError("Timed out waiting for the OAuth authorization callback.") from exc
        return LoopbackAuthorization(
            authorization_code=authorization_code,
            code_verifier=pkce.code_verifier,
            redirect_uri=redirect_uri,
        )
    finally:
        server.close()
        for task in callback_tasks:
            task.cancel()
        if callback_tasks:
            await asyncio.gather(*callback_tasks, return_exceptions=True)
        await server.wait_closed()


__all__ = [
    "DeviceCode",
    "LoopbackAuthorization",
    "OAuthAccessDenied",
    "OAuthStateMismatch",
    "PkceCodes",
    "generate_pkce",
    "generate_state",
    "poll_device_token",
    "request_device_code",
    "run_loopback_pkce_flow",
]
