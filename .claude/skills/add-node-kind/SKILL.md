---
name: add-node-kind
description: Add a custom node kind (a canvas node backed by a NodeKindHandler that emits AST), either as an external package scaffolded with `autoair new-node` or as a built-in. Use for "new node type", "custom node", "node kind", autoair.node_kinds entry points or frontend registerNodeKind.
---

# Add a node kind

Contract: `autoair.registry.NodeKindHandler` (`src/autoair/registry/`). A handler has a
Pydantic `config_model` (its JSON Schema renders the form) and `build(node, config, ctx)`
returning an `ast.expr`. It emits AST only, never source text, so user values cannot inject
code. Handlers are discovered from the entry-point group `autoair.node_kinds`
(`default_registry()`). Reference: `docs/extending.md`.

## External package (preferred)

```bash
uv run autoair new-node http_get --title "HTTP GET" --description "Call an HTTP endpoint" \
  --category integrations --icon builtin:http -o /path/outside/the/repo
cd /path/outside/the/repo/autoair-node-http-get
uv pip install -e '.[test]' && pytest
```

`NAME` must match `^[a-z][a-z0-9_]*$`; the output folder must not exist. The template is
`src/autoair/scaffold/custom-node/` (cookiecutter-compatible, rendered in a Jinja sandbox).
Do not scaffold into the repository root: it would add an untracked package next to `src/`.

## Built-in or template changes

1. Handler: typed `config_model` with field descriptions; `build()` composes `ast` nodes,
   references imports with `ctx.import_name(module, name)` and common task kwargs with
   `ctx.task_kwargs(node)`. Validation hook for cross-field rules.
2. Spec: `CustomNode` in `src/autoair/spec/models.py` carries the config; a new spec field
   needs a migration in `spec/migrations.py` (bump `SPEC_VERSION`, add to `MIGRATIONS`).
3. Frontend (only to change looks): `registerNodeKind(...)` in
   `frontend/src/extensions/registry.ts` (`icon`, `color`, `describe`, `Inspector`). Without it
   the node renders with the generic form.
4. Changing the scaffold template: `uv run pytest -q tests/unit/test_new_node.py`. The
   template is excluded from ruff and mypy; its generated tests are the check.
5. Golden output: if codegen changes, regenerate and review
   (`AUTOAIR_UPDATE_GOLDEN=1 uv run pytest tests/integration/test_airflow_dags.py -k golden_source`).

## Done when

- [ ] `build()` returns AST only; a test proves a hostile config value stays a literal
- [ ] Generated DAG passes `DagBag` (integration test or `autoair validate`)
- [ ] `docs/extending.md` updated if the contract changed
