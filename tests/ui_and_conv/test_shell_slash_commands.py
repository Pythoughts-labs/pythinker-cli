"""Tests for shell-level slash commands."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from pythinker_core.message import Message
from pythinker_host.path import HostPath

from pythinker_code.auth.oauth import OAuthEvent
from pythinker_code.cli import Reload
from pythinker_code.config import Config, LLMModel, LLMProvider
from pythinker_code.session import Session
from pythinker_code.subagents.models import AgentTypeDefinition, ToolPolicy
from pythinker_code.ui.shell.slash import ShellSlashCmdFunc, shell_mode_registry
from pythinker_code.ui.shell.slash import registry as shell_slash_registry
from pythinker_code.utils.slashcmd import SlashCommand
from pythinker_code.wire.types import MCPServerSnapshot, MCPStatusSnapshot, TextPart


async def _invoke_slash_command(command: SlashCommand[ShellSlashCmdFunc], shell: Any) -> None:
    ret = command.func(shell, "")
    if isinstance(ret, Awaitable):
        await ret


async def _oauth_success_event(*args: Any, **kwargs: Any) -> AsyncIterator[OAuthEvent]:
    yield OAuthEvent("success", "ok")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_share_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Provide an isolated share directory for metadata operations."""
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


@pytest.fixture
def mock_shell(work_dir: HostPath) -> Mock:
    """Create a mock Shell whose soul passes the PythinkerSoul isinstance check.

    The mock session is treated as non-empty so that /new does not attempt
    to delete it (delete would fail on a plain Mock because it is not awaitable).
    """
    from pythinker_code.soul.pythinkersoul import PythinkerSoul

    mock_soul = Mock(spec=PythinkerSoul)
    mock_soul.runtime.session.work_dir = work_dir
    mock_soul.runtime.session.id = "current-session-id"
    mock_soul.runtime.session.is_empty.return_value = False

    shell = Mock()
    shell.soul = mock_soul
    return shell


def test_shell_slash_aliases_are_registered() -> None:
    aliases = {
        "keybindings": "keys",
        "color": "theme",
        "tasks": "task",
        "session": "sessions",
        "cost": "usage",
        "status": "usage",
    }

    for alias, canonical in aliases.items():
        command = shell_slash_registry.find_command(alias)
        assert command is not None, alias
        assert command.name == canonical


async def test_shell_login_copilot_routes_to_copilot(monkeypatch: pytest.MonkeyPatch) -> None:
    from pythinker_code.ui.shell import oauth as shell_oauth

    login = Mock(side_effect=_oauth_success_event)
    config = Config(is_from_default_location=True)
    app = SimpleNamespace(soul=SimpleNamespace(runtime=SimpleNamespace(config=config)))
    monkeypatch.setattr(shell_oauth, "ensure_pythinker_soul", lambda _app: _app.soul)
    monkeypatch.setattr(shell_oauth, "login_copilot", login)

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(shell_oauth.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(shell_oauth.console, "clear", Mock())

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.login)(app, "copilot")

    assert login.called


async def test_shell_logout_copilot_routes_to_copilot(monkeypatch: pytest.MonkeyPatch) -> None:
    from pythinker_code.ui.shell import oauth as shell_oauth

    logout = Mock(side_effect=_oauth_success_event)
    config = Config(is_from_default_location=True)
    app = SimpleNamespace(soul=SimpleNamespace(runtime=SimpleNamespace(config=config)))
    monkeypatch.setattr(shell_oauth, "ensure_pythinker_soul", lambda _app: _app.soul)
    monkeypatch.setattr(shell_oauth, "logout_copilot", logout)

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(shell_oauth.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(shell_oauth.console, "clear", Mock())

    with pytest.raises(Reload):
        await cast(Any, shell_oauth.logout)(app, "copilot")

    assert logout.called


async def test_model_switch_starts_fresh_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing models should reload into a new session so old context is not reused."""
    from pythinker_code.soul.pythinkersoul import PythinkerSoul
    from pythinker_code.ui.shell import model_picker
    from pythinker_code.ui.shell import slash as shell_slash

    config = Config(
        is_from_default_location=True,
        default_model="model-a",
        # Pin the start-fresh path; carry-over has its own coverage in
        # tests/core/test_model_switch_carryover.py.
        model_switch_carryover=False,
        providers={
            "test-provider": LLMProvider(
                type="pythinker",
                base_url="https://example.test",
                api_key=SecretStr("test-key"),
            )
        },
        models={
            "model-a": LLMModel(
                provider="test-provider",
                model="model-a",
                max_context_size=100_000,
            ),
            "model-b": LLMModel(
                provider="test-provider",
                model="model-b",
                max_context_size=100_000,
            ),
        },
    )
    current_session = SimpleNamespace(
        id="current-session",
        work_dir=HostPath("/tmp/work"),
        state=SimpleNamespace(additional_dirs=["/extra"]),
    )
    mock_soul = Mock(spec=PythinkerSoul)
    mock_soul.runtime = SimpleNamespace(
        config=config,
        llm=SimpleNamespace(model_config=config.models["model-a"]),
        session=current_session,
    )
    mock_soul.thinking_effort = "off"
    mock_soul.thinking = False
    shell = SimpleNamespace(soul=mock_soul)

    class _ModelPicker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run(self) -> str:
            return "model-b"

    saved_configs: list[Config] = []

    async def fake_refresh_managed_models(_config: Config) -> None:
        return None

    fresh_session = SimpleNamespace(
        id="fresh-session",
        state=SimpleNamespace(additional_dirs=[]),
        save_state=Mock(),
    )
    threaded_calls: list[Any] = []

    async def fake_create_session(work_dir: HostPath) -> SimpleNamespace:
        assert work_dir == current_session.work_dir
        return fresh_session

    async def fake_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        threaded_calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(shell_slash, "refresh_managed_models", fake_refresh_managed_models)
    monkeypatch.setattr(model_picker, "ModelPickerApp", _ModelPicker)
    monkeypatch.setattr(shell_slash, "load_config", lambda: config.model_copy(deep=True))
    monkeypatch.setattr(shell_slash, "save_config", saved_configs.append)
    monkeypatch.setattr(shell_slash.Session, "create", fake_create_session)
    monkeypatch.setattr(shell_slash.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(shell_slash.console, "print", Mock())

    cmd = shell_slash_registry.find_command("model")
    assert cmd is not None
    with pytest.raises(Reload) as exc_info:
        await _invoke_slash_command(cmd, shell)

    assert exc_info.value.session_id == "fresh-session"
    assert fresh_session.state.additional_dirs == ["/extra"]
    assert threaded_calls == [fresh_session.save_state]
    fresh_session.save_state.assert_called_once_with()
    assert saved_configs[-1].default_model == "model-b"


async def test_mcp_slash_persists_only_final_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    loading = MCPStatusSnapshot(
        loading=True,
        connected=0,
        total=1,
        tools=0,
        servers=(MCPServerSnapshot(name="context7", status="connecting", tools=()),),
    )
    connected = MCPStatusSnapshot(
        loading=False,
        connected=1,
        total=1,
        tools=2,
        servers=(
            MCPServerSnapshot(
                name="context7",
                status="connected",
                tools=("resolve-library-id", "query-docs"),
            ),
        ),
    )

    class _Soul:
        def __init__(self) -> None:
            self.status = self
            self._snapshots = [loading, connected, connected]

        @property
        def mcp_status(self) -> MCPStatusSnapshot:
            if len(self._snapshots) > 1:
                return self._snapshots.pop(0)
            return self._snapshots[0]

        async def start_background_mcp_loading(self) -> bool:
            return False

        async def wait_for_background_mcp_loading(self) -> None:
            return None

    live_transient_values: list[bool] = []

    class _Live:
        def __init__(self, *args: Any, transient: bool, **kwargs: Any) -> None:
            live_transient_values.append(transient)

        def __enter__(self) -> _Live:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def update(self, *args: Any, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr("pythinker_code.ui.shell.slash.ensure_pythinker_soul", lambda _app: _Soul())
    monkeypatch.setattr("rich.live.Live", _Live)

    cmd = shell_slash_registry.find_command("mcp")
    assert cmd is not None
    await _invoke_slash_command(cmd, SimpleNamespace())

    from pythinker_code.ui.shell.components.render_utils import sanitize_ansi

    output = sanitize_ansi(capsys.readouterr().out)
    assert live_transient_values == [True]
    assert "🔌  MCP Tools" in output
    assert "context7" in output
    assert "Status: connected" in output
    assert "query-docs" in output
    assert "resolve-library-id" in output
    assert "Loading MCP inventory" not in output
    assert "MCP Servers:" not in output


# ---------------------------------------------------------------------------
# /changelog — rendering
# ---------------------------------------------------------------------------


class TestChangelogCommand:
    def test_registered_in_shell_mode_with_release_notes_alias(self) -> None:
        cmd = shell_mode_registry.find_command("changelog")
        assert cmd is not None
        assert shell_mode_registry.find_command("release-notes") is cmd

    def test_renders_release_notes_with_pythinker_markdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from rich.console import Console

        from pythinker_code.ui.shell import slash as shell_slash
        from pythinker_code.ui.shell.components.markdown import PythinkerMarkdown
        from pythinker_code.utils.changelog import ReleaseEntry

        monkeypatch.setattr(
            shell_slash,
            "CHANGELOG",
            {
                "0.31.0": ReleaseEntry(
                    description="Rendered **markdown** release notes.",
                    entries=[
                        "Added `inline code` and **bold** entries.",
                        "Lib: internal-only dependency note.",
                    ],
                )
            },
        )
        monkeypatch.setattr(shell_slash.console, "pager", lambda *_, **__: nullcontext())
        printed: list[object] = []
        monkeypatch.setattr(
            shell_slash.console, "print", lambda *args, **_kwargs: printed.extend(args)
        )

        cmd = shell_mode_registry.find_command("changelog")
        assert cmd is not None
        cmd.func(cast(Any, SimpleNamespace()), "")

        assert len(printed) == 1
        assert isinstance(printed[0], PythinkerMarkdown)

        console = Console(width=80, record=True, color_system=None)
        console.print(printed[0])
        output = console.export_text()

        assert "0.31.0" in output
        assert "Rendered markdown release notes." in output
        assert "Added inline code and bold entries." in output
        assert "Lib:" not in output


# ---------------------------------------------------------------------------
# /agents — registration/behaviour
# ---------------------------------------------------------------------------


class TestAgentsCommand:
    def test_registered_in_both_shell_registries(self) -> None:
        assert shell_slash_registry.find_command("agents") is not None
        assert shell_mode_registry.find_command("agents") is not None

    async def test_renders_registered_subagent_types(self, mock_shell: Mock, capsys: Any) -> None:
        mock_shell.soul.runtime.labor_market.builtin_types = {
            "explore": AgentTypeDefinition(
                name="explore",
                description="Map code",
                agent_file=Path("explore.yaml"),
                when_to_use="Read-only codebase reconnaissance",
                tool_policy=ToolPolicy(mode="inherit"),
            ),
            "implementer": AgentTypeDefinition(
                name="implementer",
                description="Patch code",
                agent_file=Path("implementer.yaml"),
                tool_policy=ToolPolicy(mode="allowlist", tools=("Read", "Edit")),
            ),
        }
        cmd = shell_slash_registry.find_command("agents")
        assert cmd is not None

        await _invoke_slash_command(cmd, mock_shell)

        output = capsys.readouterr().out
        assert "Agents" in output
        assert "explore" in output
        assert "Read-only codebase reconnaissance" in output
        assert "allow 2" in output

    async def test_handles_missing_labor_market(self, mock_shell: Mock, capsys: Any) -> None:
        mock_shell.soul.runtime.labor_market = None
        cmd = shell_slash_registry.find_command("agents")
        assert cmd is not None

        await _invoke_slash_command(cmd, mock_shell)

        assert "No subagents" in capsys.readouterr().out

    async def test_handles_missing_tool_policy(self, mock_shell: Mock, capsys: Any) -> None:
        mock_shell.soul.runtime.labor_market.builtin_types = {
            "legacy": SimpleNamespace(
                name="legacy",
                description="Legacy worker",
                when_to_use="",
                default_model=None,
                tool_policy=None,
            )
        }
        cmd = shell_slash_registry.find_command("agents")
        assert cmd is not None

        await _invoke_slash_command(cmd, mock_shell)

        output = capsys.readouterr().out
        assert "legacy" in output
        assert "inherit" in output


# ---------------------------------------------------------------------------
# /new — registration
# ---------------------------------------------------------------------------


class TestNewCommandRegistration:
    """Verify /new is registered in the correct registries."""

    def test_registered_in_shell_registry(self) -> None:
        cmd = shell_slash_registry.find_command("new")
        assert cmd is not None
        assert cmd.name == "new"
        assert cmd.description == "Start a new session"

    def test_not_in_shell_mode_registry(self) -> None:
        """/new should NOT be available in shell mode (Ctrl-X toggle)."""
        assert shell_mode_registry.find_command("new") is None

    def test_not_in_soul_registry(self) -> None:
        """/new should NOT appear in soul-level commands (Web UI visibility)."""
        from pythinker_code.soul.slash import registry as soul_slash_registry

        assert soul_slash_registry.find_command("new") is None


# ---------------------------------------------------------------------------
# /new — behaviour
# ---------------------------------------------------------------------------


class TestNewCommandBehavior:
    """Verify /new creates a new session and raises Reload."""

    async def test_raises_reload_with_new_session_id(
        self, isolated_share_dir: Path, mock_shell: Mock
    ) -> None:
        cmd = shell_slash_registry.find_command("new")
        assert cmd is not None

        with pytest.raises(Reload) as exc_info:
            await _invoke_slash_command(cmd, mock_shell)

        session_id = exc_info.value.session_id
        assert session_id is not None
        assert session_id != "current-session-id"

    async def test_new_session_persisted_on_disk(
        self, isolated_share_dir: Path, work_dir: HostPath, mock_shell: Mock
    ) -> None:
        cmd = shell_slash_registry.find_command("new")
        assert cmd is not None

        with pytest.raises(Reload) as exc_info:
            await _invoke_slash_command(cmd, mock_shell)

        session_id = exc_info.value.session_id
        assert session_id is not None
        new_session = await Session.find(work_dir, session_id)
        assert new_session is not None
        assert new_session.context_file.exists()
        assert new_session.context_file.stat().st_size == 0  # empty context

    async def test_consecutive_calls_produce_unique_ids(
        self, isolated_share_dir: Path, mock_shell: Mock
    ) -> None:
        cmd = shell_slash_registry.find_command("new")
        assert cmd is not None

        ids: list[str] = []
        for _ in range(3):
            with pytest.raises(Reload) as exc_info:
                await _invoke_slash_command(cmd, mock_shell)
            session_id = exc_info.value.session_id
            assert session_id is not None
            ids.append(session_id)

        assert len(set(ids)) == 3

    async def test_returns_early_without_pythinker_soul(self) -> None:
        """When soul is not a PythinkerSoul, the command should silently return."""
        shell = Mock()
        shell.soul = Mock()  # plain Mock, not spec=PythinkerSoul

        cmd = shell_slash_registry.find_command("new")
        assert cmd is not None

        # Should return without raising Reload
        await _invoke_slash_command(cmd, shell)


# ---------------------------------------------------------------------------
# /new — empty-session cleanup
# ---------------------------------------------------------------------------


def _write_context_message(context_file: Path, text: str) -> None:
    """Write a user message to a context file to make the session non-empty."""
    context_file.parent.mkdir(parents=True, exist_ok=True)
    message = Message(role="user", content=[TextPart(text=text)])
    context_file.write_text(message.model_dump_json(exclude_none=True) + "\n", encoding="utf-8")


class TestNewCommandSessionCleanup:
    """Verify /new cleans up the current session when it is empty."""

    async def test_deletes_empty_current_session(
        self, isolated_share_dir: Path, work_dir: HostPath
    ) -> None:
        """An empty current session should be removed to avoid orphan directories."""
        from pythinker_code.soul.pythinkersoul import PythinkerSoul

        empty_session = await Session.create(work_dir)
        assert empty_session.is_empty()
        session_dir = empty_session.work_dir_meta.sessions_dir / empty_session.id
        assert session_dir.exists()

        mock_soul = Mock(spec=PythinkerSoul)
        mock_soul.runtime.session = empty_session
        shell = Mock()
        shell.soul = mock_soul

        cmd = shell_slash_registry.find_command("new")
        assert cmd is not None
        with pytest.raises(Reload):
            await _invoke_slash_command(cmd, shell)

        # The empty session directory should have been cleaned up
        assert not session_dir.exists()

    async def test_preserves_non_empty_current_session(
        self, isolated_share_dir: Path, work_dir: HostPath
    ) -> None:
        """A session that already has content must NOT be deleted."""
        from pythinker_code.soul.pythinkersoul import PythinkerSoul

        session_with_content = await Session.create(work_dir)
        _write_context_message(session_with_content.context_file, "hello world")
        assert not session_with_content.is_empty()
        session_dir = session_with_content.work_dir_meta.sessions_dir / session_with_content.id

        mock_soul = Mock(spec=PythinkerSoul)
        mock_soul.runtime.session = session_with_content
        shell = Mock()
        shell.soul = mock_soul

        cmd = shell_slash_registry.find_command("new")
        assert cmd is not None
        with pytest.raises(Reload):
            await _invoke_slash_command(cmd, shell)

        # The non-empty session directory must still exist
        assert session_dir.exists()

    async def test_chained_new_does_not_accumulate_empty_sessions(
        self, isolated_share_dir: Path, work_dir: HostPath
    ) -> None:
        """Calling /new repeatedly should not leave orphan empty sessions."""
        from pythinker_code.soul.pythinkersoul import PythinkerSoul

        cmd = shell_slash_registry.find_command("new")
        assert cmd is not None

        # Simulate: session A (empty) → /new → session B (empty) → /new → session C
        session_a = await Session.create(work_dir)
        dir_a = session_a.work_dir_meta.sessions_dir / session_a.id

        mock_soul = Mock(spec=PythinkerSoul)
        mock_soul.runtime.session = session_a
        shell = Mock()
        shell.soul = mock_soul

        # First /new: A is empty → cleaned up, B created
        with pytest.raises(Reload) as exc_info:
            await _invoke_slash_command(cmd, shell)
        session_b_id = exc_info.value.session_id
        assert session_b_id is not None
        session_b = await Session.find(work_dir, session_b_id)
        assert session_b is not None
        dir_b = session_b.work_dir_meta.sessions_dir / session_b.id

        assert not dir_a.exists()  # A cleaned up
        assert dir_b.exists()  # B exists

        # Second /new: B is empty → cleaned up, C created
        mock_soul.runtime.session = session_b
        with pytest.raises(Reload) as exc_info:
            await _invoke_slash_command(cmd, shell)
        session_c_id = exc_info.value.session_id
        assert session_c_id is not None

        assert not dir_b.exists()  # B cleaned up
        session_c = await Session.find(work_dir, session_c_id)
        assert session_c is not None
