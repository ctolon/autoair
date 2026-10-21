# Authoring DagSpec documents

## Document shape
A DagSpec (`spec_version: 3`) holds DAG settings plus `nodes` and `edges`.
Edges are `{ "source": "<node id>", "target": "<node id>" }` (source runs first).

## Node kinds
- `operator`: any catalog operator. `operator` is the fully qualified class name,
  `params` its constructor arguments (only parameters from the operator's schema).
- `python_task`: a TaskFlow function. `function_name`, `code` (the function **body**;
  `return` pushes XCom), optional `decorator` (`task`, `task.virtualenv`, ...),
  `arguments`.
- `task_group`: groups nodes; members set `parent_group` to the group's node id.
- `custom`: an installed custom node handler (`handler`, `config`).
- `note`: canvas sticky note, not a task.

Task settings are node fields, never `params`: `retries`, `retry_delay_seconds`,
`retry_exponential_backoff`, `execution_timeout_seconds`, `pool`, `pool_slots`, `queue`,
`trigger_rule`, `owner`, `doc_md`, `max_active_tis_per_dagrun`, `depends_on_past`,
`outlets`, `inlets`, `mapping`, `callbacks`.

## Values
- XCom reference: `{"$type": "xcom", "node": "<node id>", "key": null}`.
- Timedelta: `{"$type": "timedelta", "seconds": 600}`; datetime:
  `{"$type": "datetime", "value": "2025-01-01T00:00:00Z"}`.
- Python object (callables, classes): `{"$type": "import", "path": "pkg.module.name"}`.
- Jinja templates are allowed in templated fields, e.g. `"{{ ds }}"`.

## Schedules
`{"type": "none"}`, `{"type": "preset", "preset": "@daily"}`,
`{"type": "cron", "expression": "0 6 * * *", "timezone": "UTC"}`,
`{"type": "interval", "seconds": 3600}`, asset schedules
`{"type": "assets", "expression": {"op": "asset", "asset": {"name": "orders"}}}`.

## DAG-level settings
`dag_id`, `description`, `doc_md`, `tags`, `start_date`, `catchup`, `max_active_runs`,
`max_active_tasks`, `dagrun_timeout_seconds`, `default_args` (`owner`, `retries`,
`retry_delay_seconds`, `execution_timeout_seconds`, `pool`, `queue`, `email`), `params`
(run-time parameters with JSON-schema validation), `callbacks`, `deadlines`, `team`.

## Validation
Problems carry a JSON pointer `path` (e.g. `/nodes/2/params/sql`), the `node_id` and the
`field`. Fix every `error`; `warning`s are policy or best-practice hints.

## Workflow for agents
1. Search operators (`search_knowledge` / `list_operators`) and read the parameter schema
   (`get_operator_schema`).
2. Draft the full document; reference existing connection ids (`list_connections`).
3. `validate_dag_spec` until `ok`; preview with `generate_code`.
4. Review (`review_dag_spec`, `scan_dag_leaks`) before saving.
5. Save only after the user confirms (`save_dag_spec` with `confirm: true`).
