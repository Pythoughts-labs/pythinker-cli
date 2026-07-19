from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest

from pythinker_code.soul import StatusSnapshot
from pythinker_code.ui.shell import prompt as shell_prompt
from pythinker_code.ui.shell.prompt import (
    PROMPT_SYMBOL,
    PROMPT_SYMBOL_AGENT_INPUT,
    AttachmentCache,
    BgTaskCounts,
    CachedAttachment,
    CustomPromptSession,
    CwdLostError,
    InputHighlightLexer,
    LocalFileMentionCompleter,
    LocalFileMentionMenuControl,
    PromptMode,
    PromptUIState,
    RunningPromptDelegate,
    SlashCommandAutoSuggest,
    SlashCommandCompleter,
    SlashCommandMenuControl,
    UserInput,
    sanitize_surrogates,
)
from tests.ui_and_conv.test_prompt_tips import _DummyReadOnlyModal, _DummyRunningPrompt

PUBLIC_PROMPT_CONTRACT = {
    "AttachmentCache": AttachmentCache,
    "BgTaskCounts": BgTaskCounts,
    "CachedAttachment": CachedAttachment,
    "CustomPromptSession": CustomPromptSession,
    "CwdLostError": CwdLostError,
    "Document": shell_prompt.Document,
    "HSplit": shell_prompt.HSplit,
    "InputHighlightLexer": InputHighlightLexer,
    "LocalFileMentionCompleter": LocalFileMentionCompleter,
    "LocalFileMentionMenuControl": LocalFileMentionMenuControl,
    "PROMPT_SYMBOL": PROMPT_SYMBOL,
    "PROMPT_SYMBOL_AGENT_INPUT": PROMPT_SYMBOL_AGENT_INPUT,
    "PromptMode": PromptMode,
    "PromptUIState": PromptUIState,
    "RunningPromptDelegate": RunningPromptDelegate,
    "SlashCommandAutoSuggest": SlashCommandAutoSuggest,
    "SlashCommandCompleter": SlashCommandCompleter,
    "SlashCommandMenuControl": SlashCommandMenuControl,
    "UserInput": UserInput,
    "Window": shell_prompt.Window,
    "Dimension": shell_prompt.Dimension,
    "sanitize_surrogates": sanitize_surrogates,
}


def test_prompt_module_preserves_repository_import_surface() -> None:
    """Names found by searching prompt imports and ``shell_prompt`` references stay exported."""
    for name, imported in PUBLIC_PROMPT_CONTRACT.items():
        assert getattr(shell_prompt, name) is imported


@pytest.fixture
def prompt_session() -> CustomPromptSession:
    return CustomPromptSession(
        status_provider=lambda: StatusSnapshot(context_usage=0.0),
        model_capabilities=set(),
        model_name=None,
        thinking=False,
        shell_mode_slash_commands=(),
        history_enabled=False,
    )


def test_custom_prompt_session_keyword_initialization_contract(
    prompt_session: CustomPromptSession,
) -> None:
    assert prompt_session._mode is PromptMode.AGENT
    assert prompt_session._session.default_buffer.completer is prompt_session._agent_mode_completer
    assert prompt_session._session.app.max_render_postpone_time == pytest.approx(1 / 30)


class _CountingRunningPrompt(_DummyRunningPrompt):
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.hide_card = False

    def render_agent_status(self, columns: int) -> str:
        self.calls["agent_status"] += 1
        return f"agent status ({columns})"

    def render_running_prompt_body(self, columns: int) -> str:
        self.calls["body"] += 1
        return f"live view ({columns})"

    def render_pinned_status_tail(self, columns: int) -> str:
        self.calls["pinned"] += 1
        return f"pinned ({columns})"

    def running_prompt_placeholder(self) -> str:
        self.calls["placeholder"] += 1
        return "placeholder"

    def running_prompt_hide_input_card(self) -> bool:
        self.calls["hide_card"] += 1
        return self.hide_card

    def running_prompt_hide_input_card_chrome(self) -> bool:
        self.calls["hide_chrome"] += 1
        return False


def _sample_render_calls(
    prompt_session: CustomPromptSession,
    monkeypatch: pytest.MonkeyPatch,
) -> Counter[str]:
    delegate = _CountingRunningPrompt()
    prompt_session._running_prompt_delegate = delegate
    monkeypatch.setattr(shell_prompt, "is_card_style", lambda: True)
    monkeypatch.setattr(
        shell_prompt,
        "get_app_or_none",
        lambda: SimpleNamespace(
            output=SimpleNamespace(get_size=lambda: SimpleNamespace(columns=80, rows=24))
        ),
    )

    # Sample both card-visible and card-hidden frames twice. Together they exercise
    # the placeholder and hide-chrome branches of the current rendering facade.
    for hide_card in (False, False, True, True):
        delegate.hide_card = hide_card
        prompt_session._render_agent_prompt_message()
    return delegate.calls


def test_running_prompt_delegate_render_methods_are_sampled(
    prompt_session: CustomPromptSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _sample_render_calls(prompt_session, monkeypatch)

    for method in (
        "agent_status",
        "body",
        "pinned",
        "placeholder",
        "hide_card",
        "hide_chrome",
    ):
        assert calls[method] >= 1


@pytest.mark.parametrize(
    "method",
    ("agent_status", "body", "pinned", "placeholder", "hide_card", "hide_chrome"),
)
@pytest.mark.xfail(strict=True, reason="exact-once sampling lands in Task 2")
def test_running_prompt_delegate_render_method_is_sampled_exactly_once(
    method: str,
    prompt_session: CustomPromptSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _sample_render_calls(prompt_session, monkeypatch)

    assert calls[method] == 1


@pytest.mark.xfail(strict=True, reason="height-overflow handling lands in Task 3")
def test_running_prompt_layers_do_not_overflow_terminal_height(
    prompt_session: CustomPromptSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TallRunningPrompt(_DummyRunningPrompt):
        def render_agent_status(self, columns: int) -> str:
            return "\n".join(f"agent {index}" for index in range(10))

    class _TallReadOnlyModal(_DummyReadOnlyModal):
        def render_running_prompt_body(self, columns: int) -> str:
            return "\n".join(f"modal {index}" for index in range(10))

    prompt_session._running_prompt_delegate = _TallRunningPrompt()
    prompt_session._modal_delegates = [_TallReadOnlyModal()]
    rows = 8
    monkeypatch.setattr(
        shell_prompt,
        "get_app_or_none",
        lambda: SimpleNamespace(
            output=SimpleNamespace(get_size=lambda: SimpleNamespace(columns=80, rows=rows))
        ),
    )

    rendered = prompt_session._render_agent_prompt_message()
    plain = "".join(fragment[1] for fragment in rendered)

    assert len(plain.splitlines()) <= rows
