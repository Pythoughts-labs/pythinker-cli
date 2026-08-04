from __future__ import annotations

DEFAULT_FEEDBACK_GITHUB_REPO = "Pythoughts-labs/pythinker-cli"
_LEGACY_FEEDBACK_GITHUB_OWNER = "mohamed-elkholy95"

_LEGACY_DEFAULT_FEEDBACK_GITHUB_REPOS = {
    _LEGACY_FEEDBACK_GITHUB_OWNER + "/Pythinker-Code",
    _LEGACY_FEEDBACK_GITHUB_OWNER + "/pythinker-code",
    "TechMatrix-labs/pythinker-code",
    "Pythoughts-labs/pythinker-code",
}
_LEGACY_DEFAULT_FEEDBACK_GITHUB_REPOS_LOWER = {
    repo.lower() for repo in _LEGACY_DEFAULT_FEEDBACK_GITHUB_REPOS
}


def normalize_feedback_github_repo(repo: str) -> str:
    """Return the active feedback repo, migrating stale bundled defaults."""
    cleaned = repo.strip().strip("/")
    if not cleaned:
        return DEFAULT_FEEDBACK_GITHUB_REPO
    if cleaned.lower() in _LEGACY_DEFAULT_FEEDBACK_GITHUB_REPOS_LOWER:
        return DEFAULT_FEEDBACK_GITHUB_REPO
    return cleaned
