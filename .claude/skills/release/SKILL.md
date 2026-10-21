---
name: release
description: Prepare an AutoAir release - version bump in four files, CHANGELOG section, checks, and the tag that triggers .github/workflows/release.yml. Use only when the user asks to prepare or cut a release; tagging and pushing are the user's call.
disable-model-invocation: true
argument-hint: "[version, e.g. 1.2.0]"
---

# Cut a release

Target version: $ARGUMENTS (if empty, ask the user before changing any file).

Publishing is irreversible (PyPI, GHCR image, GitHub Release). Prepare everything locally,
then hand the tag and push commands to the user. Do not run `git tag`, `git push`, `uv
publish` or `gh release` unless the user explicitly says so in this conversation.

## 1. Version (four files must agree)

- `pyproject.toml` `[project] version`
- `src/autoair/__init__.py` `__version__`
- `frontend/package.json` `version`
- `website/package.json` `version`

```bash
uv run python scripts/check_release.py                       # versions agree
RELEASE_TAG=vX.Y.Z uv run python scripts/check_release.py    # tag matches
```

Pre-releases use PEP 440 suffixes (`a`, `b`, `rc`, `dev`); the workflow marks them as
pre-releases and does not move `latest`.

## 2. CHANGELOG

Move `## [Unreleased]` entries under `## [X.Y.Z] - YYYY-MM-DD` (Keep a Changelog sections:
Added, Changed, Deprecated, Removed, Fixed, Security), leave an empty `[Unreleased]`, update
the comparison links at the bottom. The release job fails without `## [X.Y.Z]`; the GitHub
Release notes are that section verbatim.

## 3. Checks

```bash
make check
uv run pytest -q && (cd frontend && npm test)
(cd frontend && npm ci && npm run build) && uv build && uvx twine check --strict dist/*
uv run python scripts/check_release.py --artifacts           # wheel ships bundles, py.typed, licence
(cd website && npm run build)
```

`dist/` must contain exactly one wheel and one sdist for `--artifacts`; clear old builds.
`npm run build` rewrites the bundles the shared demo serves (expected at release time).

## 4. Hand over to the user

```bash
git tag -s vX.Y.Z -m "AutoAir X.Y.Z" && git push origin vX.Y.Z
```

`release.yml` then: verifies tag and versions, reruns CI (`ci.yml` via `workflow_call`),
builds sdist + wheel with checksums, publishes to PyPI (trusted publishing, environment
`pypi`), pushes the multi-arch image `ghcr.io/<owner>/autoair` (base Airflow pinned by
`AIRFLOW_VERSION` in the workflow) with SBOM and provenance, and creates the GitHub Release.
`pages.yml` redeploys the docs on release. `workflow_dispatch` runs everything without
publishing (dry run).

## Done when

- [ ] Four versions agree; CHANGELOG section present
- [ ] All checks above pass
- [ ] Tag command handed to the user, not executed
