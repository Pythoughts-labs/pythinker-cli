"""Auth-boundary behavior for the OpenAI Responses chat provider.

Regression coverage for the mid-session credential blank: when an OAuth refresh
token is rejected server-side (e.g. it was rotated on another machine), the
Pythinker OAuth refresh path blanks the *live* OpenAI client's ``api_key`` to
``""``. The OpenAI SDK then raises a bare ``TypeError`` from ``_validate_headers``
at request-build time. That ``TypeError`` is neither ``OpenAIError`` nor
``httpx.HTTPError``, so without conversion it escapes every handler as a fatal
"Unexpected error". The provider must convert it to a typed ``APIStatusError``
(401) so the standard refresh -> ``/login`` recovery engages instead.
"""

import pytest

from pythinker_core.chat_provider import APIStatusError
from pythinker_core.contrib.chat_provider.openai_responses import OpenAIResponses
from pythinker_core.message import Message


@pytest.mark.parametrize("stream", [True, False])
async def test_blanked_credential_raises_typed_401(stream: bool) -> None:
    # Construct with a valid key (the SDK enforces credentials at construction),
    # then simulate the live blank applied by the OAuth refresh path.
    provider = OpenAIResponses(model="gpt-5-codex", api_key="sk-valid-dummy", stream=stream)
    # Mirror _apply_access_token(runtime, ref, "") blanking the live client.
    provider._client.api_key = ""  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(APIStatusError) as exc_info:
        await provider.generate("You are helpful.", [], [Message(role="user", content="hi")])

    assert exc_info.value.status_code == 401


async def test_typeerror_with_valid_key_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    # A TypeError that is NOT the missing-credential case must still propagate,
    # so the conversion never becomes a bug-swallower.
    provider = OpenAIResponses(model="gpt-5-codex", api_key="sk-valid-dummy", stream=True)

    async def boom(*args: object, **kwargs: object) -> None:
        raise TypeError("unrelated bug, not an auth failure")

    monkeypatch.setattr(provider._client.responses, "create", boom)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(TypeError, match="unrelated bug"):
        await provider.generate("You are helpful.", [], [Message(role="user", content="hi")])
