---
paths:
  - "src/autoair/settings.py"
  - "src/autoair/sections.py"
  - "src/autoair/config_sources.py"
  - "src/autoair/config_reference.py"
  - "src/autoair/**/config.py"
  - "src/autoair/feature_overrides.py"
  - "scripts/scan_hardcoded.py"
  - "scripts/hardcoded_decisions.json"
  - "scripts/gen_config_docs.py"
  - "scripts/gen_shared_constants.py"
---

# Settings, feature flags and the hard-coded audit

Procedures: skill `add-setting` (new or changed setting) and skill `hardcoded-audit-triage`
(scanner findings).

- Settings are pydantic-settings models (`env_prefix="AUTOAIR_"`, `env_nested_delimiter="__"`).
  Precedence (`config_sources.py`): init args > env > `.env` > secrets dir > config files >
  defaults.
- Every field: `Field(default=..., description="...")`. The description is the user
  documentation (what it does, the unit, the effect of edge values such as 0 or empty), and
  constraints go in the type (`gt=0`, `Literal[...]`). Secrets are `SecretStr | None`, and only
  with the owner's agreement; prefer an Airflow `*_conn_id`.
- A feature area's section model lives in its own module (`ai/config.py`, `kb/config.py`,
  `iam/config.py`, `storage/config.py`, `persistence/config.py`, `lsp/config.py`), registered as
  one field of `Settings`; generic sections are in `sections.py`.
- Feature flags (`FeatureFlags`) change at runtime: admins override them through
  `feature_overrides.py` unless `features_locked` (fnmatch) or an `AUTOAIR_FEATURES__*` env value
  locks them. Read a flag per request (`settings.feature(name)`, the `feature("<name>")` route
  dependency), never at import or start-up. A flag that needs a missing extra reports why in
  `Settings.feature_unavailable`.
- Read values where they are used, at request time when they can change; never copy them into
  module-level state. Fields named `*_pattern` are compiled as regexes by `_valid_regex`.
- After a change: `uv run python scripts/gen_config_docs.py` (rewrites
  `docs/configuration-reference.md`), then `--check`; `uv run python scripts/scan_hardcoded.py
  --check`; `uv run python scripts/gen_shared_constants.py --check` when the value is mirrored
  in `frontend/src/lib/generated/`.
- The scanner reports module constants, numeric defaults and kwargs, numbers >= 100, regexes,
  URLs and paths under `src/autoair`; `EXCLUDED` paths are audited by their owners. Decisions
  in `scripts/hardcoded_decisions.json` are `config` (names the setting) or `keep` (the reason
  names the protocol, invariant or risk). A security floor is never made configurable below its
  current default. `--write` produces `.artifacts/hardcoded-audit.md`, which is never committed.
- Changing a default is user-visible: CHANGELOG entry under `[Unreleased]`.
