"""Tests for the /feedback shell slash command."""

from __future__ import annotations

from collections.abc import Awaitable
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

from pythinker_core.message import Message, ToolCall

from pythinker_code.feedback import (
    FeedbackSubmission,
    build_feedback_issue_url,
    feedback_summary,
    parse_feedback_args,
    redact_text,
    submit_feedback_payload,
)
from pythinker_code.ui.shell import slash as shell_slash
from pythinker_code.ui.shell.slash import registry as shell_slash_registry
from pythinker_code.ui.shell.slash import shell_mode_registry
from pythinker_code.wire.types import TextPart, ThinkPart


async def _run_feedback(app: object, args: str) -> None:
    result = shell_slash.feedback(app, args)  # pyright: ignore[reportArgumentType]
    if result is not None:
        await cast(Awaitable[None], result)


class TestFeedbackRegistration:
    def test_registered_in_shell_registry(self) -> None:
        cmd = shell_slash_registry.find_command("feedback")
        assert cmd is not None
        assert cmd.name == "feedback"

    def test_registered_in_shell_mode_registry(self) -> None:
        cmd = shell_mode_registry.find_command("feedback")
        assert cmd is not None


class TestFeedbackFallback:
    async def test_opens_new_issue_url_when_no_soul(self, monkeypatch) -> None:
        open_mock = Mock(return_value=True)
        monkeypatch.setattr("pythinker_code.utils.term.open_url_in_browser", open_mock)
        monkeypatch.setattr(shell_slash.console, "print", Mock())

        await _run_feedback(Mock(), "bug broken thing")

        open_mock.assert_called_once()
        url = open_mock.call_args.args[0]
        assert "Pythoughts-labs/pythinker-cli" in url
        assert "new" in url

    async def test_prints_success_when_browser_opens(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "pythinker_code.utils.term.open_url_in_browser", Mock(return_value=True)
        )
        print_mock = Mock()
        monkeypatch.setattr(shell_slash.console, "print", print_mock)

        await _run_feedback(Mock(), "feature add thing")

        output = " ".join(str(c) for c in print_mock.call_args_list)
        assert "Opening" in output or "browser" in output.lower()

    async def test_prints_url_when_browser_fails(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "pythinker_code.utils.term.open_url_in_browser", Mock(return_value=False)
        )
        print_mock = Mock()
        monkeypatch.setattr(shell_slash.console, "print", print_mock)

        await _run_feedback(Mock(), "ux confusing prompt")

        output = " ".join(str(c) for c in print_mock.call_args_list)
        assert "Pythoughts-labs/pythinker-cli" in output

    async def test_invalid_args_without_soul_still_offer_github_fallback(self, monkeypatch) -> None:
        open_mock = Mock(return_value=True)
        monkeypatch.setattr("pythinker_code.utils.term.open_url_in_browser", open_mock)
        print_mock = Mock()
        monkeypatch.setattr(shell_slash.console, "print", print_mock)

        await _run_feedback(Mock(), "--unknown-option")

        open_mock.assert_called_once()
        output = " ".join(str(c) for c in print_mock.call_args_list)
        assert "Unknown /feedback option" in output
        assert "Opening GitHub feedback" in output


class TestFeedbackDestination:
    def test_builtin_endpoint_without_pythinker_auth_falls_back(self, monkeypatch) -> None:
        from pythinker_code.config import Config

        monkeypatch.delenv("PYTHINKER_FEEDBACK_URL", raising=False)
        soul = Mock()
        soul.runtime.config = Config()

        assert shell_slash._feedback_destination(soul) is None

    def test_configured_endpoint_is_used_without_pythinker_auth(self, monkeypatch) -> None:
        from pythinker_code.config import Config

        monkeypatch.delenv("PYTHINKER_FEEDBACK_URL", raising=False)
        soul = Mock()
        soul.runtime.config = Config()
        soul.runtime.config.feedback.endpoint_url = "https://feedback.example/submit"

        assert shell_slash._feedback_destination(soul) == ("https://feedback.example/submit", {})

    def test_builtin_endpoint_uses_pythinker_auth(self, monkeypatch) -> None:
        from pydantic import SecretStr

        from pythinker_code.auth import PYTHINKER_CODE_PLATFORM_ID
        from pythinker_code.auth.platforms import managed_provider_key
        from pythinker_code.config import Config, LLMProvider

        monkeypatch.delenv("PYTHINKER_FEEDBACK_URL", raising=False)
        soul = Mock()
        soul.runtime.config = Config()
        soul.runtime.config.providers[managed_provider_key(PYTHINKER_CODE_PLATFORM_ID)] = (
            LLMProvider(
                type="pythinker",
                base_url="https://api.pythinker.com/coding/v1",
                api_key=SecretStr("token"),
            )
        )

        feedback_url, headers = shell_slash._feedback_destination(soul) or ("", {})

        assert feedback_url.endswith("/feedback")
        assert headers["Authorization"] == "Bearer token"


class TestFeedbackSubmission:
    async def test_submits_structured_payload(self, tmp_path: Path, monkeypatch) -> None:
        from pythinker_code.soul.pythinkersoul import PythinkerSoul

        soul = Mock(spec=PythinkerSoul)
        soul.runtime.session.id = "sess-123"
        soul.runtime.session.title = "Feedback task"
        soul.runtime.session.work_dir = tmp_path
        soul.runtime.session.subagents_dir = tmp_path / "subagents"
        soul.runtime.session.subagents_dir.mkdir()
        soul.runtime.role = "root"
        soul.runtime.config.feedback.github_repo = "Pythoughts-labs/pythinker-cli"
        soul.name = "default"
        soul.context.history = [
            Message(role="user", content=[TextPart(text="please fix this")]),
            Message(
                role="assistant", content=[ThinkPart(think="hidden"), TextPart(text="I can help")]
            ),
            Message(
                role="assistant",
                content=[],
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolCall.FunctionBody(
                            name="Bash", arguments='{"command":"pytest"}'
                        ),
                    )
                ],
            ),
        ]

        app = Mock()
        app.soul = soul
        monkeypatch.setattr(shell_slash, "_feedback_destination", lambda _soul: ("https://fb", {}))
        submit_mock = AsyncMock(
            return_value=FeedbackSubmission(number=42, html_url="https://issue/42")
        )
        monkeypatch.setattr("pythinker_code.feedback.submit_feedback_payload", submit_mock)
        monkeypatch.setattr("pythinker_code.feedback.current_model_key", lambda _soul: "test/model")
        print_mock = Mock()
        monkeypatch.setattr(shell_slash.console, "print", print_mock)

        await _run_feedback(app, "bug --yes command failed")

        submit_mock.assert_awaited_once()
        submit_call = submit_mock.await_args
        assert submit_call is not None
        payload = submit_call.args[2]
        assert payload["type"] == "bug"
        assert payload["content"] == "command failed"
        assert payload["session_id"] == "sess-123"
        assert payload["privacy"]["redacted"] is True
        assert payload["privacy"]["included_diff"] is False
        assert payload["context"]["last_messages"][-1]["text"] == "I can help"
        assert "hidden" not in str(payload)
        assert payload["context"]["tool_calls"][-1]["name"] == "Bash"

    async def test_prints_follow_up_issue_url_when_endpoint_returns_no_link(
        self, monkeypatch
    ) -> None:
        from pythinker_code.soul.pythinkersoul import PythinkerSoul

        soul = Mock(spec=PythinkerSoul)
        soul.runtime.session.id = "sess-123"
        soul.runtime.config.feedback.github_repo = "Pythoughts-labs/pythinker-cli"
        app = Mock()
        app.soul = soul
        payload = {
            "type": "bug",
            "content": "command failed",
            "privacy": {},
            "context": {},
            "repo": {},
        }
        monkeypatch.setattr(
            "pythinker_code.feedback.build_feedback_payload", AsyncMock(return_value=payload)
        )
        monkeypatch.setattr(shell_slash, "_feedback_destination", lambda _soul: ("https://fb", {}))
        monkeypatch.setattr(
            "pythinker_code.feedback.submit_feedback_payload",
            AsyncMock(return_value=FeedbackSubmission(number=None, html_url=None)),
        )
        print_mock = Mock()
        monkeypatch.setattr(shell_slash.console, "print", print_mock)

        await _run_feedback(app, "bug --yes command failed")

        output = " ".join(str(c) for c in print_mock.call_args_list)
        assert "No report link was returned" in output
        assert "github.com/Pythoughts-labs/pythinker-cli" in output

    async def test_prompts_before_submitting_by_default(self, monkeypatch) -> None:
        from pythinker_code.soul.pythinkersoul import PythinkerSoul

        class RejectPromptSession:
            @classmethod
            def __class_getitem__(cls, _item: object) -> type[RejectPromptSession]:
                return cls

            async def prompt_async(self, *_args: object, **_kwargs: object) -> str:
                return "n"

        app = Mock()
        app.soul = Mock(spec=PythinkerSoul)
        monkeypatch.setattr(
            "pythinker_code.feedback.build_feedback_payload",
            AsyncMock(return_value={"privacy": {}, "context": {}, "repo": {}}),
        )
        submit_mock = AsyncMock()
        monkeypatch.setattr("pythinker_code.feedback.submit_feedback_payload", submit_mock)
        monkeypatch.setattr("prompt_toolkit.PromptSession", RejectPromptSession)
        print_mock = Mock()
        monkeypatch.setattr(shell_slash.console, "print", print_mock)

        await _run_feedback(app, "bug command failed")

        submit_mock.assert_not_awaited()
        output = " ".join(str(c) for c in print_mock.call_args_list)
        assert "Feedback cancelled" in output


class TestFeedbackHelpers:
    def test_parse_feedback_type_and_flags(self) -> None:
        parsed = parse_feedback_args("bug --include-diff --yes broken tests")
        assert not isinstance(parsed, str)
        assert parsed.kind == "bug"
        assert parsed.include_diff is True
        assert parsed.yes is True
        assert parsed.message == "broken tests"

    async def test_submit_feedback_payload_accepts_empty_success_body(self, monkeypatch) -> None:
        class FakeResponse:
            status = 200

            async def __aenter__(self) -> FakeResponse:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def json(self, *_args: object, **_kwargs: object) -> object:
                raise ValueError("empty body")

        class FakeSession:
            async def __aenter__(self) -> FakeSession:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
                return FakeResponse()

        monkeypatch.setattr("pythinker_code.feedback.new_client_session", FakeSession)

        submission = await submit_feedback_payload("https://feedback", {}, {"content": "hi"})

        assert submission.number is None
        assert submission.html_url is None

    def test_feedback_issue_url_migrates_legacy_default_repo(self) -> None:
        payload = {"type": "other", "content": "hi"}
        old_owner = "mohamed-elkholy95"

        url = build_feedback_issue_url(payload, old_owner + "/Pythinker-Code")

        assert "github.com/Pythoughts-labs/pythinker-cli/issues/new" in url
        assert old_owner not in url

    def test_feedback_issue_url_uses_compact_body_for_large_payload(self) -> None:
        payload = {
            "type": "bug",
            "content": "problem " * 600,
            "session_id": "sess-123",
            "client": {
                "version": "1.2.3",
                "os": "Linux",
                "python": "3.14",
                "model": "test/model",
            },
            "repo": {
                "branch": "feature/feedback",
                "head": "abc1234",
                "dirty": True,
                "diff": "+secret diff\n" * 10_000,
            },
            "context": {
                "last_messages": [{"role": "user", "text": "message " * 500}],
                "tool_calls": [{"name": "Bash", "hint": "pytest"}],
            },
            "privacy": {"redacted": True, "included_diff": True},
        }

        url = build_feedback_issue_url(payload)

        assert len(url) < 8_000
        assert "+secret diff" not in url
        assert "Patch+diff+omitted" in url

    def test_feedback_summary_shows_default_privacy_exclusions(self) -> None:
        summary = feedback_summary(
            {
                "privacy": {
                    "included_diff": False,
                    "included_transcript": False,
                    "included_tool_details": False,
                },
                "context": {},
                "repo": {},
            }
        )

        assert "✗ patch diff" in summary
        assert "✗ extended transcript" in summary
        assert "✗ detailed tool args/results" in summary
        assert "best-effort secret/path redaction" in summary

    def test_redacts_common_secrets_and_home_path(self) -> None:
        text = f"Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz123456 {Path.home()}/repo"

        redacted = redact_text(text)

        assert "ghp_" not in redacted
        assert str(Path.home()) not in redacted
        assert "<redacted" in redacted

    def test_redacts_url_credentials_and_package_manager_secrets(self) -> None:
        text = (
            "PIP_INDEX_URL=https://user:pass@example.test/simple "
            "HTTPS_PROXY=http://proxy-user:proxy-pass@proxy.test:8080 "
            "Authorization: Basic dXNlcjpwYXNzd29yZA=="
        )

        redacted = redact_text(text)

        assert "user:pass" not in redacted
        assert "proxy-user:proxy-pass" not in redacted
        assert "dXNlcjpwYXNzd29yZA" not in redacted
        assert redacted.count("<redacted") >= 3

    def test_redacts_common_bare_token_formats(self) -> None:
        text = " ".join(
            [
                "".join(["sk-", "ant", "-api03-", "abcdefghijklmnopqrstuvwxyz0123456789"]),
                "".join(["sk-", "proj", "-", "abcdefghijklmnopqrstuvwxyz0123456789"]),
                "".join(["xo", "xb-", "1234567890", "-", "abcdefghijklmnop"]),
                "".join(["AI", "za", "SyAbCdEfGhIjKlMnOpQrStUvWxYz012345678"]),
                ".".join(
                    [
                        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
                        "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
                        "signature1234567890",
                    ]
                ),
            ]
        )

        redacted = redact_text(text)

        assert "sk-ant" not in redacted
        assert "sk-proj" not in redacted
        assert "xoxb" not in redacted
        assert "AIza" not in redacted
        assert "eyJ" not in redacted
        assert redacted.count("<redacted") >= 5
