---
name: add-git-provider
description: Add or change a git hosting provider for the git deploy target (pull/merge requests, branch checks, token user) under src/autoair/storage/git. Use for GitHub, GitLab, Bitbucket, Azure DevOps, Gitea or a new host.
---

# Add a git provider

Providers implement the `GitProvider` protocol in `src/autoair/storage/git/provider.py`
(`PullRequest`, `CommitInfo`, `check_branch`). Git operations themselves run through the git
CLI (`local_git.py`, `LocalGitProvider`); hosted providers add REST calls for pull requests.
There is no registry: selection is a `Literal` plus an if/elif.

## Steps

1. Implement `src/autoair/storage/git/<host>.py`. Subclass `LocalGitProvider` when the host
   only adds REST calls on top of the CLI (as `GiteaProvider` does).
2. HTTP through `autoair.net.tls` (`httpx_kwargs("git", ...)` / `async_client("git", ...)`)
   so the outbound CA, proxy and TLS settings apply. Bound timeouts and response sizes.
3. Register: add the name to `GitStorageSettings.provider` (`Literal[...]` in
   `src/autoair/storage/config.py`) and a lazy-import branch in `_git_provider` in the same
   file. Provider-specific settings get descriptions and defaults; run
   `uv run python scripts/gen_config_docs.py`.
4. Credentials: from an Airflow connection (`storage/git/config.py::credentials_from_connection`)
   or the per-request header token (`storage/request_token.py`). Never persist a token; never
   put it in a URL that can be logged; send it only to `https://` hosts. Wrap error paths so
   the token is masked (`secret_mask.masking`).
5. Tests: the provider takes a `transport=` argument; unit tests pass a `MockTransport` that
   fakes the host's REST API (see `tests/unit/test_storage_gitea.py`,
   `test_storage_bitbucket_azure.py`). `tests/git_http.py` is a real `git http-backend`
   server for push/clone paths. Live tests only behind `-m live`.
6. Docs: `website/docs/deployment/git-bundles.mdx` and `website/docs/user-guide/deploy.mdx`
   (provider list, token scopes), the token table in `docs/configuration.md`, CHANGELOG.

## Pitfalls

- 403 vs 401 handling feeds `AuthError`; keep messages actionable without echoing headers.
- Branch names come from users: validate with `check_branch` before passing them to git.

## Done when

- [ ] Provider selectable via `AUTOAIR_STORAGE__GIT__PROVIDER=<name>`
- [ ] Unit tests with a mock transport, including an auth failure that does not leak the token
- [ ] Config docs regenerated; deployment docs updated
