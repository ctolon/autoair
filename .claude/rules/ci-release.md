---
paths:
  - ".github/**"
  - "pyproject.toml"
  - "noxfile.py"
  - "CHANGELOG.md"
  - "Makefile"
  - "scripts/check_release.py"
  - "src/autoair/__init__.py"
  - "deploy/**"
---

# CI, packaging and releases

Release preparation: `/release` (user-invoked skill); readiness check without publishing:
subagent `release-checker`. Only the user tags and pushes.

- `.github/workflows/ci.yml` (push to main, pull requests, merge queue, `workflow_call`): lint
  (ruff, format, mypy, hard-coded audit, config reference drift, version consistency); pytest
  matrix over Python 3.10-3.14 x Airflow 3.0.6-3.3.2; Airflow pre-release (non-blocking);
  PostgreSQL backend tests; frontend (codegen drift, typecheck, lint, prettier, vitest, build);
  visual regression (non-blocking); Playwright E2E against Airflow with the plugin (crawler
  excluded); website build with `llms.txt`; packaging (twine, `check_release.py --artifacts`,
  clean-venv install); Docker image smoke test and Compose config validation; live auth-manager
  tests (non-blocking); optional live GitLab. The single required check is **CI OK** (`ci-ok`).
- `release.yml` (tag `v*`): tag/version check, CI, build with checksums, PyPI trusted
  publishing, multi-arch GHCR image with SBOM and provenance, GitHub Release from the CHANGELOG
  section; `workflow_dispatch` is a dry run. `pages.yml` deploys the website from main and on
  release; `codeql.yml` is code scanning.
- Four version files must agree (`uv run python scripts/check_release.py`): `pyproject.toml`,
  `src/autoair/__init__.py`, `frontend/package.json`, `website/package.json`.
- `CHANGELOG.md` follows Keep a Changelog; user-visible changes go under `[Unreleased]`, and the
  release job needs a `## [X.Y.Z]` section.
- Nox sessions (`uv run nox --list`): `tests(3.0)` ... `tests(3.3)`, `prerelease(3.4.0b2)`,
  `oldest` (Python 3.10 + Airflow 3.0.6), `noai` (no `autoair[ai]`), `lint`. Never raise the
  Airflow floor (`apache-airflow>=3.0`) or drop a CI version without the owner.
- New dependencies are justified, pinned with a floor, and an optional extra when not core.
