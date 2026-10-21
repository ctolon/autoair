---
title: Operator packages and metadata
description: Make an operator package look first-class in AutoAir's palette - palette groups, icons, parameter widgets, examples and docs links - from the package itself or from AutoAir's configuration.
sidebar_label: Operator metadata
---

AutoAir derives an operator's palette entry and form from its class: parameters, types, defaults, docstrings, `template_fields`, connection pickers for `*_conn_id` parameters. That is enough to *use* an in-house operator, but the result looks generic: the operators are listed under their raw package name, with an inferred icon and plain text inputs.

An operator package can describe its operators to AutoAir, and an administrator can do the same, or override it, in AutoAir's configuration. This page uses a fictional package, **`bifrost`**, that a company installs with pip.

## Precedence

For every field the first source that sets it wins:

1. **AutoAir configuration**: `catalog.groups`, `catalog.param_overrides`, `icons.rules_extra`, icon assignments made in the UI.
2. **Package metadata**: an `autoair.yaml` / `autoair.json` file in the package, or an `autoair.operator_metadata` entry point.
3. **Inference**: what AutoAir derives from the class (provider group, logo or concept glyph, widgets from types and parameter names).

## The bifrost package

```text
bifrost/
├── pyproject.toml
└── src/bifrost/
    ├── __init__.py
    ├── autoair.yaml          # metadata for AutoAir (read without importing bifrost)
    ├── icons/bifrost.svg
    └── operators/
        ├── __init__.py
        └── ingest.py         # BifrostIngestOperator, BifrostPurgeOperator
```

```python
# src/bifrost/operators/ingest.py
from collections.abc import Sequence

from airflow.sdk import BaseOperator


class BifrostIngestOperator(BaseOperator):
    """Load a dataset into Bifrost.

    :param query: Query selecting the rows.
    :param target_dsn: Bifrost endpoint (a connection id).
    :param mode: Write mode.
    :param api_token: API token.
    """

    template_fields: Sequence[str] = ("query",)

    def __init__(self, *, query: str, target_dsn: str = "bifrost_default",
                 mode: str = "append", api_token: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.query, self.target_dsn, self.mode, self.api_token = query, target_dsn, mode, api_token

    def execute(self, context):
        ...
```

Ship the metadata file and the icon as package data. With hatchling everything under the package folder is included; with setuptools add `[tool.setuptools.package-data] bifrost = ["autoair.yaml", "icons/*.svg"]`.

```toml
# pyproject.toml
[project]
name = "bifrost"
version = "1.2.0"
dependencies = ["apache-airflow>=3.0"]

[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"
```

### `autoair.yaml`

```yaml
# src/bifrost/autoair.yaml - applies to bifrost and every sub-package
version: 1
group: Bifrost                 # palette group of every bifrost operator
group_order: 10                # groups sort by this key (lower first), then by title
icon: logo                     # default icon: a name of `icons`, an SVG path or builtin:<logo>
docs_url: https://docs.bifrost.example/operators
icons:
  logo: icons/bifrost.svg      # relative to this file; sanitised before it is served
operators:
  # Keys: a class path, a class name or an fnmatch pattern; every matching key applies,
  # longer (more specific) keys last.
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

| Key | Level | Meaning |
|---|---|---|
| `group`, `group_order` | package, operator | Palette group title and its sort key |
| `icon` | package, operator | `builtin:<logo>`, a glyph name (`agent`, `clock`, `terminal`…), a name of `icons`, an `.svg` path relative to the file, or inline `<svg>` markup |
| `icons` | package | Named SVG icons (paths or inline markup) |
| `docs_url` | package, operator | Documentation link shown in the inspector (http/https) |
| `description` | operator | Palette summary (replaces the docstring's first paragraph) |
| `params` | operator | Parameter overrides, see [Parameter widgets](#parameter-widgets) |
| `examples` | operator | `{title, description, params}` sets the inspector can apply in one click |

A file applies to the package directory that contains it and to its sub-packages; a file deeper in the tree wins over one closer to the root. Unknown keys and invalid values are reported in the [catalog diagnostics](#diagnostics) and the file is ignored; they never break the catalog.

### SVG icons

Package SVGs are re-serialised through an allowlist before AutoAir serves them (`GET /api/icons/package:<name>`): only static shapes, gradients, clip paths, masks and text survive; scripts, event handlers, `foreignObject`, links, external `url(...)` / `href` references and DTDs are removed or refused. The size is capped by `catalog.package_icon_max_bytes` (64 KB) and the whole metadata file by `catalog.metadata_max_bytes` (512 KB). Icons are served with a sandboxing `Content-Security-Policy`.

### Entry point instead of a file

A package can declare the same document through the `autoair.operator_metadata` entry point group. The value is a dict or a function returning one:

```toml
[project.entry-points."autoair.operator_metadata"]
bifrost = "bifrost.autoair_meta:metadata"
```

```python
# src/bifrost/autoair_meta.py
def metadata():
    return {
        "group": "Bifrost",
        "operators": {"BifrostIngestOperator": {"params": {"query": {"widget": "sql"}}}},
    }
```

It applies to the modules of the distribution that declares it. With the default static catalog strategy AutoAir **does not import** the module: it reads `NAME = {...}` or a function whose body is a single `return {...}` with literal values from the source. Anything computed needs `catalog_strategy=import` (the entry point is then loaded normally) or an `autoair.yaml` file. Relative icon paths of an entry point are resolved against the folder of its module. When a package ships both, the file wins for the keys both set.

## Install and enable

```bash
# 1. Install the package where AutoAir runs (and on the Airflow workers / dag-processor:
#    the generated DAGs import bifrost).
pip install bifrost==1.2.0

# 2. Tell AutoAir to scan the distribution. bifrost requires apache-airflow, so the
#    provider scan finds it anyway; naming it also covers packages without that requirement.
export AUTOAIR_EXTRA_OPERATOR_DISTRIBUTIONS='["bifrost"]'

# 3. Optional: restrict what the palette shows (fnmatch patterns over class paths).
export AUTOAIR_OPERATOR_ALLOW='["airflow.providers.standard.*", "bifrost.*"]'
export AUTOAIR_OPERATOR_DENY='["bifrost.operators.ingest.BifrostPurgeOperator"]'
```

Then refresh the catalog of the running server: **Admin panel → Catalog → Refresh** (or `POST /api/catalog/refresh`, permission `catalog:refresh`), or restart AutoAir. Images that precompute the catalog rebuild it after `pip install`:

```dockerfile
RUN pip install bifrost==1.2.0 \
 && AUTOAIR_EXTRA_OPERATOR_DISTRIBUTIONS='["bifrost"]' autoair catalog build -o /opt/autoair/catalog.json
ENV AUTOAIR_CATALOG_FILE=/opt/autoair/catalog.json \
    AUTOAIR_EXTRA_OPERATOR_DISTRIBUTIONS='["bifrost"]'
```

Package metadata is read at catalog build time and stored in the catalog (and its cache); configuration overrides are applied when the catalog is loaded, so changing them needs a restart but no rebuild.

## Configuring the same from AutoAir

Administrators can set or override everything above without touching the package, for example for a third-party package that ships no metadata:

```yaml
# autoair.yaml configuration file (AUTOAIR_CONFIG_FILE) or AUTOAIR_CATALOG__* / AUTOAIR_ICONS__*
catalog:
  groups:                                   # first match wins
    - {match: '^bifrost\.', group: Bifrost, order: 10}
    - {match: '^acme_', group: ACME, order: 20}
  default_group_order: 100                  # provider groups no rule names
  connection_param_patterns_extra:          # besides the built-in *_conn_id naming
    - '_dsn$'
    - {pattern: '^warehouse$', conn_type: snowflake}
  param_overrides:
    'bifrost.*':                            # fnmatch over the class path (or a class name)
      api_token: {widget: secret}
    '*.BifrostIngestOperator':
      query: {widget: sql, required: true}
      mode: {hidden: true}
icons:
  rules_extra:                              # evaluated before the built-in icon inference
    - {match: '^bifrost\.|^bifrost$', icon: custom:bifrost}   # class path or distribution
    - {match: 'Agent', icon: agent}
```

- `catalog.groups[].match` is searched in the class path, the module and the distribution/provider name.
- `catalog.connection_param_patterns` replaces the built-in list (`(^|_)conn(ection)?_id$`); `connection_param_patterns_extra` extends it. An entry with a `conn_type` makes the picker offer connections of that type. These patterns are part of the catalog cache key: changing them rebuilds the catalog.
- `icons.rules_extra[].icon` is a bundled id (`builtin:<logo>`), a glyph name or an uploaded icon (`custom:<name>`). Regexes and icon ids are validated when the configuration is loaded; an invalid value stops startup with the setting's name in the message. An uploaded icon that is deleted later is skipped with a warning.

## Parameter widgets

`params` entries (in package metadata) and `catalog.param_overrides` values share one schema:

| Field | Effect |
|---|---|
| `widget` | `sql`, `bash`, `python`, `json`, `yaml`, `markdown` (code editors), `connection`, `variable`, `pool` (Airflow pickers), `enum`, `secret` (masked input), `text` (plain input) |
| `enum` | Allowed values; implies the `enum` widget |
| `conn_type` | Connection type(s) the connection picker offers |
| `label`, `help` | Field label and help text |
| `required` | Make the parameter required (validated on save and deploy) or optional |
| `hidden` | Hide the field from the form; a value already in the spec is kept |
| `default` | Default shown as placeholder and used to prefill a required parameter |

Overrides change the form and the validation, never the generated code: the DAG still passes the values to the operator's constructor.

## Diagnostics

**Admin panel → Catalog** (permission `catalog:refresh`) and `GET /api/catalog/diagnostics` show what the last build did:

- the sources and what they scanned (providers, folders, packages, distributions, modules), with module and operator counts and the operators found per source;
- what was left out and why: `operator_allow` / `operator_deny`, `discovery.skip_class_pattern`, `discovery.skip_module_pattern`, `discovery.exclude_distributions`, and operators refused by policy (`forbidden_operators`, `policy.privileged_operators`);
- modules that failed to parse, import or be analysed, with the error;
- the metadata files and entry points applied, with the number of operators and icons each provided, or the error that made AutoAir ignore it;
- the build time and duration, whether the catalog came from the cache, and a **Refresh** button.

The same report on the command line, without a server:

```bash
AUTOAIR_EXTRA_OPERATOR_DISTRIBUTIONS='["bifrost"]' autoair catalog dump --diagnostics \
  | jq '{metadata, filtered: [.filtered[] | select(.name | startswith("bifrost"))]}'
```

## Troubleshooting

| Symptom | Check |
|---|---|
| Metadata not applied | Is the file inside the package (`bifrost/autoair.yaml`) and installed as package data? The diagnostics list every file and entry point read, with its error |
| Entry point reported as "cannot be read statically" | Return a literal dict, ship `autoair.yaml`, or set `catalog_strategy=import` |
| Icon missing | Invalid or oversized SVG (see the metadata entry's error); a `custom:` icon of `icons.rules_extra` that no longer exists |
| Group not shown | A `catalog.groups` rule matches first, or the operator is hidden by `operator_deny` (see *Filtered*) |

## Related pages

- [Custom operators](/docs/extending/custom-operators)
- [Catalog sources](/docs/extending/catalog-sources)
- [`catalog` settings](/docs/reference/configuration#catalog), [`icons` settings](/docs/reference/configuration#icons)
