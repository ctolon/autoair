# {{ cookiecutter.project_slug }}

{{ cookiecutter.description }}

An [AutoAir](https://github.com/) custom node kind: `{{ cookiecutter.node_name }}`
("{{ cookiecutter.title }}", palette category `{{ cookiecutter.category }}`).

## Layout

- `src/{{ cookiecutter.package_name }}/handler.py` — the handler: config model (its JSON Schema
  renders the inspector form), UI hints (icon, colour, example config, output schema),
  a validation hook and `build()`, which returns the task as a Python **AST** expression.
- `pyproject.toml` — registers the handler under the `autoair.node_kinds` entry point.
- `tests/` — codegen and discovery tests.

## Develop

```bash
pip install -e '.[test]'      # into the environment AutoAir runs in
pytest
```

Restart AutoAir: the node appears in the palette as "{{ cookiecutter.title }}".

## Rules

- Emit AST only (`ast.Constant`, `ctx.import_name(...)`, `ctx.task_kwargs(node)`), never
  source text: user-entered values then cannot inject code.
- `build()` runs in the AutoAir server; it must not do I/O or execute user input.
- The generated DAG imports whatever `build()` references: it must be installed on the
  Airflow workers / dag-processor too (a provider package or this package).

## Shipping operators too

If this package also contains Airflow operators (classes AutoAir lists in its operator
palette), describe them for AutoAir in an `autoair.yaml` next to them — palette group, icon,
parameter widgets, examples, docs links — or through the `autoair.operator_metadata` entry
point. It is read without importing the package; see AutoAir's `docs/extending.md`
("Operator packages"):

```yaml
# src/{{ cookiecutter.package_name }}/autoair.yaml
version: 1
group: "{{ cookiecutter.title }}"
operators:
  "*Operator":
    params: {}
```
