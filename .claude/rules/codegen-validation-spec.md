---
paths:
  - "src/autoair/spec/**"
  - "src/autoair/codegen/**"
  - "src/autoair/loader/**"
  - "src/autoair/validation/**"
  - "src/autoair/catalog/**"
  - "src/autoair/importer/**"
  - "src/autoair/registry/**"
  - "src/autoair/scaffold/**"
  - "src/autoair/runner/**"
  - "src/autoair/_compat.py"
  - "src/autoair/policy_rules.py"
  - "tests/golden/**"
---

# Spec, codegen, catalog and validation

Related skills: `airflow-compat-check`, `add-node-kind`, `add-operator-widget`.

- `DagSpec` (`spec/models.py`) is the single source of truth. A new spec field needs a
  migration in `spec/migrations.py` (bump `SPEC_VERSION`, append to `MIGRATIONS`).
- Raw code DAGs: `DagSpec.raw_code` set means a hand-written file with no nodes; it needs the
  `code:raw` permission and is checked by `validation/raw.py` (`validate_raw`).
- Generated code is built from AST nodes; user values reach it only as AST literals, never as
  source text. YAML goes through a serializer. Node kind handlers return `ast.expr` only.
- Generated code follows the *target* Airflow version (`render(..., airflow_version=)`), not the
  installed one. Version-bound spec features declare `Feature.min_version`
  (`codegen/features.py`, `required_features`); symbols that moved between versions go in
  `_compat._SYMBOL_LOCATIONS` (`sdk_symbol_module`). `airflow.*` imports run inside functions
  with an `ImportError`/`AttributeError` fallback.
- Catalog: the default `static` strategy parses provider sources (AST) instead of importing
  them; results are cached on disk and images ship a precomputed catalog (`autoair catalog
  build`, `AUTOAIR_CATALOG_FILE`). Never import operator modules on a request path.
- DagBag import checks use the warm pool (`validation/pool.py`, `DagBagPool`); never spawn an
  interpreter per request. The one deliberate exception is the isolated validator service
  (`validation/service.py`, `validator.max_tasks_per_child=1`, a pre-warmed spare per check).
  Task and DAG test runs (`runner/`) use a child process with a scrubbed environment.
- Jinja is evaluated in `jinja2.sandbox.SandboxedEnvironment` (`validation/jinja.py`,
  `scaffold/`). Constants of `validation/jinja.py` are mirrored into the frontend: run
  `uv run python scripts/gen_shared_constants.py` after changing them.
- OPA: `policy.opa_fail_mode` defaults to `open` (warning when OPA is unreachable); `closed`
  makes it an error. Never turn a fail-closed path into fail-open.
- Golden snapshots: after an intended codegen change regenerate them (command in CLAUDE.md,
  "Generated files") and review `git diff tests/golden/expected` like code.
