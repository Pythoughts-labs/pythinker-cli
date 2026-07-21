from __future__ import annotations

import json
import stat
from types import SimpleNamespace
from typing import Any, cast

from PIL import Image

from pythinker_code.ui.shell import prompt as shell_prompt
from pythinker_code.ui.shell import slash as shell_slash
from pythinker_code.ui.shell.placeholders import AttachmentCache, PromptPlaceholderManager
from pythinker_code.ui.shell.prompting.history import PromptHistoryError, PromptHistoryStore
from pythinker_code.ui.shell.slash import registry, slash_command_arg_suggestions


def _make_prompt_session(
    tmp_path, manager: PromptPlaceholderManager
) -> shell_prompt.CustomPromptSession:
    prompt_session = object.__new__(shell_prompt.CustomPromptSession)
    prompt_session._history_file = tmp_path / "history.jsonl"
    prompt_session._last_history_content = None
    prompt_session._history_enabled = True
    prompt_session._placeholder_manager = manager
    prompt_session._attachment_cache = manager.attachment_cache
    return prompt_session


def _read_history_lines(path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_append_history_entry_expands_text_placeholders_but_preserves_images(tmp_path) -> None:
    manager = PromptPlaceholderManager(attachment_cache=AttachmentCache(root=tmp_path / "cache"))
    pasted_text = "\n".join([f"line{i}" for i in range(1, 16)])
    text_token = manager.maybe_placeholderize_pasted_text(pasted_text)
    image = Image.new("RGB", (4, 4), color=(10, 20, 30))
    image_token = manager.create_image_placeholder(image)

    assert image_token == "[Image #1]"

    prompt_session = _make_prompt_session(tmp_path, manager)
    prompt_session._append_history_entry(f"before {text_token} {image_token} after")

    # Display token `[Image #N]` is rewritten to canonical `[image:<id>,WxH]` so history
    # remains resolvable across sessions via the attachment cache.
    lines = _read_history_lines(prompt_session._history_file)
    assert len(lines) == 1
    content = lines[0]["content"]
    assert content.startswith(f"before {pasted_text} [image:")
    assert content.endswith(",4x4] after")


def test_append_history_entry_deduplicates_consecutive_tokens_with_same_expanded_text(
    tmp_path,
) -> None:
    manager = PromptPlaceholderManager()
    prompt_session = _make_prompt_session(tmp_path, manager)
    token_one = manager.maybe_placeholderize_pasted_text("alpha\nbeta\ngamma")
    token_two = manager.maybe_placeholderize_pasted_text("alpha\nbeta\ngamma")

    prompt_session._append_history_entry(token_one)
    prompt_session._append_history_entry(token_two)

    assert _read_history_lines(prompt_session._history_file) == [{"content": "alpha\nbeta\ngamma"}]


def test_append_history_entry_writes_sanitized_surrogate_text(tmp_path) -> None:
    manager = PromptPlaceholderManager()
    prompt_session = _make_prompt_session(tmp_path, manager)
    token = manager.maybe_placeholderize_pasted_text("A" * 1000 + "\ud83d")

    prompt_session._append_history_entry(token)

    lines = _read_history_lines(prompt_session._history_file)
    assert len(lines) == 1
    assert "\ud83d" not in lines[0]["content"]
    assert "\ufffd" in lines[0]["content"]
    assert lines[0]["content"].startswith("A" * 1000)


def test_append_history_entry_redacts_common_secret_patterns(tmp_path) -> None:
    manager = PromptPlaceholderManager()
    prompt_session = _make_prompt_session(tmp_path, manager)

    prompt_session._append_history_entry(
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz "
        "api_key=sk-abcdefghijklmnop "
        '{"api_key": "quotedsecretvalue123"} '
        'token="quotedtokenvalue123"'
    )

    content = _read_history_lines(prompt_session._history_file)[0]["content"]
    assert "abcdefghijklmnopqrstuvwxyz" not in content
    assert "sk-abcdefghijklmnop" not in content
    assert "quotedsecretvalue123" not in content
    assert "quotedtokenvalue123" not in content
    assert content.count("[REDACTED]") == 4


def test_append_history_entry_redacts_oauth_and_vendor_tokens(tmp_path) -> None:
    manager = PromptPlaceholderManager()
    prompt_session = _make_prompt_session(tmp_path, manager)

    prompt_session._append_history_entry(
        "Authorization: Basic dXNlcjpwYXNzd29yZA== "
        "https://example.test/cb?access_token=access-token-secret&refresh_token=refresh-secret "
        "id_token=header.payload.signature "
        "ghp_abcdefghijklmnopqrstuvwxyz123456 "
        "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz1234567890 "
        "AIzaSyabcdefghijklmnopqrstuvwxyz1234567"
    )

    content = _read_history_lines(prompt_session._history_file)[0]["content"]
    for secret in (
        "dXNlcjpwYXNzd29yZA==",
        "access-token-secret",
        "refresh-secret",
        "header.payload.signature",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz1234567890",
        "AIzaSyabcdefghijklmnopqrstuvwxyz1234567",
    ):
        assert secret not in content
    assert content.count("[REDACTED]") == 7


def test_append_history_entry_can_be_disabled(tmp_path) -> None:
    manager = PromptPlaceholderManager()
    prompt_session = _make_prompt_session(tmp_path, manager)
    prompt_session._history_enabled = False

    prompt_session._append_history_entry("do not persist")

    assert not prompt_session._history_file.exists()


def test_append_history_entry_restricts_file_permissions(tmp_path) -> None:
    manager = PromptPlaceholderManager()
    prompt_session = _make_prompt_session(tmp_path, manager)

    prompt_session._append_history_entry("hello")

    mode = stat.S_IMODE(prompt_session._history_file.stat().st_mode)
    assert mode == 0o600


def test_prompt_history_store_loads_only_configured_tail(tmp_path) -> None:
    store = PromptHistoryStore(tmp_path / "history.jsonl", max_entries=2)

    appended_one = store.append("one")
    appended_two = store.append("two")
    appended_three = store.append("three")
    assert appended_one
    assert appended_two
    assert appended_three

    assert [entry.content for entry in store.load()] == ["two", "three"]


def test_prompt_history_store_excludes_credential_commands(tmp_path) -> None:
    store = PromptHistoryStore(tmp_path / "history.jsonl")

    login_appended = store.append("/login api-key")
    logout_appended = store.append("logout openai")
    assert login_appended is False
    assert logout_appended is False
    assert not store.path.exists()


def test_prompt_history_store_skips_oversized_records(tmp_path) -> None:
    store = PromptHistoryStore(tmp_path / "history.jsonl")

    oversized_appended = store.append("x" * (256 * 1024))
    assert oversized_appended is False
    assert not store.path.exists()


def test_prompt_history_store_clear_confirms_both_files_are_removed(tmp_path) -> None:
    store = PromptHistoryStore(tmp_path / "history.jsonl")
    kept_appended = store.append("kept")
    assert kept_appended
    encoding = "utf-8"
    store.rotated_path.write_text('{"content":"older"}\n', encoding=encoding)

    status = store.clear()

    assert status.entries == 0
    assert not store.path.exists()
    assert not store.rotated_path.exists()


def test_prompt_history_slash_command_has_only_supported_argument_suggestions() -> None:
    command = registry.find_command("prompt-history")

    assert command is not None
    assert command.name == "prompt-history"
    assert slash_command_arg_suggestions()["prompt-history"] == ("status", "clear")


def test_prompt_history_clear_reports_success_only_after_confirmed_removal(
    tmp_path,
    monkeypatch,
) -> None:
    store = PromptHistoryStore(tmp_path / "history.jsonl")
    kept_appended = store.append("kept")
    assert kept_appended
    prompt_session = object.__new__(shell_prompt.CustomPromptSession)
    cast(Any, prompt_session)._history_store = store
    app = SimpleNamespace(_prompt_session=prompt_session)
    messages: list[str] = []
    monkeypatch.setattr(shell_slash.console, "print", lambda message: messages.append(str(message)))
    command = registry.find_command("prompt-history")
    assert command is not None

    command.func(cast(Any, app), "clear")

    assert any("Prompt history cleared" in message for message in messages)
    assert not store.path.exists()


def test_prompt_history_clear_failure_never_claims_success(monkeypatch) -> None:
    class _FailingStore:
        def clear(self) -> None:
            raise PromptHistoryError("lock timed out")

    prompt_session = object.__new__(shell_prompt.CustomPromptSession)
    cast(Any, prompt_session)._history_store = _FailingStore()
    app = SimpleNamespace(_prompt_session=prompt_session)
    messages: list[str] = []
    monkeypatch.setattr(shell_slash.console, "print", lambda message: messages.append(str(message)))
    command = registry.find_command("prompt-history")
    assert command is not None

    command.func(cast(Any, app), "clear")

    assert any("Failed to clear prompt history" in message for message in messages)
    assert all("Prompt history cleared" not in message for message in messages)
