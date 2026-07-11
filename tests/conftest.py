"""Test configuration and fixtures."""
# ruff: noqa: E402 -- terminal-capability env must be pinned before pythinker imports

from __future__ import annotations

import os
import platform
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

# Pin a capable terminal for the whole test session BEFORE any pythinker import
# runs, because ``ui.shell.glyphs`` freezes its glyph constants from
# ``terminal_capabilities`` at module load. CI runs with ``NO_COLOR=1`` and
# ``TERM=dumb`` to keep build logs clean; left untouched those would degrade
# every renderer to ASCII glyphs and stripped color, breaking the UI-contract
# tests that assert the rich appearance. The degraded fallbacks themselves are
# covered explicitly by tests/ui_and_conv/test_terminal_capabilities.py, which
# sets these vars per-test and reloads. (Don't set PYTHINKER_TUI_GLYPHS=unicode
# here: that mode short-circuits the TERM=dumb check and would break the
# fallback tests; popping it and relying on TERM != dumb + UTF-8 stdout is
# enough.)
for _capability_var in (
    "NO_COLOR",
    "PYTHINKER_NO_COLOR",
    "CLICOLOR",
    "PYTHINKER_TUI_GLYPHS",
    "PYTHINKER_ASCII_UI",
    "PYTHINKER_SAFE_GLYPHS",
    "PYTHINKER_REDUCED_MOTION",
    "PYTHINKER_NO_ANIMATION",
    "PYTHINKER_STATIC_OUTPUT",
    # Drop every truecolor-promoting signal so the pinned ``TERM`` below fixes
    # the color tier at 256 (matching CI). A dev shell — e.g. a VS Code-family
    # terminal — leaks ``COLORTERM``/``TERM_PROGRAM`` that ``color_depth()``
    # honors, which would otherwise flip the tier to truecolor and break the
    # 256-tier shimmer/motion contract tests. Truecolor tests opt in by setting
    # ``COLORTERM`` explicitly.
    "COLORTERM",
    "WT_SESSION",
    "TERM_PROGRAM",
):
    os.environ.pop(_capability_var, None)
os.environ["TERM"] = "xterm-256color"

import pytest
from pydantic import SecretStr
from pythinker_core.chat_provider.mock import MockChatProvider
from pythinker_host import get_current_host, reset_current_host, set_current_host
from pythinker_host.local import LocalHost
from pythinker_host.path import HostPath

from pythinker_code.auth.oauth import OAuthManager
from pythinker_code.background import BackgroundTaskManager
from pythinker_code.config import Config, PythinkerAISearchConfig, get_default_config
from pythinker_code.llm import ALL_MODEL_CAPABILITIES, LLM
from pythinker_code.metadata import WorkDirMeta
from pythinker_code.notifications import NotificationManager
from pythinker_code.plugin import loader as plugin_loader
from pythinker_code.session import Session
from pythinker_code.session_state import SessionState
from pythinker_code.soul.agent import BuiltinSystemPromptArgs, LaborMarket, Runtime
from pythinker_code.soul.approval import Approval
from pythinker_code.soul.denwarenji import DenwaRenji
from pythinker_code.soul.toolset import PythinkerToolset
from pythinker_code.subagents import AgentTypeDefinition, ToolPolicy
from pythinker_code.tools.agent import Agent as AgentTool
from pythinker_code.tools.background import (
    TaskHandoff,
    TaskInput,
    TaskList,
    TaskOutput,
    TaskStop,
)
from pythinker_code.tools.dmail import SendDMail
from pythinker_code.tools.file.glob import Glob
from pythinker_code.tools.file.grep_local import Grep, SmartSearch
from pythinker_code.tools.file.read import ReadFile
from pythinker_code.tools.file.read_media import ReadMediaFile
from pythinker_code.tools.file.replace import StrReplaceFile
from pythinker_code.tools.file.write import WriteFile
from pythinker_code.tools.shell import Shell
from pythinker_code.tools.think import Think
from pythinker_code.tools.todo import SetTodoList
from pythinker_code.tools.web.fetch import FetchURL
from pythinker_code.tools.web.search import SearchWeb
from pythinker_code.utils.environment import Environment
from pythinker_code.wire.file import WireFile


@pytest.fixture
def config() -> Config:
    """Create a Config instance."""
    conf = get_default_config()
    conf.services.pythinker_ai_search = PythinkerAISearchConfig(
        base_url="https://api.pythinker.com/coding/v1/search",
        api_key=SecretStr("test-api-key"),
    )
    return conf


@pytest.fixture
def llm() -> LLM:
    """Create a LLM instance."""
    return LLM(
        chat_provider=MockChatProvider([]),
        max_context_size=100_000,
        capabilities=ALL_MODEL_CAPABILITIES,
    )


@pytest.fixture
def temp_work_dir() -> Generator[HostPath]:
    """Create a temporary working directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = Path.cwd()
        p = Path(tmpdir).resolve()
        os.chdir(p)
        token = set_current_host(LocalHost())
        try:
            yield HostPath.unsafe_from_local_path(p)
        finally:
            reset_current_host(token)
            os.chdir(original_cwd)


@pytest.fixture
def temp_share_dir() -> Generator[Path]:
    """Create a temporary shared directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture(autouse=True)
def _isolate_share_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point ``PYTHINKER_SHARE_DIR`` at a unique temp dir for every test.

    ``get_share_dir()`` defaults to ``~/.pythinker`` when the env var is unset, so any
    test that builds a ``ProjectMemoryStore``/store without explicit isolation would
    otherwise write into the real user home. This guarantees isolation by default; tests
    that set ``PYTHINKER_SHARE_DIR`` themselves still override it within their own scope.
    """
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path_factory.mktemp("share")))


@pytest.fixture(autouse=True)
def _isolate_external_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize Claude/Codex plugin discovery so the suite never reads the
    developer's real ``~/.claude``/``~/.codex`` plugins.

    External plugin auto-detection (``plugins.discover_external`` defaults on)
    makes agent/skill/command discovery scan those roots. Left unpatched,
    discovery results would depend on whatever plugins a given machine has
    installed, making tests non-deterministic across dev boxes and CI. Tests
    that exercise external discovery monkeypatch these roots to their own temp
    dirs, which overrides this default within their own scope.
    """
    monkeypatch.setattr(plugin_loader, "claude_plugin_roots", lambda: [])
    monkeypatch.setattr(plugin_loader, "codex_plugin_roots", lambda: [])


@pytest.fixture
def builtin_args(temp_work_dir: HostPath) -> BuiltinSystemPromptArgs:
    """Create builtin arguments with temporary work directory."""
    return BuiltinSystemPromptArgs(
        PYTHINKER_NOW="1970-01-01T00:00:00+00:00",
        PYTHINKER_WORK_DIR=temp_work_dir,
        PYTHINKER_WORK_DIR_LS="Test ls content",
        PYTHINKER_AGENTS_MD="Test agents content",
        PYTHINKER_SKILLS="No skills found.",
        PYTHINKER_ADDITIONAL_DIRS_INFO="",
        PYTHINKER_OS="macOS",
        PYTHINKER_SHELL="bash (`/bin/bash`)",
    )


@pytest.fixture
def denwa_renji() -> DenwaRenji:
    """Create a DenwaRenji instance."""
    return DenwaRenji()


@pytest.fixture
def session(temp_work_dir: HostPath, temp_share_dir: Path) -> Session:
    """Create a Session instance."""
    return Session(
        id="test",
        work_dir=temp_work_dir,
        work_dir_meta=WorkDirMeta(path=str(temp_work_dir), host=get_current_host().name),
        context_file=temp_share_dir / "context.jsonl",
        wire_file=WireFile(path=temp_share_dir / "wire.jsonl"),
        state=SessionState(),
        title="Test Session",
        updated_at=0.0,
    )


@pytest.fixture
def approval() -> Approval:
    """Create a Approval instance."""
    return Approval(yolo=True)


@pytest.fixture
def labor_market() -> LaborMarket:
    """Create a LaborMarket instance."""
    return LaborMarket()


@pytest.fixture
def environment() -> Environment:
    """Create an Environment instance."""
    if platform.system() == "Windows":
        return Environment(
            os_kind="Windows",
            os_arch="x86_64",
            os_version="1.0",
            shell_name="Windows PowerShell",
            shell_path=HostPath("powershell.exe"),
        )
    else:
        return Environment(
            os_kind="Unix",
            os_arch="aarch64",
            os_version="1.0",
            shell_name="bash",
            shell_path=HostPath("/bin/bash"),
        )


@pytest.fixture
def runtime(
    config: Config,
    llm: LLM,
    builtin_args: BuiltinSystemPromptArgs,
    denwa_renji: DenwaRenji,
    session: Session,
    approval: Approval,
    labor_market: LaborMarket,
    environment: Environment,
) -> Runtime:
    """Create a Runtime instance."""
    from pythinker_code.skill import SkillCatalog

    notifications = NotificationManager(
        session.context_file.parent / "notifications", config.notifications
    )
    rt = Runtime(
        config=config,
        llm=llm,
        builtin_args=builtin_args,
        denwa_renji=denwa_renji,
        session=session,
        approval=approval,
        labor_market=labor_market,
        environment=environment,
        notifications=notifications,
        background_tasks=BackgroundTaskManager(
            session,
            config.background,
            notifications=notifications,
        ),
        skill_catalog=SkillCatalog({}, ()),
        skills={},
        oauth=OAuthManager(config),
        additional_dirs=[],
        skills_dirs=[],
        role="root",
    )
    rt.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="mocker",
            description="The mock agent for testing purposes.",
            agent_file=Path("/tmp/mocker-agent.yaml"),
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    return rt


@pytest.fixture
def toolset() -> PythinkerToolset:
    return PythinkerToolset()


@contextmanager
def tool_call_context(
    tool_name: str, arguments: dict[str, object] | None = None
) -> Generator[None]:
    """Create a tool call context. Pass ``arguments`` to populate the call payload."""
    import json

    from pythinker_code.soul.toolset import current_tool_call
    from pythinker_code.wire.types import ToolCall

    encoded = json.dumps(arguments) if arguments is not None else None
    token = current_tool_call.set(
        ToolCall(id="test", function=ToolCall.FunctionBody(name=tool_name, arguments=encoded))
    )
    try:
        yield
    finally:
        current_tool_call.reset(token)


@pytest.fixture
def agent_tool(runtime: Runtime) -> AgentTool:
    """Create an Agent tool instance."""
    return AgentTool(runtime)


@pytest.fixture
def send_dmail_tool(denwa_renji: DenwaRenji) -> SendDMail:
    """Create a SendDMail tool instance."""
    return SendDMail(denwa_renji)


@pytest.fixture
def think_tool() -> Think:
    """Create a Think tool instance."""
    return Think()


@pytest.fixture
def set_todo_list_tool(runtime: Runtime) -> SetTodoList:
    """Create a SetTodoList tool instance."""
    return SetTodoList(runtime)


@pytest.fixture
def shell_tool(approval: Approval, environment: Environment, runtime: Runtime) -> Generator[Shell]:
    """Create a Shell tool instance."""
    with tool_call_context("Shell"):
        yield Shell(approval, environment, runtime)


@pytest.fixture
def task_list_tool(runtime: Runtime) -> Generator[TaskList]:
    with tool_call_context("TaskList"):
        yield TaskList(runtime)


@pytest.fixture
def task_output_tool(runtime: Runtime) -> TaskOutput:
    with tool_call_context("TaskOutput"):
        return TaskOutput(runtime)


@pytest.fixture
def task_handoff_tool(runtime: Runtime) -> Generator[TaskHandoff]:
    with tool_call_context("TaskHandoff"):
        yield TaskHandoff(runtime)


@pytest.fixture
def task_input_tool(runtime: Runtime, approval: Approval) -> Generator[TaskInput]:
    with tool_call_context("TaskInput"):
        yield TaskInput(runtime, approval)


@pytest.fixture
def task_stop_tool(runtime: Runtime, approval: Approval) -> Generator[TaskStop]:
    with tool_call_context("TaskStop"):
        yield TaskStop(runtime, approval)


@pytest.fixture
def read_file_tool(runtime: Runtime) -> ReadFile:
    """Create a ReadFile tool instance."""
    return ReadFile(runtime)


@pytest.fixture
def read_media_file_tool(runtime: Runtime) -> ReadMediaFile:
    """Create a ReadMediaFile tool instance."""
    return ReadMediaFile(runtime)


@pytest.fixture
def glob_tool(runtime: Runtime) -> Glob:
    """Create a Glob tool instance."""
    return Glob(runtime)


@pytest.fixture
def grep_tool(runtime: Runtime) -> Grep:
    """Create a Grep tool instance."""
    return Grep(runtime)


@pytest.fixture
def smart_search_tool(runtime: Runtime) -> SmartSearch:
    """Create a SmartSearch tool instance."""
    return SmartSearch(runtime)


@pytest.fixture
def write_file_tool(runtime: Runtime, approval: Approval) -> Generator[WriteFile]:
    """Create a WriteFile tool instance."""
    with tool_call_context("WriteFile"):
        yield WriteFile(runtime, approval)


@pytest.fixture
def str_replace_file_tool(runtime: Runtime, approval: Approval) -> Generator[StrReplaceFile]:
    """Create a StrReplaceFile tool instance."""
    with tool_call_context("StrReplaceFile"):
        yield StrReplaceFile(runtime, approval)


@pytest.fixture
def search_web_tool(config: Config, runtime: Runtime) -> SearchWeb:
    """Create a SearchWeb tool instance."""
    return SearchWeb(config, runtime)


@pytest.fixture
def fetch_url_tool(config: Config, runtime: Runtime) -> FetchURL:
    """Create a FetchURL tool instance."""
    return FetchURL(config, runtime)


# misc fixtures


@pytest.fixture
def outside_file() -> Generator[Path]:
    """Return a path to a file outside the working directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "outside_file.txt"
