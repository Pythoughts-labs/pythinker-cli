from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest

from pythinker_code.auth.oauth import OAuthDeviceExpired
from pythinker_code.auth.oauth_flows import (
    DeviceCode,
    OAuthAccessDenied,
    OAuthStateMismatch,
    generate_pkce,
    generate_state,
    poll_device_token,
    request_device_code,
    run_loopback_pkce_flow,
)


class _FakeResponse:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self.payload = payload

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def json(self, *, content_type: object = None) -> dict[str, Any]:
        return self.payload


class _FakeSession:
    def __init__(
        self,
        responses: list[_FakeResponse],
        calls: list[tuple[str, dict[str, str]]],
    ) -> None:
        self.responses = responses
        self.calls = calls

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def post(self, endpoint: str, *, data: dict[str, str]) -> _FakeResponse:
        self.calls.append((endpoint, data))
        return self.responses.pop(0)


def _mock_http(
    monkeypatch: pytest.MonkeyPatch,
    *responses: tuple[int, dict[str, Any]],
) -> list[tuple[str, dict[str, str]]]:
    queued = [_FakeResponse(status, payload) for status, payload in responses]
    calls: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        "pythinker_code.auth.oauth_flows.new_client_session",
        lambda: _FakeSession(queued, calls),
    )
    return calls


def test_generate_pkce_and_state_have_oauth_safe_shape() -> None:
    first = generate_pkce()
    second = generate_pkce()
    state = generate_state()

    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first.code_verifier)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first.code_challenge)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", state)
    verifier_bytes = first.code_verifier.encode(encoding="utf-8")
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier_bytes).digest())
        .decode(encoding="ascii", errors="replace")
        .rstrip("=")
    )
    assert first.code_challenge == expected_challenge
    assert first != second


@pytest.mark.asyncio
async def test_request_device_code_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_http(
        monkeypatch,
        (
            200,
            {
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://login.example/device",
                "verification_uri_complete": "https://login.example/device?user_code=ABCD-EFGH",
                "expires_in": 600,
                "interval": 3,
            },
        ),
    )

    authorization = await request_device_code(
        device_authorization_endpoint="https://login.example/oauth/device",
        client_id="client-id",
        scope=["openid", "profile"],
        extra_params={"audience": "example-api"},
    )

    assert authorization == DeviceCode(
        user_code="ABCD-EFGH",
        verification_uri="https://login.example/device",
        verification_uri_complete="https://login.example/device?user_code=ABCD-EFGH",
        device_code="device-secret",
        interval=3,
        expires_in=600,
    )
    assert calls == [
        (
            "https://login.example/oauth/device",
            {
                "audience": "example-api",
                "client_id": "client-id",
                "scope": "openid profile",
            },
        )
    ]


@pytest.mark.asyncio
async def test_poll_device_token_handles_pending_slow_down_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _mock_http(
        monkeypatch,
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        (200, {"access_token": "access-secret", "token_type": "Bearer"}),
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("pythinker_code.auth.oauth_flows.asyncio.sleep", fake_sleep)

    payload = await poll_device_token(
        token_endpoint="https://login.example/oauth/token",
        client_id="client-id",
        device_code=_device_code(interval=2),
    )

    assert payload == {"access_token": "access-secret", "token_type": "Bearer"}
    assert sleeps == [2, 2, 7]
    assert len(calls) == 3
    assert calls[0][1] == {
        "client_id": "client-id",
        "device_code": "device-secret",
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        ("expired_token", OAuthDeviceExpired),
        ("access_denied", OAuthAccessDenied),
    ],
)
async def test_poll_device_token_raises_typed_terminal_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: str,
    expected_error: type[Exception],
) -> None:
    _mock_http(monkeypatch, (400, {"error": error}))

    async def fake_sleep(delay: float) -> None:
        assert delay == 1

    monkeypatch.setattr("pythinker_code.auth.oauth_flows.asyncio.sleep", fake_sleep)

    with pytest.raises(expected_error):
        await poll_device_token(
            token_endpoint="https://login.example/oauth/token",
            client_id="client-id",
            device_code=_device_code(interval=1),
        )


def _device_code(*, interval: int) -> DeviceCode:
    return DeviceCode(
        user_code="ABCD-EFGH",
        verification_uri="https://login.example/device",
        verification_uri_complete=None,
        device_code="device-secret",
        interval=interval,
        expires_in=600,
    )


class _FakeSocket:
    def __init__(self, port: int) -> None:
        self.port = port

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", self.port)


class _FakeServer:
    def __init__(self, port: int) -> None:
        self.sockets = [_FakeSocket(port)]
        self.closed = False
        self.waited_closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited_closed = True


class _FakeWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _reader(request_target: str) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    request = f"GET {request_target} HTTP/1.1\r\n\r\n"
    reader.feed_data(bytes(request, encoding="utf-8"))
    reader.feed_eof()
    return reader


def _mock_loopback_server(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    _FakeServer,
    dict[str, Callable[[asyncio.StreamReader, asyncio.StreamWriter], None]],
]:
    server = _FakeServer(port=43123)
    captured: dict[str, Callable[[asyncio.StreamReader, asyncio.StreamWriter], None]] = {}

    async def fake_start_server(
        handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], None],
        host: str,
        port: int,
    ) -> _FakeServer:
        assert host == "127.0.0.1"
        assert port == 0
        captured["handler"] = handler
        return server

    monkeypatch.setattr("pythinker_code.auth.oauth_flows.asyncio.start_server", fake_start_server)
    return server, captured


@pytest.mark.asyncio
async def test_loopback_flow_rejects_state_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    server, captured = _mock_loopback_server(monkeypatch)
    writer = _FakeWriter()

    def open_browser(url: str) -> None:
        handler = captured["handler"]
        handler(
            _reader("/oauth/callback?code=authorization-code&state=wrong-state"),
            cast("asyncio.StreamWriter", writer),
        )

    with pytest.raises(OAuthStateMismatch):
        await run_loopback_pkce_flow(
            authorize_endpoint="https://login.example/oauth/authorize",
            client_id="client-id",
            scope="openid profile",
            redirect_path="/oauth/callback",
            browser_open=open_browser,
        )

    assert bytes(writer.buffer).startswith(b"HTTP/1.1 400 Bad Request")
    assert server.closed
    assert server.waited_closed


@pytest.mark.asyncio
async def test_loopback_flow_captures_code_with_ephemeral_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, captured = _mock_loopback_server(monkeypatch)
    opened_urls: list[str] = []
    writer = _FakeWriter()

    def open_browser(url: str) -> None:
        opened_urls.append(url)
        params = parse_qs(urlsplit(url).query)
        handler = captured["handler"]
        handler(
            _reader(f"/oauth/callback?code=authorization-code&state={params['state'][0]}"),
            cast("asyncio.StreamWriter", writer),
        )

    result = await run_loopback_pkce_flow(
        authorize_endpoint="https://login.example/oauth/authorize",
        client_id="client-id",
        scope=["openid", "profile"],
        redirect_path="/oauth/callback",
        extra_authorize_params={"prompt": "login"},
        browser_open=open_browser,
    )

    assert result.authorization_code == "authorization-code"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", result.code_verifier)
    assert result.redirect_uri == "http://127.0.0.1:43123/oauth/callback"
    params = parse_qs(urlsplit(opened_urls[0]).query)
    assert params["redirect_uri"] == [result.redirect_uri]
    assert params["scope"] == ["openid profile"]
    assert params["prompt"] == ["login"]
    assert params["code_challenge_method"] == ["S256"]
    assert bytes(writer.buffer).startswith(b"HTTP/1.1 200 OK")
    assert server.closed
    assert server.waited_closed
