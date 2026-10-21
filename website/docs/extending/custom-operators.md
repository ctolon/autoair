---
title: Custom operators
description: Make in-house and third-party Airflow operators appear in AutoAir's palette through static discovery of folders, packages, distributions, modules and files.
sidebar_label: Custom operators
---

AutoAir's operator palette is the **operator catalog**: every operator, sensor, transfer and notifier class AutoAir can find in the installed environment. The catalog is also the **import allowlist** of code generation: a DAG can only use classes that are in it. To use your own operators, make AutoAir discover them.

No extension code is needed. Operators are found by settings, and their forms are derived from the class definitions.

## How discovery works

Discovery is **static**. AutoAir reads Python files and parses them with `ast`; it does not import provider or custom code in the server process. For each class it resolves base classes across modules (following `import` aliases, `TYPE_CHECKING` blocks and lazy-import tables), linearises the class hierarchy and builds:

- the parameters of `__init__` merged across the hierarchy (inherited parameters included), with defaults and a JSON Schema from type annotations,
- parameter documentation parsed from docstrings, and a one-line summary,
- `template_fields` (which parameters accept Jinja),
- connection types (for connection pickers: parameters matching `catalog.connection_param_patterns`, by default the `*_conn_id` / `*_connection_id` naming), `ui_color`, sensor and deferrable flags, deprecation markers.

Palette groups, icons, parameter widgets, examples and documentation links can be declared by the package itself (`autoair.yaml`) or configured (`catalog.*`, `icons.rules_extra`); see [Operator metadata](/docs/extending/operator-metadata).

Classes the analyser cannot resolve statically are introspected by importing them **in a subprocess** without AutoAir's secrets, bounded by `catalog_fallback_timeout` (300 s per build). `AUTOAIR_CATALOG_STRATEGY=import` switches to importing and inspecting in-process; keep the default `static` unless you are debugging discovery.

A cheap pre-filter keeps scanning fast: a file is parsed only when it defines a class and its text matches `discovery.class_hint_pattern` (`Operator|Sensor|Notifier`).

## Sources

| Source | Setting | What is scanned |
|---|---|---|
| Installed providers | `scan_provider_packages` (`true`) | Every module of every distribution with an `apache_airflow_provider` entry point or depending on Airflow, not only the modules listed in `provider.yaml`. Covers official and community providers and libraries such as airflow-dbt-python |
| Airflow's folders | `discover_airflow_folders` (`true`) | Airflow's `plugins_folder` and `dags_folder`, each as an import root, exactly as Airflow puts them on `sys.path` |
| Custom folders | `custom_operator_paths` | Folders that are import roots, for example `["/opt/airflow/include"]` |
| Installed import packages | `extra_operator_packages` | Every module of the named packages, for example `["acme_ops"]` |
| Single modules | `extra_operator_modules` | Exactly these modules, for example `["acme_ops.jira"]` |
| pip distributions | `extra_operator_distributions` | Every top-level package shipped by the distribution (from `top_level.txt` or the `RECORD` file), even when it neither requires Airflow nor declares a provider entry point |
| Files and package folders | `extra_operator_paths` | Files or folders; the import root is inferred from `__init__.py` files (`/opt/acme/acme_ops/jira.py` becomes `acme_ops.jira` with `/opt/acme` on the path). A folder without `__init__.py` is itself an import root |
| Your own code | Entry point `autoair.catalog_sources` | Whatever modules your `CatalogSource` yields; see [Catalog sources](/docs/extending/catalog-sources) |

Custom roots are added to `sys.path` and `PYTHONPATH` of AutoAir's subprocesses (import probes, the DagBag import check, the task runner), so they can import the same modules the generated DAG files import.

### Examples

A folder of operators next to the DAGs, as Airflow sees it (`plugins/acme_ops/gitlab.py` is imported as `acme_ops.gitlab`): nothing to configure, `discover_airflow_folders` covers it.

An internal operator library installed with pip:

```bash
pip install acme-airflow-ops
AUTOAIR_EXTRA_OPERATOR_DISTRIBUTIONS='["acme-airflow-ops"]'
```

A checked-out folder on the image:

```bash
AUTOAIR_CUSTOM_OPERATOR_PATHS='["/opt/airflow/include"]'
AUTOAIR_EXTRA_OPERATOR_PATHS='["/opt/vendor/acme_ops"]'
```

The same in a YAML configuration file, with discovery filters:

```yaml
extra_operator_distributions: [acme-airflow-ops]
extra_operator_paths: [/opt/airflow/include/acme_ops]
discovery:
  skip_dirs_extra: [build, dist]
  skip_module_patterns_extra: ['\.legacy(\.|$)']   # hide a deprecated sub-package
  class_hint_patterns_extra: ['Task']               # also parse files defining *Task classes
  exclude_distributions_extra: ['acme-airflow-testing*']
```

## Discovery filters

The `discovery` settings below apply to every source. Each list or pattern can be replaced (the base setting, for example `discovery.skip_dirs`) or extended (its companion ending in `_extra`, for example `discovery.skip_dirs_extra`); the table names both. Changing them changes the catalog's cache key, so the catalog is rebuilt.

| Setting | Default | Effect |
|---|---|---|
| `discovery.skip_dirs` / `skip_dirs_extra` | `__pycache__`, `.git`, `.hg`, `.venv`, `venv`, `.tox`, `.nox`, `node_modules`, `site-packages`, `.autoair`, `.ipynb_checkpoints` | Directories never walked (dot-directories always skipped) |
| `discovery.skip_module_pattern` / `skip_module_patterns_extra` | modules named `tests`, `example_dags`, `system`, `migrations`, `_vendor`, `decorators`, `conftest` | Module names never scanned |
| `discovery.skip_class_pattern` / `skip_class_patterns_extra` | `Decorated(Sensor)?Operator$` | Classes left out of the palette (TaskFlow internals) |
| `discovery.class_hint_pattern` / `class_hint_patterns_extra` | `Operator\|Sensor\|Notifier` | Text pre-filter; empty disables it |
| `discovery.exclude_distributions` / `exclude_distributions_extra` | `apache-airflow`, `apache-airflow-core`, `apache-airflow-task-sdk`, `autoair` | Distributions never scanned as operator libraries |
| `discovery.max_file_bytes` | `2000000` | Larger files are skipped |
| `discovery.parallel_threshold`, `discovery.workers` | `300`, `min(4, CPUs)` | Parse in parallel processes above this many modules |
| `discovery.cache_max_files` | `8` | Catalog cache files kept |
| `discovery.probe_timeout` | `120` | Seconds the import probe of operator modules may take |

## Palette visibility and policy

Discovery decides what exists; these settings decide what users may pick:

| Setting | Effect |
|---|---|
| `operator_allow` (`["*"]`), `operator_deny` (`[]`) | fnmatch patterns of class paths shown in the palette |
| `forbidden_operators` | fnmatch patterns that fail validation |
| `policy.privileged_operators` (+ `_extra`) | Operators that run arbitrary commands (`BashOperator`, `BashSensor` by default); need `allow_privileged_operators` |

See [Policy](/docs/admin/policy).

## Python references

Parameters that take Python objects (for example `python_callable`, Kafka's `producer_function` or `GrpcOperator.stub_class`) are given as an import path: `{"$type": "import", "path": "acme_ops.funcs.greet"}`, shown as **Python reference** in the parameter's value menu. The generated DAG imports the object. This needs `allow_python_code=true`; `python_ref_allow` (fnmatch, default `["*"]`) restricts what may be referenced, for example `["acme_ops.*"]`. Validation checks statically that the module exists and defines the name.

## Caching and refresh

Catalog builds are cached on disk (`cache_dir`, default `$XDG_CACHE_HOME/autoair`). The cache key includes the installed Airflow distributions, AutoAir's version, a hash of the catalog-building code, the discovery filters, the strategy and, for custom sources, each module's path, size and modification time. Editing a custom operator file therefore produces a new catalog on the next build; there is no cache to clear by hand.

For production images precompute the catalog (`autoair catalog build --output ...` and `AUTOAIR_CATALOG_FILE`). A precomputed file built before you added a custom source is ignored because its key no longer matches.

To rebuild a running server's catalog use **Rebuild catalog** in the [admin panel](/docs/admin/admin-panel) or `POST /api/catalog/refresh` (permission `catalog:refresh`, rate limit `limits.rate_limits.catalog_refresh`, 6 per minute). It acts on the replica that serves the request.

Inspect the result:

```bash
autoair catalog dump | jq -r '.[] | select(.fqn | startswith("acme_ops.")) | .fqn'
autoair catalog dump --details > catalog.json
autoair catalog dump --diagnostics   # sources, filtered operators and why, failed modules, metadata
```

`GET /api/catalog/stats` reports operator and provider counts and discovery errors. `GET /api/catalog/diagnostics` (permission `catalog:refresh`) and the **Catalog** tab of the admin panel show the full build report: what each source scanned and found, what was filtered out and by which setting, modules that failed to parse or import, the package metadata applied, the build time and duration.

## Runtime requirements

AutoAir only describes the operator. The **generated DAG imports it**, so the module must be importable on the DAG processor and the workers: install the same package, or ship the folder in the plugins or DAG folder. The DagBag import check on deploy (`import_check_on_deploy`) catches a missing module before the file is written, if AutoAir's environment matches Airflow's.

## Troubleshooting

| Symptom | Check |
|---|---|
| Operator missing from the palette | Is the file under a scanned root? Does its text match `class_hint_pattern`? Is the module or class name excluded by a filter? Is it hidden by `operator_deny`? The *Filtered* list of the catalog diagnostics names the setting |
| Operator listed but marked unsupported | A required parameter takes a Python object the builder cannot express; use a Python reference or the TaskFlow equivalent offered in the inspector |
| Parameters missing or untyped | Base class in a module AutoAir cannot locate; add that module's root to `custom_operator_paths` |
| Catalog differs between replicas | Different packages installed; use one image and `AUTOAIR_CATALOG_FILE` |
| Deploy fails with `ModuleNotFoundError` in the import check | The module is not importable in AutoAir's environment; install it or add its root |

## Related pages

- [Catalog sources](/docs/extending/catalog-sources)
- [Operator metadata](/docs/extending/operator-metadata)
- [Operators and parameters](/docs/user-guide/operators-and-parameters)
- [`discovery` settings](/docs/reference/configuration#discovery)
