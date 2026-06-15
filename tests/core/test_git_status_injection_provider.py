"""Git status dynamic injection for the root agent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from pythinker_code.soul.dynamic_injections.git_status import GitStatusInjectionProvider


def _make_soul(*, is_subagent: bool = False, git_status_injection: bool = True) -> MagicMock:
    soul = MagicMock()
    soul.is_subagent = is_subagent
    soul.runtime.work_dir = "/tmp/repo"
    soul.runtime.config.git_status_injection = git_status_injection
    return soul


class TestGitStatusInjectionProvider:
    async def test_injects_bounded_snapshot_on_first_step(self) -> None:
        provider = GitStatusInjectionProvider()
        soul = _make_soul()
        git_block = (
            "<git-context>\n"
            "Working directory: /tmp/repo\n"
            "Branch: main\n"
            "Dirty files (1):\n"
            "  M src/foo.py\n"
            "</git-context>"
        )
        with patch(
            "pythinker_code.soul.dynamic_injections.git_status.collect_git_context",
            new_callable=AsyncMock,
            return_value=git_block,
        ):
            result = await provider.get_injections([], soul)

        assert len(result) == 1
        assert result[0].type == "git_status"
        assert "may be stale" in result[0].content
        assert "Branch: main" in result[0].content
        assert "M src/foo.py" in result[0].content
        assert "<git-context>" not in result[0].content

    async def test_skips_when_not_a_git_repo(self) -> None:
        provider = GitStatusInjectionProvider()
        with patch(
            "pythinker_code.soul.dynamic_injections.git_status.collect_git_context",
            new_callable=AsyncMock,
            return_value="",
        ):
            assert await provider.get_injections([], _make_soul()) == []

    async def test_does_not_reinject_unchanged_snapshot(self) -> None:
        provider = GitStatusInjectionProvider()
        soul = _make_soul()
        git_block = "<git-context>\nBranch: main\n</git-context>"
        with patch(
            "pythinker_code.soul.dynamic_injections.git_status.collect_git_context",
            new_callable=AsyncMock,
            return_value=git_block,
        ):
            assert len(await provider.get_injections([], soul)) == 1
            assert await provider.get_injections([], soul) == []

    async def test_reinjects_after_compaction(self) -> None:
        provider = GitStatusInjectionProvider()
        soul = _make_soul()
        git_block = "<git-context>\nBranch: main\n</git-context>"
        with patch(
            "pythinker_code.soul.dynamic_injections.git_status.collect_git_context",
            new_callable=AsyncMock,
            return_value=git_block,
        ):
            await provider.get_injections([], soul)
            await provider.on_context_compacted()
            assert len(await provider.get_injections([], soul)) == 1

    async def test_subagents_are_excluded(self) -> None:
        provider = GitStatusInjectionProvider()
        with patch(
            "pythinker_code.soul.dynamic_injections.git_status.collect_git_context",
            new_callable=AsyncMock,
        ) as mock_collect:
            assert await provider.get_injections([], _make_soul(is_subagent=True)) == []
            mock_collect.assert_not_called()

    async def test_respects_config_disable(self) -> None:
        provider = GitStatusInjectionProvider()
        with patch(
            "pythinker_code.soul.dynamic_injections.git_status.collect_git_context",
            new_callable=AsyncMock,
        ) as mock_collect:
            assert await provider.get_injections([], _make_soul(git_status_injection=False)) == []
            mock_collect.assert_not_called()
