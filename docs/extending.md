# Extending AutoAir

Reference for contributors and operators. Task-oriented guide on the documentation site: [Extending AutoAir](https://ctolon.github.io/autoair/docs/extending/overview).

## Custom node kinds
Implement `autoair.registry.NodeKindHandler`: a Pydantic `config_model` (its JSON Schema renders
the form) and `build(node, config, ctx) -> ast.expr`. It emits AST only, never source
text, so values cannot inject code. Register via entry point:
```toml
[project.entry-points."autoair.node_kinds"]
http_get = "my_pkg.autoair_ext:HttpGet"
```
Frontend customisation (icon, colour, custom inspector): `registerNodeKind()` in
`frontend/src/extensions/registry.ts`.

### Scaffolding a node kind package: `autoair new-node`

```bash
autoair new-node http_get --title "HTTP GET" --description "Call an HTTP endpoint" \
  --category integrations --icon builtin:http -o .
cd autoair-node-http-get
pip install -e '.[test]'      # into the environment AutoAir runs in
pytest                        # codegen compiles, no code injection, validation, discovery
```

It creates a package with the handler (`src/autoair_node_http_get/handler.py`: config model,
UI hints, a validation hook and an AST-emitting `build()`), a `pyproject.toml` that registers it
under the `autoair.node_kinds` entry point, a README and tests. Restart AutoAir after installing
it: the node appears in the palette under its category (`GET /api/catalog/node-kinds` lists it).
The generated DAG imports whatever `build()` references, so install the package (or the
operator's provider) on the Airflow workers and dag-processor too.

| Option | Default | Used for |
|---|---|---|
| `NAME` | — | handler `name` (`^[a-z][a-z0-9_]*$`), entry-point key, package `autoair_node_<name>`, class `<Name>Handler` |
| `--title`, `--description`, `--category` | `My node`, `A custom AutoAir node kind`, `custom` | palette metadata |
| `--icon` | `builtin:python` | palette/canvas icon |
| `-o/--output` | `.` | parent folder (the project folder must not exist) |

The template is cookiecutter-compatible and ships inside the package
(`autoair/scaffold/custom-node/`), so `cookiecutter "$(python -c 'import autoair.scaffold as s; print(s.TEMPLATES)')/custom-node"`
works too.

## Operator sources
Installed providers are discovered automatically (static AST analysis, no imports). Add in-house
operators with `AUTOAIR_EXTRA_OPERATOR_MODULES` or an `autoair.catalog_sources` entry point.

## Operator packages: groups, icons, parameter widgets

An operator package can make its operators look first-class in the palette with no AutoAir
configuration, and administrators can set or override the same things in the `catalog` /
`icons` settings. **Precedence: AutoAir configuration > package metadata > inference.**

### Package metadata (`autoair.yaml` or an entry point)

Example: a company package `bifrost` (installed with pip) ships `bifrost/autoair.yaml` as
package data. It applies to the package directory holding it and its sub-packages, and is read
statically (the package is never imported):

```yaml
# src/bifrost/autoair.yaml
version: 1
group: Bifrost                     # palette group of every bifrost operator
group_order: 10                    # lower first; groups no rule names use catalog.default_group_order
icon: logo                         # a name of `icons`, an .svg path, builtin:<logo> or a glyph name
docs_url: https://docs.bifrost.example/operators
icons:
  logo: icons/bifrost.svg          # relative to this file; sanitised (allowlist) before it is served
operators:                         # key: class path, class name or fnmatch pattern
  bifrost.operators.ingest.BifrostIngestOperator:
    description: Ingest a dataset into Bifrost.
    docs_url: https://docs.bifrost.example/ingest
    params:
      query:      {widget: sql, label: Query, help: The rows to load.}
      target_dsn: {widget: connection, conn_type: bifrost}
      mode:       {widget: enum, enum: [append, overwrite], default: append}
      api_token:  {widget: secret}
    examples:
      - title: Daily load
        params: {query: "select * from sales where day = '{{ ds }}'", mode: append}
  "*Purge*":
    icon: builtin:glyph-empty
```

`autoair.json` (same layout) works too. Instead of a file, the distribution can declare an entry
point whose value is a dict or a function returning one; it applies to the distribution's
modules:

```toml
[project.entry-points."autoair.operator_metadata"]
bifrost = "bifrost.autoair_meta:metadata"
```

With the default `catalog_strategy=static` the entry point is **not imported**: AutoAir reads a
literal `NAME = {...}` or a function whose body is `return {...}` from the source; computed values
need `catalog_strategy=import` (the entry point is then loaded) or the file. Package SVG icons
are served as `package:<name>` ids (`GET /api/icons/package:<name>`), stripped of scripts, event
handlers, links, `foreignObject` and external references; sizes are capped by
`catalog.package_icon_max_bytes` and `catalog.metadata_max_bytes`. Invalid metadata is reported in
the catalog diagnostics and ignored; it never breaks the catalog.

Install and enable it:

```bash
pip install bifrost==1.2.0                                  # also on the Airflow workers
export AUTOAIR_EXTRA_OPERATOR_DISTRIBUTIONS='["bifrost"]'   # scan the distribution
export AUTOAIR_OPERATOR_ALLOW='["airflow.providers.standard.*", "bifrost.*"]'   # optional
export AUTOAIR_OPERATOR_DENY='["bifrost.operators.ingest.BifrostPurgeOperator"]' # optional
# running server: Admin panel -> Catalog -> Refresh (POST /api/catalog/refresh), or restart
autoair catalog build -o /opt/autoair/catalog.json          # images with AUTOAIR_CATALOG_FILE
autoair catalog dump --diagnostics | jq .metadata           # which metadata was applied
```

### Configuration (`catalog.*`, `icons.rules_extra`)

```yaml
catalog:
  groups:                                        # first match wins; matched against the class
    - {match: '^bifrost\.', group: Bifrost, order: 10}  # path, module and distribution
  connection_param_patterns_extra:               # besides the built-in (^|_)conn(ection)?_id$
    - '_dsn$'
    - {pattern: '^warehouse$', conn_type: snowflake}
  param_overrides:                               # fnmatch over class paths (or a class name)
    'bifrost.*': {api_token: {widget: secret}}
    '*.BifrostIngestOperator': {query: {widget: sql, required: true}, mode: {hidden: true}}
icons:
  rules_extra:                                   # before the built-in icon inference
    - {match: '^bifrost', icon: custom:bifrost}  # class path or distribution; builtin:/custom:/glyph
```

Parameter override fields: `widget` (`sql`, `bash`, `python`, `json`, `yaml`, `markdown`,
`connection`, `variable`, `pool`, `enum`, `secret`, `text`), `enum`, `conn_type`, `label`, `help`,
`required`, `hidden`, `default`. They change the form and validation, not the generated code.
Regexes and icon ids are validated at load time (the error names the setting).
`catalog.connection_param_patterns` replaces the built-in pattern and is part of the catalog cache
key; the other presentation settings apply when the catalog is loaded. The full guide, with the
`bifrost` package layout, is [Operator metadata](https://ctolon.github.io/autoair/docs/extending/operator-metadata).

### Catalog diagnostics

The admin panel's **Catalog** tab and `GET /api/catalog/diagnostics` (permission
`catalog:refresh`) show the sources scanned with module/operator counts and the operators each
found, what was filtered out and why (`operator_allow` / `operator_deny`, the `discovery` skip and
exclude lists, `forbidden_operators`, `policy.privileged_operators`), modules that failed to parse
or import, the metadata files and entry points applied, and the last build time and duration,
with a refresh button. `autoair catalog dump --diagnostics` prints the same report.

## Templates & building blocks
Built-in starter DAGs and fragments live in `autoair/templates/builtin.py`; users save their own
DAG templates and reusable fragments (macro nodes) from the UI (stored at `AUTOAIR_TEMPLATES_URL`).
Teams can share them through a signed **marketplace** index; see
[Marketplace](https://ctolon.github.io/autoair/docs/extending/marketplace) and `examples/marketplace/`. Extension packages
(node kinds, catalog sources) can be listed there too; they are installed with pip by an admin.

## Policy
Organisation rules (`require_owner`, `require_description`, `min_retries`, `required_tags`,
`forbidden_operators`, `max_tasks`, `dag_id_prefix`, `operator_allow` / `operator_deny` and the
`policy.*` patterns) are enforced on validation and deploy; see [Policy](https://ctolon.github.io/autoair/docs/admin/policy) and the [`policy` reference](configuration-reference.md#policy). Declarative
`policy.rules` and an optional Open Policy Agent are described in [policy.md](policy.md).
