---
title: Spec format
description: The DagSpec JSON document AutoAir stores for every DAG - its JSON Schema, top-level fields, nodes, edges, groups, params, tagged values and versioning.
sidebar_label: Spec format
---

Every DAG built in AutoAir is stored as a **DagSpec**: a strictly typed JSON document that is the single source of truth for the canvas, validation, code generation, the runtime loader and the exporters. The generated Python (or YAML) file is derived from it on every deploy.

The format matters when you generate specs from other tools, review them in pull requests, write CI checks, or build integrations through the [REST API](/docs/extending/rest-api) or [MCP](/docs/extending/mcp-integration).

## Where specs live

The deploy target stores each spec as `<dag_id>.autoair.json` next to the generated DAG file (for example `dags/autoair/orders_daily.autoair.json` and `orders_daily.py` in a git repository). The spec is written first and the DAG file last. In the UI, **File → Export → AutoAir spec (JSON)** downloads it and **File → Import → From AutoAir spec (JSON)…** loads one.

## JSON Schema

The schema is generated from the Pydantic models (`autoair.spec.DagSpec`):

```bash
autoair export-schema --output dag-spec.schema.json      # default output: dag-spec.schema.json
uv run autoair export-schema --output dag-spec.schema.json   # from a checkout
```

The same schema is available:

- on this site as [`/schemas/dag-spec.schema.json`](pathname:///schemas/dag-spec.schema.json) (exported by `npm run export-api` in `website/`),
- from a running server at `GET /api/schema/spec` (any signed-in builder user),
- to MCP clients through the `get_dagspec_schema` tool and the `autoair://schema/dagspec` resource.

Point your editor at it for completion and validation of `*.autoair.json` files, for example in VS Code:

```json
{
  "json.schemas": [
    {"fileMatch": ["*.autoair.json"], "url": "./dag-spec.schema.json"}
  ]
}
```

The JSON Schema checks structure only. Semantic checks (does the operator exist in the catalog, are task ids unique, is the graph acyclic, do XCom references point at upstream tasks, policy rules, Jinja, Airflow version compatibility) run in AutoAir's validation: `autoair validate <spec.json>` or `POST /api/validate`.

## Strictness and versioning

- **Unknown fields are rejected** everywhere (`additionalProperties: false`), so a typo never passes silently.
- `spec_version` is `2`. Documents are migrated step by step before validation (`autoair.spec.migrations`): a version 1 document loads as version 2. A document with a newer `spec_version` than the installed AutoAir supports is refused with "upgrade AutoAir".
- Identifiers follow Airflow's rules: `dag_id`, `task_id` and `group_id` match `^[A-Za-z0-9_.-]+$` (at most 250 characters); node `id` values match `^[A-Za-z0-9_-]+$` (at most 100); keyword-argument names are Python identifiers.

## Example

A complete, valid spec: a Bash task, a TaskFlow task inside a task group receiving the Bash task's output, an `EmptyOperator` that always runs, two params, a sticky note included in the docs, and a `dev` environment overlay.

```json
{
  "spec_version": 2,
  "dag_id": "orders_daily",
  "description": "Load yesterday's orders.",
  "schedule": {"type": "cron", "expression": "0 6 * * *", "timezone": "Europe/Istanbul"},
  "start_date": "2026-01-01T00:00:00Z",
  "catchup": false,
  "tags": ["orders", "autoair"],
  "default_args": {"owner": "data-platform", "retries": 2, "retry_delay_seconds": 300},
  "params": {
    "full_refresh": {"default": false, "type": "boolean", "title": "Full refresh"},
    "region": {"default": "eu", "type": "string", "enum": ["eu", "us"], "section": "Scope"}
  },
  "environments": {
    "dev": {"schedule": {"type": "none"}, "connections": {"warehouse": "warehouse_dev"}}
  },
  "nodes": [
    {
      "kind": "operator",
      "id": "extract",
      "task_id": "extract_orders",
      "operator": "airflow.providers.standard.operators.bash.BashOperator",
      "params": {"bash_command": "echo extracting {{ ds }} for {{ params.region }}"},
      "position": {"x": 0, "y": 0}
    },
    {
      "kind": "task_group",
      "id": "load",
      "group_id": "load",
      "tooltip": "Transform and load",
      "position": {"x": 300, "y": -40, "width": 420, "height": 200}
    },
    {
      "kind": "python_task",
      "id": "transform",
      "task_id": "transform",
      "parent_group": "load",
      "function_name": "transform",
      "arguments": {"raw": {"$type": "xcom", "node": "extract"}},
      "code": "return {'rows': len(raw or '')}",
      "position": {"x": 20, "y": 40}
    },
    {
      "kind": "operator",
      "id": "report",
      "task_id": "report",
      "parent_group": "load",
      "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
      "trigger_rule": "all_done",
      "position": {"x": 220, "y": 40}
    },
    {
      "kind": "note",
      "id": "note1",
      "text": "Owned by the data platform team.",
      "include_in_docs": true,
      "position": {"x": 0, "y": 200}
    }
  ],
  "edges": [
    {"source": "extract", "target": "transform"},
    {"source": "transform", "target": "report"}
  ]
}
```

The generated Python for this spec starts like this (`autoair generate spec.json`):

```python
from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import DAG, CronTriggerTimetable, Param, TaskGroup, task

with DAG(
    dag_id="orders_daily",
    schedule=CronTriggerTimetable("0 6 * * *", timezone="Europe/Istanbul"),
    ...
) as dag:
    g_load = TaskGroup(group_id="load", tooltip="Transform and load")
    t_extract_orders = BashOperator(
        task_id="extract_orders", bash_command="echo extracting {{ ds }} for {{ params.region }}"
    )

    @task(task_id="transform", task_group=g_load)
    def transform(raw):
        return {"rows": len(raw or "")}

    t_transform = transform(raw=t_extract_orders.output)
    t_report = EmptyOperator(task_id="report", task_group=g_load, trigger_rule="all_done")
    t_extract_orders >> t_transform
    t_transform >> t_report
```

## Top-level fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `spec_version` | `2` | `2` | Document version |
| `dag_id` | string | required | Airflow DAG id |
| `output_mode` | `python`, `loader`, `yaml` | `python` | Generated file: a full Python DAG, a small loader stub that builds the DAG from the spec at parse time, or an Airflow 3.4 YAML DAG |
| `display_name`, `description`, `doc_md` | string | none | DAG metadata |
| `schedule` | object | `{"type": "none"}` | See [Schedules](#schedules) |
| `timezone` | IANA name | none | Wall-clock zone for `start_date`/`end_date` and schedules without their own timezone |
| `start_date`, `end_date` | ISO date-time | none | |
| `catchup` | bool | `false` | |
| `tags` | string list | `[]` | |
| `max_active_runs`, `max_active_tasks` | int ≥ 1 | none | |
| `dagrun_timeout_seconds` | number > 0 | none | |
| `is_paused_upon_creation` | bool | none | |
| `default_args` | object | `{}` | `owner`, `retries`, `retry_delay_seconds`, `execution_timeout_seconds`, `depends_on_past`, `email`, `email_on_failure`, `pool`, `queue` |
| `params` | map name → [param](#params) | `{}` | Trigger-form parameters |
| `callbacks` | object | `{}` | DAG-level `on_success` / `on_failure` notifiers |
| `deadlines` | list | `[]` | Airflow 3.1+ deadline alerts: `name`, `reference` (`dagrun_queued_at` default, `dagrun_logical_date`, `fixed_datetime`, `average_runtime`), `interval_seconds`, `fixed_datetime` (required with that reference), `average_max_runs`, `callback`, `synchronous` |
| `fail_fast`, `max_consecutive_failed_dag_runs`, `owner_links`, `render_template_as_native_obj`, `rerun_with_latest_version` | | none | Passed to the DAG |
| `team` | `^[a-z0-9_-]+$` | none | Multi-team: selects the team's deploy target and pickers |
| `lineage` | `inherit`, `enabled`, `disabled` | `inherit` | OpenLineage selective enable/disable |
| `environments` | map name → [overlay](#environments) | `{}` | Per-environment overrides applied at deploy time |
| `nodes` | list of [nodes](#nodes) | `[]` | Tasks, groups, custom nodes, notes |
| `edges` | list of [edges](#edges) | `[]` | Dependencies |

## Nodes

`nodes` is a list discriminated by `kind`. Every node has:

| Field | Meaning |
|---|---|
| `id` | Stable node id used by edges, XCom references and groups (not the task id) |
| `label` | Canvas label only |
| `icon` | Canvas icon override, `builtin:<logo>` or `custom:<name>` |
| `parent_group` | `id` of the `task_group` node containing this node |
| `position` | Canvas geometry `{x, y, width, height}`, relative to the parent group; ignored by code generation |

### Task fields

The three task kinds (`operator`, `python_task`, `custom`) share:

| Field | Meaning |
|---|---|
| `task_id` | Airflow task id (required) |
| `trigger_rule` | `all_success`, `all_failed`, `all_done`, `all_done_min_one_success`, `all_skipped`, `one_success`, `one_done`, `one_failed`, `none_failed`, `none_failed_min_one_success`, `none_skipped`, `always`, `all_done_setup_success` |
| `retries`, `retry_delay_seconds`, `retry_exponential_backoff`, `execution_timeout_seconds` | Retry behaviour and limits |
| `retry_policy` | Airflow 3.3+ exception retry rules: `rules` (at least one `{exception, action, retry_delay_seconds, reason, match_subclasses}`; `action` is `retry`, `fail` or `default`) and `default` (`retry`, `fail` or `default`) |
| `pool`, `pool_slots`, `queue`, `owner`, `doc_md`, `priority_weight`, `weight_rule`, `task_display_name`, `executor`, `executor_config`, `max_active_tis_per_dagrun`, `depends_on_past` | Standard operator arguments |
| `inlets`, `outlets` | Asset references (`{name, uri, group, extra, watchers}`) |
| `mapping` | Dynamic task mapping: `{"expand": {...}}` or `{"expand_kwargs": <xcom ref>}`, optional `map_index_template` |
| `is_setup`, `is_teardown` | Setup/teardown tasks (not both) |
| `callbacks` | Notifiers for `on_success`, `on_failure`, `on_retry`, `on_execute`, `on_skipped`: `[{"notifier": "<class path>", "params": {...}}]` |
| `disabled` | Kept on the canvas, left out of the generated DAG |
| `lineage` | Per-task OpenLineage switch |
| `template_ref` | Operator template the task was created from (`{id, version}`); validation enforces the template's locked fields |

### `operator`

Any operator class from the catalog.

```json
{"kind": "operator", "id": "extract", "task_id": "extract_orders",
 "operator": "airflow.providers.standard.operators.bash.BashOperator",
 "params": {"bash_command": "echo {{ ds }}"}}
```

`operator` is the fully qualified class path (it must exist in the operator catalog, which is the import allowlist). `params` maps constructor arguments to [values](#values).

### `python_task`

A TaskFlow task whose body is written in the builder.

| Field | Meaning |
|---|---|
| `decorator` | `task` (default), `branch`, `short_circuit`, `virtualenv`, `external_python`, `branch_virtualenv`, `branch_external_python`, `sensor`, `bash`, `docker`, `kubernetes`, `stub` |
| `decorator_kwargs` | Arguments of the decorator |
| `function_name` | Python identifier of the generated function |
| `arguments` | Keyword arguments; values may be XCom references |
| `extra_parameters` | Function parameters without a bound value (for example keys provided by `expand_kwargs`) |
| `code` | Function body (statements); `return` pushes an XCom |
| `multiple_outputs` | Push a returned dict as separate XComs |
| `is_result` | Airflow 3.3+: mark as the DAG result (`@result`) |
| `uses_loop_context` | Inside a loop group: receive the `loop` context |

Python task nodes are privileged: they need `allow_python_code` (and `features.python_code`).

### `custom`

A node provided by an extension (`autoair.node_kinds`): `handler` names the node kind and `config` holds its settings, validated against the handler's model. See [Custom node kinds](/docs/extending/node-sdk).

### `task_group`

| Field | Meaning |
|---|---|
| `group_id` | Airflow group id |
| `tooltip`, `prefix_group_id` (default `true`) | TaskGroup arguments |
| `loop` | Airflow 3.4+ task loop: `{"max_iterations": 1..10000, "until_code": "...", "until_name": "until_done"}`; the body must have exactly one terminal task |

Groups do not contain their children; children point at the group with `parent_group`. Groups can nest the same way.

### `note`

A sticky note: `text` (up to 20000 characters), `color` (a palette name or `#rrggbb`), `include_in_docs` (append to the DAG's `doc_md`). Notes are not tasks.

## Edges

```json
{"id": "e1", "source": "extract", "target": "transform", "label": "raw rows"}
```

`source` and `target` are node `id`s (upstream → downstream). `id` and `label` are optional and only used by the canvas. An edge to or from a `task_group` node is a dependency on the whole group.

## Values

Parameter values (operator `params`, Python task `arguments` and `decorator_kwargs`, custom node `config`, notifier `params`, mapping `expand` and `expand_kwargs`, `executor_config`, asset `extra` and watcher `kwargs`, DAG param values and environment `params_defaults`) are JSON. Values that JSON cannot express are tagged objects with a `$type` key:

| `$type` | Fields | Becomes |
|---|---|---|
| `xcom` | `node`, optional `key` | The other task's output (`XComArg`), optionally indexed: `{"$type": "xcom", "node": "extract", "key": "rows"}` |
| `timedelta` | `seconds` | `datetime.timedelta(seconds=...)` |
| `datetime` | `value` (ISO) | A timezone-aware `datetime` |
| `import` | `path` | An imported Python object, `{"$type": "import", "path": "acme_ops.funcs.greet"}`; needs `allow_python_code` and `python_ref_allow` |
| `function` | `source` | A function written in place (`def ...`), emitted at module level; privileged |
| `file` | `name`, `content`, optional `run_with` | A script embedded in the DAG file and written to a temp path at parse time; the parameter receives the path (or `"<run_with> <path>"`) |

Jinja strings (`"{{ ds }}"`) are plain strings; validation checks them against Airflow's template context and the DAG's params.

## Schedules

`schedule` is discriminated by `type`:

| `type` | Fields | Airflow timetable |
|---|---|---|
| `none` | | `None` (manual) |
| `preset` | `preset`: `@once`, `@continuous`, `@hourly`, `@daily`, `@weekly`, `@monthly`, `@quarterly`, `@yearly` | Preset |
| `cron` | `expression`, `timezone`, `data_interval`, `interval_seconds`, `run_immediately` | `CronTriggerTimetable`, or `CronDataIntervalTimetable` with `data_interval: true` |
| `interval` | `seconds`, `data_interval` | `DeltaTriggerTimetable` / `DeltaDataIntervalTimetable` |
| `multi_cron` | `expressions`, `timezone` | `MultipleCronTriggerTimetable` |
| `events` | `dates`, `restrict_to_events`, `description` | `EventsTimetable` |
| `assets` | `expression`, `batch_asset_events` | Asset-triggered |
| `assets_or_time` | `expression`, `time` | `AssetOrTimeSchedule` |
| `assets_and_time` | `expression`, `time` | Airflow 3.4+ `AssetAndTimeSchedule` |
| `partitioned_cron`, `partitioned_assets` | | Airflow 3.2+ partitioned timetables |

Asset expressions are trees of `{"op": "asset", "asset": {...}}`, `{"op": "alias", "name": "..."}` and `{"op": "all" | "any", "items": [...]}`. Features that need a newer Airflow are validated against the installed version (or `target_airflow_version`).

## Params

`params` maps a Python identifier to an `airflow.sdk.Param`, rendered in Airflow's trigger form:

| Field | Meaning |
|---|---|
| `default` | Default value |
| `type` | `string`, `number`, `integer`, `boolean`, `array`, `object`, `null`, or a list of them |
| `title`, `description`, `description_md`, `section` | Form labels and grouping |
| `format` | For example `date`, `date-time`, `time`, `duration`, `multiline`, `email` |
| `enum`, `values_display`, `examples`, `const` | Choices and examples |
| `minimum`, `maximum`, `min_length`, `max_length`, `pattern`, `items` | JSON Schema constraints (`min_length` becomes `minLength`) |

## Environments

`environments` maps an environment name to an overlay applied at deploy time (`DagSpec.with_environment`):

| Field | Effect |
|---|---|
| `schedule`, `is_paused_upon_creation`, `tags`, `default_args`, `max_active_runs` | Replace the DAG's value |
| `params_defaults` | Override param defaults (or add params) |
| `connections` | Logical connection id → this environment's connection id, substituted in `*_conn_id` parameters, Jinja `conn.<id>` and hook calls |
| `variables` | Logical Variable key → this environment's key (`var.value.<key>`, `var.json.<key>`, `Variable.get("<key>")`) |

## Working with specs outside AutoAir

```bash
autoair validate orders_daily.autoair.json          # catalog, graph, policy, DagBag import check
autoair generate orders_daily.autoair.json -o orders_daily.py
autoair export-yaml orders_daily.autoair.json        # Airflow 3.4 YAML DAG
autoair export-dag-factory orders_daily.autoair.json
autoair import-python my_dag.py                      # Python DAG file -> spec(s)
```

See the [CLI reference](/docs/reference/cli) for all options.

## Related pages

- [REST API](/docs/extending/rest-api)
- [Custom node kinds](/docs/extending/node-sdk)
- [Import and export](/docs/user-guide/import-export)
- [Code mode](/docs/user-guide/code-mode)
