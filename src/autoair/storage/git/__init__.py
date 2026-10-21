"""Git-backed deploy targets (local git CLI, GitHub, GitLab, Gitea/Forgejo, …)."""

from __future__ import annotations

from autoair.storage.git.gitea import GiteaProvider
from autoair.storage.git.github import GitHubAppTokenSource, GitHubProvider
from autoair.storage.git.gitlab import GitLabProvider
from autoair.storage.git.local_git import LocalGitProvider
from autoair.storage.git.provider import CommitInfo, GitProvider, PullRequest
from autoair.storage.git.target import FileRenderer, GitMode, GitTarget

__all__ = [
    "CommitInfo",
    "FileRenderer",
    "GitHubAppTokenSource",
    "GitHubProvider",
    "GitLabProvider",
    "GitMode",
    "GitProvider",
    "GitTarget",
    "GiteaProvider",
    "LocalGitProvider",
    "PullRequest",
]
