---
name: airflow-compat-check
description: Check that a change works on every supported Airflow (>=3.0,<4; CI covers 3.0, 3.1, 3.2, 3.3 and the 3.4 pre-release) by feature-detecting newer APIs instead of assuming them. Use when code imports from airflow, calls Airflow's REST API, generates version-dependent DAG code, or uses a feature introduced after 3.0.
---

# Airflow compatibility check

AutoAir supports Apache Airflow `>=3.0,<4` on Python 3.10-3.14. The dev venv runs Airflow
3.3; CI tests 3.0.6, 3.1.8, 3.2.2, 3.3.2 and 3.4.0b1 (`noxfile.py`, `.github/workflows/ci.yml`).
Code that works on the dev venv can still break 3.0.

## Rules

1. **Detect, do not assume.** Branch on what exists, not on what the newest version has:
   - installed Airflow: `autoair._compat.airflow_version()` (tuple or `None`),
     `supports_react_apps()` (3.1+), `dagbag_class()`, `base_operator_classes()`;
   - where a symbol lives per version: `_compat.sdk_symbol_module(name, version)` and
     `_SYMBOL_LOCATIONS` (e.g. timetables from `airflow.timetables.*` on 3.0/3.1, `airflow.sdk`
     on 3.2+); add new moved symbols there;
   - REST endpoints added later: call them and treat 404/405 as "not available" (pattern in
     `gateway/__init__.py`); expose the result through `/api/airflow/capabilities`
     (`api/routers/airflow.py`, `AirflowCapabilities`) so the UI can hide the feature;
   - spec features with a minimum version: `codegen/features.py` (`Feature.min_version`,
     `required_features(spec)`); validation reports them against the target version.
2. **Import lazily and defensively.** `airflow.*` imports inside functions, wrapped in
   `try/except ImportError` (or `AttributeError` for moved attributes) with a fallback.
   mypy skips `airflow.*` (`follow_imports = "skip"`), so the type checker will not catch a
   symbol missing on 3.0.
3. **Generated code follows the target version**, not the installed one: pass
   `airflow_version=` to `codegen.render` and test both sides of the version boundary.
4. **Never raise the floor** (`apache-airflow>=3.0`) or drop a CI version without the owner.
5. `_compat.LATEST_KNOWN` is the newest version this release knows; bump it (and the
   hard-coded audit entry) only with explicit support for that version.

## Commands

```bash
uv run pytest -q tests/unit/test_v2_validation.py tests/integration/test_v2_airflow.py
uv run nox -s "tests(3.0)"              # oldest minor
uv run nox -s oldest                    # Python 3.10 + Airflow 3.0.6
uv run nox -s "tests(3.2)"              # first version with airflow.sdk timetables
uv run nox -s prerelease                # 3.4 pre-release (allowed to fail in CI)
uv run nox                              # full matrix (slow: one venv per version)
```

Nox sessions install the extras from `extras_for()` in `noxfile.py` (`all,mcp,...`; no
`mcp` on 3.1, whose Starlette pin FastMCP 4 cannot meet) plus the pinned Airflow into `.nox/` and use a temporary
`AIRFLOW_HOME`; they do not touch the shared demo.

## Review questions

- Does any new `from airflow... import` run at module import time?
- Is a 3.2/3.3/3.4 API used without a capability check or fallback?
- Does generated code for a 3.0 target import only symbols 3.0 has?
- Is there a test on each side of the boundary (golden snapshot or unit test)?

## Done when

- [ ] Feature detection or version gate in place, with a fallback or a clear problem message
- [ ] `nox -s "tests(3.0)"` (or `oldest`) passes for the affected tests
- [ ] Capabilities endpoint / `required_features` updated when a feature is version-bound
