---
name: add-setting
description: Add or change an AutoAir configuration setting (AUTOAIR_* environment variable, config-file key, feature flag) or replace a hard-coded value with one. Use when touching src/autoair/settings.py, sections.py or any */config.py section model, or when scan_hardcoded.py reports a value operators may need to change.
---

# Add a setting

Settings are Pydantic v2 models loaded by pydantic-settings (`src/autoair/settings.py`,
`env_prefix="AUTOAIR_"`, `env_nested_delimiter="__"`). Sources and precedence are in
`src/autoair/config_sources.py`: init args > env > `.env` > secrets dir > config files > defaults.

## Where the field goes

| Area | Model | File |
|---|---|---|
| Generic sections (catalog, policy, server, outbound, limits, ui, sso, ...) | `*Settings(_Section)` | `src/autoair/sections.py` |
| IAM | `IamSettings` | `src/autoair/iam/config.py` |
| AI (needs nothing from `autoair[ai]` to import) | `AiSettings`, e.g. `TroubleshootSettings` | `src/autoair/ai/config.py` |
| Knowledge base | `KbSettings` and its `Kb*Settings` sections | `src/autoair/kb/config.py` |
| Storage / git | `StorageSettings`, `GitStorageSettings` | `src/autoair/storage/config.py`, `src/autoair/storage/git/config.py` |
| Database | `DatabaseSettings` | `src/autoair/persistence/config.py` |
| LSP | `LspSettings` | `src/autoair/lsp/config.py` |
| Collaboration | `CollabSettings` | `src/autoair/collab.py` |
| Feature switch | `FeatureFlags` | `src/autoair/settings.py` |

**Feature flags are switched at runtime.** Admins override them in the admin panel
(`feature_overrides.py`, stored document; `api/routers/admin_features.py`), unless the flag is
locked by `features_locked` (fnmatch patterns, `*` = all) or set through the environment. So
read a flag per request (`settings.feature(name)`, the `feature("<name>")` route dependency),
never at import or start-up. A flag that needs an optional extra reports why it cannot run in
`Settings.feature_unavailable` (e.g. `ai` without `autoair[ai]`).

A new feature area gets its own section model in its own module, registered as one field
of `Settings`; its variables then read `AUTOAIR_<SECTION>__<FIELD>`.

## Steps

1. Add the field with `Field(default=..., description="...")`. The description is the
   user documentation: say what it does, the unit (s, bytes), and the effect of edge values
   (0, empty). Constrain it (`gt=0`, `Literal[...]`) instead of validating later.
2. Secrets are `SecretStr | None`. Prefer an Airflow connection id (`*_conn_id`) over any new
   secret field: AutoAir does not store git tokens or LLM keys. Ask the owner before adding a
   new secret field.
3. Fields named `*_pattern` are compiled by `_Section._valid_regex`; keep the suffix for regexes.
4. Read the value where it is used (`services.settings...` or `sections.active(...)`), at
   request time when it can change at runtime. Never copy it into module-level state.
5. If the literal was flagged by the hard-coded scanner, update its entry in
   `scripts/hardcoded_decisions.json` (or remove the stale one).
6. Regenerate and check:

   ```bash
   uv run python scripts/gen_config_docs.py            # rewrites docs/configuration-reference.md
   uv run python scripts/gen_config_docs.py --check
   uv run python scripts/scan_hardcoded.py --check
   uv run python scripts/gen_shared_constants.py --check   # if the value is mirrored in the UI
   uv run pytest -q tests/unit -k "config or settings or sections"
   ```

7. If a guide covers the area, update the website page that owns it (the area index is
   "Settings by area" in `docs/configuration.md`); a new feature flag also goes into the
   flag table of `docs/configuration.md`. The website copy of the reference
   (`website/docs/reference/configuration.md`) is synced at build time; never edit it.

## Pitfalls

- `docs/configuration-reference.md` is generated; a hand edit is overwritten and fails CI.
- Lists and maps are JSON in environment variables (`'["dev","prod"]'`); say so in the
  description when the type is a collection.
- `tests/conftest.py` resets process-wide sections (`sections.reset_all()`) around every
  test; tests that need a value pass `Settings(...)` explicitly instead of setting env vars.
- Changing a default is a user-visible change: add a CHANGELOG entry under `[Unreleased]`.

## Done when

- [ ] Field has a default and a description; secrets are `SecretStr`
- [ ] `gen_config_docs.py --check` and `scan_hardcoded.py --check` pass
- [ ] A test covers the new behaviour (default and one non-default value)
- [ ] CHANGELOG entry for user-visible changes
