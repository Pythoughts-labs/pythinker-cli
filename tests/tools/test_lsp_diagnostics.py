"""Tests for passive LSP diagnostics registry, injection provider, and file hooks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from pythinker_host.path import HostPath

from pythinker_code.lsp.diagnostics import (
    SENT_FILE_LRU_CAP,
    DiagnosticEntry,
    DiagnosticFile,
    DiagnosticRegistry,
    ServerDiagnostics,
    register_publish_diagnostics_handler,
    render_diagnostics_block,
    uri_to_path,
)
from pythinker_code.lsp.protocol import Position, Range
from pythinker_code.soul.agent import Runtime
from pythinker_code.soul.approval import Approval
from pythinker_code.soul.dynamic_injection import (
    DynamicInjection,
    collect_within_budget,
    dynamic_to_candidate,
)
from pythinker_code.soul.dynamic_injections.lsp_diagnostics import LspDiagnosticsInjectionProvider
from pythinker_code.tools.file.replace import Edit, StrReplaceFile
from pythinker_code.tools.file.replace import Params as ReplaceParams
from pythinker_code.tools.file.write import Params as WriteParams
from pythinker_code.tools.file.write import WriteFile
from tests.conftest import tool_call_context


def _entry(
    message: str,
    severity: int,
    line: int = 0,
    *,
    source: str | None = None,
    code: str | int | None = None,
) -> DiagnosticEntry:
    return DiagnosticEntry(
        message=message,
        severity=severity,
        range=Range(start=Position(line=line, character=0), end=Position(line=line, character=1)),
        source=source,
        code=code,
    )


def _file(uri: str, diagnostics: list[DiagnosticEntry]) -> DiagnosticFile:
    return DiagnosticFile(uri=uri, path=uri_to_path(uri) or uri, diagnostics=diagnostics)


class TestDiagnosticRegistry:
    def test_dedup_within_batch(self) -> None:
        registry = DiagnosticRegistry()
        duplicate = _entry("same", 1)
        registry.register_pending(
            "pyright",
            [_file("file:///tmp/a.py", [duplicate, duplicate])],
        )
        assert registry.pending_count == 1

    def test_dedup_across_turns(self) -> None:
        registry = DiagnosticRegistry()
        file = _file("file:///tmp/a.py", [_entry("error one", 1)])
        registry.register_pending("pyright", [file])
        first = registry.check_for_diagnostics()
        assert len(first) == 1
        registry.register_pending("pyright", [file])
        assert registry.check_for_diagnostics() == []

    def test_volume_cap_per_file(self) -> None:
        registry = DiagnosticRegistry()
        diagnostics = [_entry(f"msg-{index}", 1, line=index) for index in range(15)]
        registry.register_pending("pyright", [_file("file:///tmp/a.py", diagnostics)])
        groups = registry.check_for_diagnostics()
        assert sum(len(file.diagnostics) for file in groups[0].files) == 10

    def test_volume_cap_total(self) -> None:
        registry = DiagnosticRegistry()
        for index in range(40):
            registry.register_pending(
                "pyright",
                [_file(f"file:///tmp/file{index}.py", [_entry("err", 1)])],
            )
        groups = registry.check_for_diagnostics()
        total = sum(len(file.diagnostics) for group in groups for file in group.files)
        assert total == 30

    def test_severity_sort_prefers_errors(self) -> None:
        registry = DiagnosticRegistry()
        registry.register_pending(
            "pyright",
            [
                _file(
                    "file:///tmp/a.py",
                    [
                        _entry("warning", 2),
                        _entry("error", 1),
                        _entry("hint", 4),
                    ],
                )
            ],
        )
        groups = registry.check_for_diagnostics()
        messages = [diag.message for diag in groups[0].files[0].diagnostics]
        assert messages == ["error", "warning", "hint"]

    def test_clear_for_file_and_clear_all(self) -> None:
        registry = DiagnosticRegistry()
        registry.register_pending("pyright", [_file("file:///tmp/a.py", [_entry("err", 1)])])
        assert registry.pending_count == 1
        registry.clear_for_file("file:///tmp/a.py")
        assert registry.pending_count == 0
        registry.register_pending("pyright", [_file("file:///tmp/b.py", [_entry("err", 1)])])
        registry.clear_all()
        assert registry.pending_count == 0

    def test_sent_file_lru_cap(self) -> None:
        registry = DiagnosticRegistry()
        for index in range(SENT_FILE_LRU_CAP + 5):
            uri = f"file:///tmp/file{index}.py"
            registry.register_pending("pyright", [_file(uri, [_entry("err", 1)])])
            registry.check_for_diagnostics()
        assert len(registry._sent_keys) == SENT_FILE_LRU_CAP

    def test_render_diagnostics_block(self) -> None:
        from pythinker_code.lsp.diagnostics import ServerDiagnostics

        groups = [
            ServerDiagnostics(
                server_name="pyright",
                files=[
                    _file(
                        "file:///tmp/a.py", [_entry("type error", 1, source="pyright", code="E001")]
                    )
                ],
            )
        ]
        text = render_diagnostics_block(groups)
        assert "LSP diagnostics" in text
        assert "/tmp/a.py" in text
        assert "Error (1:1)" in text
        assert "type error" in text


class TestPublishDiagnosticsHandler:
    async def test_handler_registers_and_resets_failures(self) -> None:
        registry = DiagnosticRegistry()
        instance = MagicMock()
        register_publish_diagnostics_handler(registry, "pyright", instance)
        handler = instance.on_notification.call_args[0][1]

        await handler(
            {
                "uri": "file:///tmp/a.py",
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 1},
                        },
                        "message": "bad",
                        "severity": 1,
                    }
                ],
            }
        )
        assert registry.pending_count == 1

        await handler({"uri": "not-a-valid-params"})
        assert registry.pending_count == 1


class TestLspDiagnosticsInjectionProvider:
    def _make_runtime(self, *, connected: bool) -> MagicMock:
        runtime = MagicMock()
        lsp = MagicMock()
        lsp.is_connected.return_value = connected
        lsp.diagnostics = DiagnosticRegistry()
        runtime.lsp = lsp
        return runtime

    async def test_returns_nothing_when_disconnected(self) -> None:
        provider = LspDiagnosticsInjectionProvider(self._make_runtime(connected=False))
        soul = MagicMock()
        assert await provider.get_injections([], soul) == []

    async def test_returns_block_when_connected(self) -> None:
        runtime = self._make_runtime(connected=True)
        runtime.lsp.diagnostics.register_pending(
            "pyright",
            [_file("file:///tmp/a.py", [_entry("oops", 1)])],
        )
        provider = LspDiagnosticsInjectionProvider(runtime)
        soul = MagicMock()
        result = await provider.get_injections([], soul)
        assert len(result) == 1
        assert result[0].type == "lsp_diagnostics"
        assert "oops" in result[0].content
        assert await provider.get_injections([], soul) == []

    async def test_rearm_allows_reinjection(self) -> None:
        runtime = self._make_runtime(connected=True)
        provider = LspDiagnosticsInjectionProvider(runtime)
        soul = MagicMock()
        runtime.lsp.diagnostics.register_pending(
            "pyright",
            [_file("file:///tmp/a.py", [_entry("first", 1)])],
        )
        await provider.get_injections([], soul)
        provider.rearm("lsp_diagnostics")
        runtime.lsp.diagnostics.register_pending(
            "pyright",
            [_file("file:///tmp/b.py", [_entry("second", 1)])],
        )
        result = await provider.get_injections([], soul)
        assert "second" in result[0].content

    def test_budget_truncates_large_block(self) -> None:
        lines = "\n".join(f"line {index}" for index in range(200))
        text = render_diagnostics_block(
            [
                ServerDiagnostics(
                    server_name="pyright",
                    files=[_file("file:///tmp/a.py", [_entry(lines, 1)])],
                )
            ]
        )
        candidate = dynamic_to_candidate(
            DynamicInjection(type="lsp_diagnostics", content=text),
            priority=100,
        )
        selected = collect_within_budget([candidate], budget_tokens=20)
        assert len(selected) == 1
        assert selected[0].content.endswith("…")


class TestFileToolLspHooks:
    async def test_write_file_calls_lsp_and_rearms(
        self, runtime, approval, temp_work_dir: HostPath
    ) -> None:
        lsp = MagicMock()
        lsp.change_file = AsyncMock()
        lsp.save_file = AsyncMock()
        lsp.diagnostics = MagicMock()
        rearmed: list[str] = []
        runtime.lsp = lsp
        runtime.rearm_injection = rearmed.append

        target = Path(temp_work_dir.unsafe_to_local_path()) / "hook.py"
        with tool_call_context("WriteFile"):
            tool = WriteFile(runtime, Approval(yolo=True))
            result = await tool(WriteParams(path=str(target), content="print('hi')\n"))

        assert not result.is_error
        lsp.diagnostics.clear_for_file.assert_called_once_with(target.resolve().as_uri())
        lsp.change_file.assert_awaited_once_with(str(target), "print('hi')\n")
        lsp.save_file.assert_awaited_once_with(str(target))
        assert rearmed == ["lsp_diagnostics"]

    async def test_str_replace_file_calls_lsp_and_rearms(
        self, runtime, approval, temp_work_dir: HostPath
    ) -> None:
        lsp = MagicMock()
        lsp.change_file = AsyncMock()
        lsp.save_file = AsyncMock()
        lsp.diagnostics = MagicMock()
        rearmed: list[str] = []
        runtime.lsp = lsp
        runtime.rearm_injection = rearmed.append

        target = Path(temp_work_dir.unsafe_to_local_path()) / "edit.py"
        target.write_text("old value\n", encoding="utf-8")

        with tool_call_context("StrReplaceFile"):
            tool = StrReplaceFile(runtime, Approval(yolo=True))
            result = await tool(ReplaceParams(path=str(target), edit=Edit(old="old", new="new")))

        assert not result.is_error
        lsp.diagnostics.clear_for_file.assert_called_once_with(target.resolve().as_uri())
        lsp.change_file.assert_awaited_once_with(str(target), "new value\n")
        lsp.save_file.assert_awaited_once_with(str(target))
        assert rearmed == ["lsp_diagnostics"]

    async def test_write_file_succeeds_when_lsp_notification_fails(
        self, runtime, approval, temp_work_dir: HostPath
    ) -> None:
        lsp = MagicMock()
        lsp.change_file = AsyncMock(side_effect=RuntimeError("LSP server crashed"))
        lsp.save_file = AsyncMock()
        lsp.diagnostics = MagicMock()
        rearmed: list[str] = []
        runtime.lsp = lsp
        runtime.rearm_injection = rearmed.append

        target = Path(temp_work_dir.unsafe_to_local_path()) / "hook.py"
        with tool_call_context("WriteFile"):
            tool = WriteFile(runtime, Approval(yolo=True))
            result = await tool(WriteParams(path=str(target), content="print('hi')\n"))

        assert not result.is_error
        assert target.read_text(encoding="utf-8") == "print('hi')\n"
        lsp.diagnostics.clear_for_file.assert_called_once_with(target.resolve().as_uri())
        assert rearmed == []

    async def test_str_replace_file_succeeds_when_lsp_notification_fails(
        self, runtime, approval, temp_work_dir: HostPath
    ) -> None:
        lsp = MagicMock()
        lsp.change_file = AsyncMock(side_effect=RuntimeError("LSP server crashed"))
        lsp.save_file = AsyncMock()
        lsp.diagnostics = MagicMock()
        rearmed: list[str] = []
        runtime.lsp = lsp
        runtime.rearm_injection = rearmed.append

        target = Path(temp_work_dir.unsafe_to_local_path()) / "edit.py"
        target.write_text("old value\n", encoding="utf-8")

        with tool_call_context("StrReplaceFile"):
            tool = StrReplaceFile(runtime, Approval(yolo=True))
            result = await tool(ReplaceParams(path=str(target), edit=Edit(old="old", new="new")))

        assert not result.is_error
        assert target.read_text(encoding="utf-8") == "new value\n"
        lsp.diagnostics.clear_for_file.assert_called_once_with(target.resolve().as_uri())
        assert rearmed == []

    async def test_write_file_skips_lsp_when_unwired(
        self, runtime, approval, temp_work_dir: HostPath
    ) -> None:
        runtime.lsp = None
        runtime.rearm_injection = None
        target = Path(temp_work_dir.unsafe_to_local_path()) / "plain.py"
        with tool_call_context("WriteFile"):
            tool = WriteFile(runtime, Approval(yolo=True))
            result = await tool(WriteParams(path=str(target), content="x"))
        assert not result.is_error


def test_lsp_provider_registered_in_subagent_soul(runtime: Runtime, tmp_path: Path) -> None:
    """LspDiagnosticsInjectionProvider must be wired into every PythinkerSoul, including subagents."""
    from pythinker_core.tooling.empty import EmptyToolset

    from pythinker_code.soul.agent import Agent
    from pythinker_code.soul.context import Context
    from pythinker_code.soul.pythinkersoul import PythinkerSoul

    sub_runtime = runtime.copy_for_subagent(agent_id="sa-1", subagent_type="coder")
    agent = Agent(name="test", system_prompt="", toolset=EmptyToolset(), runtime=sub_runtime)
    soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "h.jsonl"))

    assert any(isinstance(p, LspDiagnosticsInjectionProvider) for p in soul._injection_providers)
    assert sub_runtime.rearm_injection is not None
