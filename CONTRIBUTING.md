# Contributing to AutoAir

Thanks for helping. This guide covers the development setup, tests, code style, docs and the
pull request process. Bug reports and feature requests go to
[issues](https://github.com/ctolon/autoair/issues/new/choose). Security problems do **not**:
see [SECURITY.md](SECURITY.md).

By participating you agree to the [code of conduct](CODE_OF_CONDUCT.md).

## Licence and sign-off

AutoAir is licensed under [Apache-2.0](LICENSE). Contributions are accepted under the same
licence: Apache-2.0 §5 applies to anything you submit. There is **no CLA and no DCO**, so you
do not need `Signed-off-by` lines. Only submit work you have the right to contribute.

## Repository layout

| Path | What |
| --- | --- |
| `src/autoair/` | Python package: FastAPI app, Airflow plugin, CLI, spec, codegen, validation, storage, IAM, AI, MCP |
| `frontend/` | React 19 editor (Vite). `npm run build` writes both bundles into `src/autoair/static/` |
| `tests/` | `unit/`, `integration/` (real Airflow), `golden/` (codegen snapshots), `live/` (real services) |
| `docs/` | Reference documentation (Markdown) |
| `website/` | Docusaurus site published to GitHub Pages |
| `deploy/` | Dockerfiles, Compose demo, Helm values |
| `scripts/` | Dev servers, live-test bootstraps, doc generators, release checks |

## Development setup

You need [uv](https://docs.astral.sh/uv/), Node.js 22.18 or later, git and, for the
E2E tests, Google Chrome.

```bash
git clone https://github.com/ctolon/autoair && cd autoair
uv sync                                   # Python 3.12 venv with Airflow 3.3 + dev tools
(cd frontend && npm ci && npm run build)  # editor bundles served by the plugin and `autoair serve`
```

Run it:

```bash
make dev-airflow   # Airflow 3 standalone + plugin: http://localhost:8080 → Browse → DAG Builder
make serve         # standalone AutoAir: http://127.0.0.1:8765/ui/
make ui-dev        # Vite dev server with hot reload, proxying /api to `make serve`
uv run autoair local --open   # no Airflow server needed
```

The whole stack (PostgreSQL, split Airflow, two replicas, optional Gitea, Redis, AI, TLS and
tracing) runs in Docker: see [deploy/compose/README.md](deploy/compose/README.md).

## Tests

```bash
make check    # ruff, mypy --strict, hard-coded audit, config reference, tsc, eslint, prettier
make test     # pytest (unit + integration against the installed Airflow) and vitest
make matrix   # nox: every supported Airflow minor (3.0 … 3.3), the 3.4 pre-release, Python 3.10
make e2e      # Playwright inside the Airflow UI; needs `make dev-airflow` running
make crawl    # UI crawler: every menu, dialog, tab and inspector must render (~25 min)
```

Single sessions and focused runs:

```bash
uv run pytest tests/unit/test_codegen.py -q -k branch
uv run nox -s "tests(3.0)"                 # one Airflow minor
uv run nox -s oldest                       # Python 3.10 + Airflow 3.0
cd frontend && npx vitest run src/lib      # one vitest folder
cd frontend && npm run test:visual         # visual regression (light and dark)
```

- **Golden tests:** generated code is compared with `tests/golden/expected/`. When a change is
  intended, regenerate the snapshots and review the diff like code.
- **PostgreSQL:** tests marked `postgres` need `AUTOAIR_TEST_POSTGRES_URL`, for example a
  throw-away `postgres:17-alpine`.
- **Live suites** (`-m live`) run against real FAB/Keycloak auth managers and GitLab. See
  `docs/deployment.md` for the bootstrap scripts.
- UI changes need a look in both themes and in both modes (plugin and standalone). Invalid
  input must never break a panel.

## Code style

- **Python:** `ruff` (lint and format, line length 100) and `mypy --strict` for `src/`.
  Public functions are typed. Do not hard-code values that operators may need to change: add
  a setting with a description (`src/autoair/settings.py` / `sections.py`). Every literal
  timeout, size or URL is triaged in `scripts/hardcoded_decisions.json`:

  ```bash
  uv run python scripts/scan_hardcoded.py           # untriaged findings
  uv run python scripts/scan_hardcoded.py --write   # full report in .artifacts/hardcoded-audit.md
  uv run python scripts/scan_hardcoded.py --check   # what CI runs: all triaged, none stale
  ```

  The decisions file is the committed source of truth. The markdown report is a local,
  gitignored artifact (CI also uploads it as the `hardcoded-audit` artifact).

- **TypeScript:** eslint, prettier (`npm run format`) and `tsc --noEmit`. Do not edit the API
  client in `frontend/src/api/gen/` by hand. After backend API changes, run `make codegen`
  and commit `frontend/openapi.json` together with the regenerated client.
- **Settings and docs:** after adding or changing a setting, run
  `uv run python scripts/gen_config_docs.py` to regenerate
  `docs/configuration-reference.md`. CI fails when it is stale.
- **Shared constants:** `frontend/src/lib/generated/` is written from Python by
  `uv run python scripts/gen_shared_constants.py` (for example the Jinja logical-date
  variables in `autoair.validation.jinja`); never edit it by hand. CI fails when it is stale.
- Keep commits focused, with messages in the imperative mood ("Add GitLab draft MRs").

## Documentation

- `docs/` holds the in-repo reference read on GitHub: the generated configuration reference,
  architecture, the IAM and SSO security model, policy rules, MCP and AI internals. The
  website in `website/docs/` holds the user documentation: installation, user guide,
  administration, AI and extending. Write a topic in one place and link to it from the other;
  the build copies `docs/configuration-reference.md` into the site.
- The contributor guide for the site is `website/docs/contributing/documentation.mdx`.
- `make docs` builds the site and `make docs-serve` starts it with live reload. The build
  fails on broken links.
- User-visible changes get an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md),
  in the Added / Changed / Deprecated / Removed / Fixed / Security sections.

## Agent-assisted contributions

Contributions made with Claude Code or another coding agent follow [CLAUDE.md](CLAUDE.md) and
`.claude/`: area rules in `.claude/rules/`, procedures as skills in `.claude/skills/`, reviewer
subagents in `.claude/agents/`, and hooks that block edits to generated files and destructive
commands. The same checks, tests and review apply as for any other pull request.

## Pull requests

1. Fork the repository, or create a branch if you have access. Never push to `main`.
2. Make the change with tests. `make check test` must pass locally.
3. Open the pull request using the template. Link the issue, describe user-visible effects,
   and attach screenshots (light and dark) for UI changes.
4. CI must be green. The **CI OK** check summarises lint, the Python × Airflow matrix,
   PostgreSQL, the frontend, E2E, the website, packaging and the Docker smoke test.
5. A review by a code owner (see `.github/CODEOWNERS`) is required. Pull requests are
   squash-merged.

## Releases (maintainers)

1. Bump the version in `pyproject.toml`, `src/autoair/__init__.py`, `frontend/package.json`
   and `website/package.json`. `uv run python scripts/check_release.py` checks that they
   agree.
2. Move the `[Unreleased]` entries in `CHANGELOG.md` under `## [X.Y.Z] - YYYY-MM-DD` and update
   the comparison links.
3. Merge to `main`, then tag: `git tag -s vX.Y.Z -m "AutoAir X.Y.Z" && git push origin vX.Y.Z`.
4. `.github/workflows/release.yml` re-runs CI (skipped when the tag message contains
   `[fast-release]`; see the release section of the development guide), publishes to PyPI (trusted publishing),
   pushes the multi-arch image `ghcr.io/ctolon/autoair` with SBOM and provenance, and creates
   the GitHub Release from the changelog section. The documentation site redeploys when the
   release is published.
