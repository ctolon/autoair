---
title: Astronomer Blueprint steps
description: Insert Astronomer Blueprint templates (airflow-blueprint) as configurable canvas nodes, preview the tasks they render and generate DAGs that call them.
sidebar_label: Blueprint steps
---

[Astronomer Blueprint](https://github.com/astronomer/blueprint) (`airflow-blueprint` on PyPI) lets a platform team write reusable task templates in Python: a `Blueprint[Config]` class with a Pydantic config model and a `render()` method that returns one task or a TaskGroup. AutoAir lists the installed templates in the palette and inserts each one as a single **blueprint step** node. You fill in its config in a form generated from the Pydantic model, see the tasks it renders, and connect it like any other node.

This page is about Astronomer Blueprint. AutoAir's own reusable tasks are [operator templates](/docs/user-guide/operator-templates), which the UI also calls blueprints in some places.

## Requirements

| Where | What |
|---|---|
| AutoAir's environment | `pip install 'autoair[blueprint]'` (installs `airflow-blueprint>=0.3,<1`) and the packages or folders with your templates. AutoAir imports the template modules in a child process to read their config schemas; without the package the palette still lists the templates, but configs are not checked. |
| Airflow's environment | `airflow-blueprint` and the same template modules. The generated DAG imports them at parse time. Validation reminds you of this with an info message on every DAG with blueprint steps. |
| Feature switch | `features.blueprints` (on by default). Off: no palette section, the `/api/blueprints` routes answer 404, the MCP tool `list_blueprints` reports `available: false` and validation refuses blueprint steps (`blueprint_disabled`). |

Blueprint requires Python 3.10 or newer and supports Airflow 3.

## Where AutoAir finds templates

AutoAir discovers templates the way the blueprint registry does, but without importing anything in the API process: it reads the Python files and keeps classes whose base is written `Blueprint` or `Blueprint[Config]`.

| Source | Setting | Module name in the generated DAG |
|---|---|---|
| Installed packages that declare the `airflow_blueprint.blueprints` entry point (the package and all its submodules; names starting with `_` are skipped) | `blueprints.entry_points` (default `true`) | the package's own module, for example `company_blueprints.etl` |
| Folders of template files | `blueprints.template_dirs` (default empty) | the file's path relative to the folder, for example `etl` for `<folder>/etl.py` |
| Explicit modules | `blueprints.modules` (default empty) | as configured |

Template files are code that AutoAir imports (in a child process without AutoAir's secrets) to read schemas, and every template becomes an allowed import of generated DAGs. List only folders that administrators control in `template_dirs`, never a folder that DAG authors deploy to; shared packages through the entry point are the safer default.

A folder in `template_dirs` should be on Airflow's `sys.path` (for example a folder next to the dags folder that Airflow imports from) (or another folder on Airflow's `sys.path`); otherwise the import in the generated DAG fails in Airflow. The import check and the task preview run in AutoAir's DagBag workers, which import modules the same way: a template folder must also be importable there (it is the dags folder in plugin mode; add it to `PYTHONPATH` in standalone mode). Packages advertised through the entry point need nothing extra.

Names and versions follow the blueprint project: the class name in snake_case is the name (`MultiSourceETL` is `multi_source_etl`), a `V<n>` suffix is the version (`ExtractV2` is version 2 of `extract`), and the `name` and `version` class attributes override both. Two classes with the same name and version are reported as catalog errors; the first one is used.

The catalog is rebuilt when a source file changes (checked every `blueprints.rescan_interval` seconds, default 30) and on `POST /api/blueprints/refresh` (permission `catalog:refresh`). Other limits: `blueprints.max_files` (default 2000 files per build), `blueprints.schema_timeout` (default 120 s for reading schemas), `blueprints.preview_timeout` (default 60 s per preview). See the [configuration reference](/docs/reference/configuration).

## Add a blueprint step

1. In the palette, open **Blueprints (Astronomer)** or search for the template name. Each template appears once, with its newest version.
2. Double-click it, drag it onto the canvas or press Enter. The node is named after the template (`extract_1`).
3. In the inspector:
   - **Step id** is the task or group id the template renders under (its `step_id`). Inside a task group the Airflow id gets the group prefix.
   - **Version** pins the step to one version. Every version is its own class with its own config model; values are kept when you switch, so check them against the new form.
   - **Task group** places the step inside a task group of the canvas. Loop groups (Airflow 3.4 task loops) cannot hold blueprint steps.
   - **Disabled** leaves the step out of the generated DAG; dependencies through it are bridged, like disabled tasks.
   - **Configuration** is a form generated from the config model's JSON Schema (`Blueprint.get_schema()`). Fields you leave empty use the model's defaults.
4. Connect the node. An edge to or from a blueprint step is a dependency on everything it renders, like an edge to a task group.

**Task preview** in the inspector lists the tasks the step renders with the current config, shortly after you stop typing (**Refresh** renders it again). The node on the canvas shows the same list; the chevron collapses it to a task count. While the config is empty the node shows the catalog's default preview, which exists only for templates whose defaults are a complete config. A preview that cannot be rendered (a config the model rejects, a module that fails to import) shows the error instead. Rendering runs the template's code, so it follows the import-check rule: with `iam.import_check_requires_code_execute` on (the default), only users holding `code:execute` get previews.

Blueprint steps are not tasks: they have no XCom output of their own, so they are not offered as XCom sources, and config values are plain JSON (no XCom references, timedeltas or other tagged values). Retries, pools and other task settings belong in the template.

## Generated code

The Python output calls the template the way the blueprint project documents for hand-written DAGs:

```python
from company_blueprints.etl import Extract, ExtractConfig

with DAG(dag_id="customer_pipeline", schedule="@daily", catchup=False) as dag:
    g_ingest = TaskGroup(group_id="ingest")
    extract_customers_blueprint = Extract()
    extract_customers_blueprint.step_id = "extract_customers"
    with g_ingest:
        g_extract_customers = extract_customers_blueprint.render(
            ExtractConfig(source_table="raw.customers", batch_size=500)
        )
    g_extract_customers >> t_load
```

When the config model cannot be imported from the template's module by name, the code asks the class for it: `Extract.get_config_type()(...)`. Config keys that are not Python identifiers go through `model_validate({...})`. Values are always literals.

| Output mode | Blueprint steps |
|---|---|
| `python` | supported |
| `loader` | supported (the loader builds the same code at parse time) |
| `yaml` (Airflow YAML Dags) | not supported: validation reports `blueprint_output_mode` and the export lists the step as an error |
| dag-factory export | the step and its dependencies are skipped, with a warning |

The blueprint project's own `.dag.yaml` format, `BlueprintDagArgs` templates, `trigger_rule` per step and runtime parameter overrides (`supports_params`) are not part of AutoAir's spec; a template with `supports_params = True` still renders, but AutoAir does not add the step's DAG params.

## Validation

| Code | Severity | Meaning |
|---|---|---|
| `blueprint_disabled` | error | `features.blueprints` is off |
| `blueprint_unknown` | error | the name and version are not in the catalog (the message lists the available versions) |
| `blueprint_class` | error | `class_path` or `config_class` differ from the catalog entry. The catalog is the import allowlist of generated code |
| `blueprint_config` | error | the config does not match the schema, or holds a tagged value |
| `blueprint_loop` | error | the step is inside a loop group |
| `blueprint_output_mode` | error | the DAG uses the `yaml` output mode |
| `xcom_blueprint` | error | an XCom reference points at a blueprint step |
| `blueprint_schema` | warning | the schema could not be read (the reason says why); the config is not checked |
| `blueprint_outdated` | info | a newer version of the template exists |
| `blueprint_runtime` | info | the Airflow environment needs `airflow-blueprint` and the template module |

Organisation policy rules (`max_tasks`, forbidden and privileged operators) see a blueprint step as one node; they do not inspect the tasks the template renders. The schema check covers what JSON Schema expresses (types, required fields, `ge`/`le`, patterns, `Literal` values). Custom `@field_validator` and `@model_validator` logic runs only when Airflow parses the DAG, and in the import check.

## Import from Python

[Importing a Python DAG](/docs/user-guide/import-export) recognises blueprint calls: every `render()` of a `Blueprint` subclass becomes one blueprint step with its step id, version, class and config (the fields you set), and the tasks it rendered are not imported as separate nodes. The template module must be importable by the import (the DAG's repository root or `PYTHONPATH`).

## API and MCP

| Endpoint | Permission | Purpose |
|---|---|---|
| `GET /api/blueprints` | `builder:use` | every template version (palette) |
| `GET /api/blueprints/{name}/{version}` | `builder:use` | one version with `config_schema`, docstring and default preview |
| `GET /api/blueprints/status` | `builder:use` | whether `airflow-blueprint` is installed, counts and catalog errors |
| `POST /api/blueprints/preview` | `builder:use` (and `code:execute` while `iam.import_check_requires_code_execute` is on) | tasks one step renders with a config (rate-limit group `validate`) |
| `POST /api/blueprints/refresh` | `catalog:refresh` | rebuild the catalog (rate-limit group `catalog_refresh`) |

All of them need `features.blueprints`. The MCP tool `list_blueprints` returns the same entries with their schemas, and `list_node_kinds` describes the `blueprint` node kind. The node's fields are in the [spec format](/docs/extending/spec-format#blueprint).
