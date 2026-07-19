from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import UUID

import pytest
from pythinker_host.path import HostPath

from pythinker_code.config import OAuthRef
from pythinker_code.session import Session
from pythinker_code.session_state import load_session_state, save_session_state
from pythinker_code.web.api import sessions as sessions_api
from pythinker_code.web.models import GenerateTitleRequest

if TYPE_CHECKING:
    from pythinker_code.web.runner.process import PythinkerCLIRunner


@pytest.fixture
def isolated_share_dir(monkeypatch, tmp_path: Path) -> Path:
    share_dir = tmp_path / "share"
    share_dir.mkdir()

    def _get_share_dir() -> Path:
        share_dir.mkdir(parents=True, exist_ok=True)
        return share_dir

    monkeypatch.setattr("pythinker_code.share.get_share_dir", _get_share_dir)
    monkeypatch.setattr("pythinker_code.metadata.get_share_dir", _get_share_dir)
    return share_dir


@pytest.fixture
def work_dir(tmp_path: Path) -> HostPath:
    path = tmp_path / "work"
    path.mkdir()
    return HostPath.unsafe_from_local_path(path)


class _FakeOAuthManager:
    def __init__(self, _config: object) -> None:
        self.ensured_ref: OAuthRef | None = None

    async def ensure_fresh(self, *, oauth_ref: OAuthRef | None = None) -> None:
        self.ensured_ref = oauth_ref


class _FakeRunner:
    """Stand-in for ``PythinkerCLIRunner`` for tests that bypass FastAPI dependency injection."""

    def get_session(self, _session_id: UUID) -> None:
        return None


class _FakeLLM:
    chat_provider = object()


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.message = _FakeMessage(text)


@pytest.mark.anyio
async def test_generate_title_preserves_concurrent_manual_title(
    isolated_share_dir: Path,
    work_dir: HostPath,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = await Session.create(work_dir)

    oauth_ref = OAuthRef(storage="file", key="oauth/test-provider")
    config = SimpleNamespace(
        default_model="test-model",
        models={"test-model": SimpleNamespace(provider="test-provider")},
        providers={"test-provider": SimpleNamespace(oauth=oauth_ref)},
    )
    oauth_manager = _FakeOAuthManager(config)

    monkeypatch.setattr("pythinker_code.config.load_config", lambda: config)
    monkeypatch.setattr(
        "pythinker_code.llm.create_llm",
        lambda provider_config, model_config, oauth=None: _FakeLLM(),
    )
    monkeypatch.setattr("pythinker_code.auth.oauth.OAuthManager", lambda _config: oauth_manager)

    async def fake_generate(*, chat_provider, system_prompt, tools, history):
        state = load_session_state(session.dir)
        state.custom_title = "Manual Title"
        state.title_generated = True
        save_session_state(state, session.dir)
        return _FakeResult("AI Title")

    monkeypatch.setattr("pythinker_core.generate", fake_generate)

    response = await sessions_api.generate_session_title(
        UUID(session.id),
        GenerateTitleRequest(
            user_message="debug the flaky web session rename issue",
            assistant_response="I'll inspect the session state writes.",
        ),
        runner=cast("PythinkerCLIRunner", _FakeRunner()),
    )

    state = load_session_state(session.dir)
    assert response.title == "Manual Title"
    assert state.custom_title == "Manual Title"
    assert state.title_generated is True
    assert state.title_generate_attempts == 0
    assert oauth_manager.ensured_ref == oauth_ref
