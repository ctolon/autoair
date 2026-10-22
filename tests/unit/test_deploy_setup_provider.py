"""Provider hint for a git remote: hostname parsing, not substring matching."""

from __future__ import annotations

import pytest

from autoair.deploy_setup import suggested_provider


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/dags.git", "github"),
        ("https://GitHub.com./acme/dags.git", "github"),
        ("git@github.example.com:acme/dags.git", "github"),
        ("git@gitlab.example.com:acme/dags.git", "gitlab"),
        ("ssh://git@gitlab.com:2222/acme/dags.git", "gitlab"),
        ("https://user:token@bitbucket.org/acme/dags.git", "bitbucket"),
        ("git@ssh.dev.azure.com:v3/acme/proj/dags", "azure"),
        ("https://acme.visualstudio.com/proj/_git/dags", "azure"),
        ("https://codeberg.org/acme/dags.git", "gitea"),
        ("https://gitea.corp.example/acme/dags.git", "gitea"),
        ("https://forgejo.corp.example/acme/dags.git", "gitea"),
        ("file:///srv/dags.git", "local"),
        ("/srv/github/dags.git", "local"),
        (None, "local"),
        ("", "local"),
    ],
)
def test_known_hosts(url: str | None, expected: str) -> None:
    assert suggested_provider(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/bitbucket.org/acme/dags.git",
        "https://bitbucket.org.evil.example/acme/dags.git",
        "https://evilbitbucket.org/acme/dags.git",
        "https://dev.azure.com.evil.example/acme/dags",
        "https://evil.example/?next=visualstudio.com",
        "https://notcodeberg.org/acme/dags.git",
        "https://evil.example/github/dags.git",
        "https://notgithub.example/acme/dags.git",
        "git@evil.example:codeberg.org/dags.git",
        "https://[::1/dags.git",
    ],
)
def test_lookalike_hosts_are_plain_git(url: str) -> None:
    assert suggested_provider(url) == "local"
