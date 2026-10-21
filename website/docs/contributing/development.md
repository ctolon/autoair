---
title: Development
description: Set up a development environment for AutoAir, run the checks and tests, follow the code style and submit a pull request.
---

The authoritative contribution workflow is [CONTRIBUTING.md](https://github.com/ctolon/autoair/blob/main/CONTRIBUTING.md) in the repository; when the two disagree, CONTRIBUTING.md wins. Participation is governed by the [code of conduct](https://github.com/ctolon/autoair/blob/main/CODE_OF_CONDUCT.md) (Contributor Covenant 2.1).

Bug reports and feature requests go to [GitHub issues](https://github.com/ctolon/autoair/issues/new/choose). Security problems do not: see [Security reporting](/docs/security/reporting).

## Licence and sign-off

AutoAir is licensed under Apache-2.0, and contributions are accepted under the same licence (Apache-2.0 section 5 applies to anything you submit). There is no CLA and no DCO, so `Signed-off-by` lines are not needed. Only submit work you have the right to contribute.

## Repository layout

| Path | Contents |
|---|---|
| `src/autoair/` | Python package: FastAPI app, Airflow plugin, CLI, spec, codegen, validation, storage, IAM, AI, MCP |
| `frontend/` | React 19 editor (Vite). `npm run build` writes both bundles into `src/autoair/static/` |
| `tests/` | `unit/`, `integration/` (real Airflow), `golden/` (codegen snapshots), `live/` (real services) |
| `docs/` | Reference documentation in Markdown |
| `website/` | This Docusaurus site, published to GitHub Pages |
| `deploy/` | Dockerfiles, the Compose demo, Helm values |
| `scripts/` | Dev servers, live-test bootstraps, doc generators, release checks |

The typed DAG specification is the interface between the UI, validation and code generation; tests mirror the backend domains.

## Setup

You need [uv](https://docs.astral.sh/uv/), Node.js 22.18 or later, git and, for the end-to-end tests, Google Chrome.

```bash
git clone https://github.com/ctolon/autoair && cd autoair
uv sync                                   # Python 3.12 venv with Airflow 3.3 and dev tools
(cd frontend && npm ci && npm run build)  # editor bundles served by the plugin and `autoair serve`
(cd website && npm ci)                    # only when working on this site
```

Run it:

```bash
make dev-airflow   # Airflow 3 standalone + plugin: http://localhost:8080 -> Browse -> DAG Builder
make serve         # standalone AutoAir: http://127.0.0.1:8765/ui/
make ui-dev        # Vite dev server with hot reload, proxying /api to `make serve`
uv run autoair local --open   # no Airflow server needed
```

The whole stack (PostgreSQL, split Airflow, two AutoAir replicas, and optional Gitea, Redis, AI service, TLS and tracing) runs in Docker through `make demo`; see [Docker Compose](/docs/installation/docker-compose).

## Checks and tests

```bash
make check    # ruff, mypy --strict, hard-coded audit, config reference, tsc, eslint, prettier
make test     # pytest (unit + integration against the installed Airflow) and vitest
make matrix   # nox: every supported Airflow minor (3.0 to 3.3), the 3.4 pre-release, Python 3.10
make e2e      # Playwright inside the Airflow UI; needs `make dev-airflow` running
make crawl    # UI crawler: every menu, dialog, tab and inspector must render (about 25 minutes)
```

Focused runs:

```bash
uv run pytest tests/unit/test_codegen.py -q -k branch
uv run nox -s "tests(3.0)"                 # one Airflow minor
uv run nox -s oldest                       # Python 3.10 + Airflow 3.0
cd frontend && npx vitest run src/lib      # one vitest folder
cd frontend && npm run test:visual         # visual regression (light and dark)
```

- **Golden tests** compare generated code with `tests/golden/expected/`. When a change is intended, regenerate the snapshots and review the diff like code.
- **PostgreSQL** tests (marker `postgres`) need `AUTOAIR_TEST_POSTGRES_URL`, for example a throw-away `postgres:17-alpine`.
- **Live suites** (`-m live`) run against real FAB and Keycloak auth managers and GitLab; the bootstrap scripts are in `scripts/` and described in [`docs/deployment.md`](https://github.com/ctolon/autoair/blob/main/docs/deployment.md#live-verification-against-real-services). Passing mock or contract tests is not proof that a live integration works.
- **UI changes** need a look in light and dark mode, and in both the Airflow plugin and standalone mode. Invalid or partially entered input must never break a panel. Browser tests live in `frontend/e2e`; the crawler exercises partially entered and invalid values. Verify with the crawler and end-to-end tests rather than fuzzing.

## Code style

- **Python:** `ruff` (lint and format, line length 100) and `mypy --strict` for `src/`. Public functions are typed.
- **No hard-coded operator values.** Values that operators may need to change become settings with a description (`src/autoair/settings.py`, `sections.py`). Every literal timeout, size or URL is triaged in `scripts/hardcoded_decisions.json`:

  ```bash
  uv run python scripts/scan_hardcoded.py           # untriaged findings
  uv run python scripts/scan_hardcoded.py --write   # full report in .artifacts/hardcoded-audit.md
  uv run python scripts/scan_hardcoded.py --check   # what CI runs: all triaged, none stale
  ```

  `scripts/hardcoded_decisions.json` is the committed source of truth; the markdown report is a local, gitignored file (CI uploads it as the `hardcoded-audit` artifact).

- **Settings reference:** after adding or changing a setting, run `uv run python scripts/gen_config_docs.py` to regenerate `docs/configuration-reference.md` (this site's [configuration reference](/docs/reference/configuration) is generated from it). CI fails when it is stale.
- **TypeScript:** eslint, prettier (`npm run format`) and `tsc --noEmit`. Never edit `frontend/src/api/gen/` by hand: after backend API changes run `make codegen` and commit `frontend/openapi.json` together with the regenerated client.
- Keep commits focused, with messages in the imperative mood ("Add GitLab draft MRs").

## Documentation and changelog

- User documentation lives on this site (`website/docs/`); the in-repository reference for contributors lives in `docs/`. Each topic is written in one place; see [Where documentation lives](/docs/contributing/documentation#where-documentation-lives) and, for how pages are written, [Documentation](/docs/contributing/documentation).
- `make docs` builds the site and `make docs-serve` starts it with live reload at `http://localhost:3000/autoair/`. The build fails on broken links.
- User-visible changes get an entry under `## [Unreleased]` in [CHANGELOG.md](https://github.com/ctolon/autoair/blob/main/CHANGELOG.md), in the Added, Changed, Deprecated, Removed, Fixed or Security section.

## Pull requests

1. Fork the repository, or create a branch if you have access. Never push to `main`.
2. Make the change with tests. `make check test` must pass locally.
3. Open the pull request using the template. Link the issue, describe user-visible effects, and attach light and dark screenshots for UI changes.
4. CI must be green. The **CI OK** check summarises lint, the Python by Airflow matrix, PostgreSQL, the frontend, end-to-end tests, the website, packaging and the Docker smoke test.
5. A review by a code owner (`.github/CODEOWNERS`) is required. Pull requests are squash-merged.

## Releases (maintainers)

1. Bump the version in `pyproject.toml`, `src/autoair/__init__.py`, `frontend/package.json` and `website/package.json`. `uv run python scripts/check_release.py` checks that they agree.
2. Move the `[Unreleased]` entries in `CHANGELOG.md` under `## [X.Y.Z] - YYYY-MM-DD` and update the comparison links.
3. Merge to `main`, then push a signed tag: `git tag -s vX.Y.Z -m "AutoAir X.Y.Z" && git push origin vX.Y.Z`.
4. `.github/workflows/release.yml` checks that the tag matches the package version and that `CHANGELOG.md` has a section for it, re-runs CI, builds the sdist and wheel (with the editor bundles), publishes to PyPI through trusted publishing, pushes the multi-arch image `ghcr.io/ctolon/autoair` with SBOM and build provenance, and creates the GitHub Release from the changelog section. Running the workflow manually (`workflow_dispatch`) builds and checks everything without publishing. The documentation site redeploys when the release is published.

Publishing credentials belong in GitHub environments or trusted publishing, never in the repository.
