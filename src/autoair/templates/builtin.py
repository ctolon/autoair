"""Built-in starter templates (validated by tests against the spec and Airflow)."""

from __future__ import annotations

from typing import Any

_START = "2026-01-01T00:00:00Z"
BASH = "airflow.providers.standard.operators.bash.BashOperator"
EMPTY = "airflow.providers.standard.operators.empty.EmptyOperator"
SQL = "airflow.providers.common.sql.operators.sql.SQLExecuteQueryOperator"
FILE_SENSOR = "airflow.providers.standard.sensors.filesystem.FileSensor"
TRIGGER = "airflow.providers.standard.operators.trigger_dagrun.TriggerDagRunOperator"
APPROVAL = "airflow.providers.standard.operators.hitl.ApprovalOperator"


def _pos(x: int, y: int) -> dict[str, int]:
    return {"x": x, "y": y}


BUILTIN_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "etl-basic",
        "title": "Daily ETL",
        "description": "Extract with Bash, transform in Python, load with SQL — the classic shape.",
        "category": "data",
        "tags": ["etl", "starter"],
        "kind": "dag",
        "builtin": True,
        "requires": ["apache-airflow-providers-common-sql"],
        "spec": {
            "dag_id": "daily_etl",
            "description": "Daily extract-transform-load",
            "schedule": {"type": "cron", "expression": "0 3 * * *"},
            "start_date": _START,
            "tags": ["etl"],
            "default_args": {"owner": "data", "retries": 2, "retry_delay_seconds": 300},
            "nodes": [
                {
                    "kind": "operator",
                    "id": "extract",
                    "task_id": "extract",
                    "operator": BASH,
                    "params": {"bash_command": "echo 'extract for {{ ds }}'"},
                    "position": _pos(0, 0),
                },
                {
                    "kind": "python_task",
                    "id": "transform",
                    "task_id": "transform",
                    "function_name": "transform",
                    "code": "rows = [1, 2, 3]\nreturn len(rows)",
                    "position": _pos(280, 0),
                },
                {
                    "kind": "operator",
                    "id": "load",
                    "task_id": "load",
                    "operator": SQL,
                    "params": {
                        "conn_id": "warehouse",
                        "sql": "SELECT {{ ti.xcom_pull('transform') }}",
                    },
                    "position": _pos(560, 0),
                },
            ],
            "edges": [
                {"source": "extract", "target": "transform"},
                {"source": "transform", "target": "load"},
            ],
        },
    },
    {
        "id": "event-driven-consumer",
        "title": "Asset-triggered consumer",
        "description": "Runs whenever upstream DAGs update the `orders` asset "
        "(data-aware scheduling).",
        "category": "event-driven",
        "tags": ["assets", "event-driven"],
        "kind": "dag",
        "builtin": True,
        "spec": {
            "dag_id": "orders_consumer",
            "schedule": {
                "type": "assets",
                "expression": {"op": "asset", "asset": {"name": "orders"}},
            },
            "start_date": _START,
            "tags": ["assets"],
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "read",
                    "task_id": "read_events",
                    "function_name": "read_events",
                    "code": "print('triggered by asset update')\nreturn True",
                    "position": _pos(0, 0),
                },
                {
                    "kind": "operator",
                    "id": "publish",
                    "task_id": "publish",
                    "operator": EMPTY,
                    "outlets": [{"name": "orders_enriched"}],
                    "position": _pos(280, 0),
                },
            ],
            "edges": [{"source": "read", "target": "publish"}],
        },
    },
    {
        "id": "human-approval",
        "min_airflow": "3.1.0",
        "title": "Human approval gate",
        "description": "Pause for a human decision (Airflow 3.1 HITL) before publishing.",
        "category": "human-in-the-loop",
        "tags": ["hitl", "approval"],
        "kind": "dag",
        "builtin": True,
        "spec": {
            "dag_id": "approval_gate",
            "schedule": {"type": "none"},
            "start_date": _START,
            "tags": ["hitl"],
            "nodes": [
                {
                    "kind": "operator",
                    "id": "prepare",
                    "task_id": "prepare_report",
                    "operator": BASH,
                    "params": {"bash_command": "echo report ready"},
                    "position": _pos(0, 0),
                },
                {
                    "kind": "operator",
                    "id": "approve",
                    "task_id": "approve",
                    "operator": APPROVAL,
                    "params": {
                        "subject": "Publish report for {{ ds }}?",
                        "body": "Review the report and approve publishing.",
                    },
                    "position": _pos(280, 0),
                },
                {
                    "kind": "operator",
                    "id": "publish",
                    "task_id": "publish",
                    "operator": BASH,
                    "params": {"bash_command": "echo published"},
                    "position": _pos(560, 0),
                },
            ],
            "edges": [
                {"source": "prepare", "target": "approve"},
                {"source": "approve", "target": "publish"},
            ],
        },
    },
    {
        "id": "branching",
        "title": "Branch and join",
        "description": "Choose a path at runtime with @task.branch and join with a trigger rule.",
        "category": "control-flow",
        "tags": ["branch"],
        "kind": "dag",
        "builtin": True,
        "spec": {
            "dag_id": "branch_and_join",
            "schedule": {"type": "none"},
            "start_date": _START,
            "params": {"mode": {"default": "full", "type": "string", "enum": ["full", "delta"]}},
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "choose",
                    "task_id": "choose",
                    "decorator": "branch",
                    "function_name": "choose",
                    "arguments": {"mode": "{{ params.mode }}"},
                    "code": "return 'full_load' if mode == 'full' else 'delta_load'",
                    "position": _pos(0, 80),
                },
                {
                    "kind": "operator",
                    "id": "full",
                    "task_id": "full_load",
                    "operator": EMPTY,
                    "position": _pos(280, 0),
                },
                {
                    "kind": "operator",
                    "id": "delta",
                    "task_id": "delta_load",
                    "operator": EMPTY,
                    "position": _pos(280, 160),
                },
                {
                    "kind": "operator",
                    "id": "join",
                    "task_id": "join",
                    "operator": EMPTY,
                    "trigger_rule": "none_failed_min_one_success",
                    "position": _pos(560, 80),
                },
            ],
            "edges": [
                {"source": "choose", "target": "full", "label": "full"},
                {"source": "choose", "target": "delta", "label": "delta"},
                {"source": "full", "target": "join"},
                {"source": "delta", "target": "join"},
            ],
        },
    },
    {
        "id": "dynamic-mapping",
        "title": "Fan-out over files",
        "description": "List inputs, process each one in parallel (dynamic task mapping), "
        "then aggregate.",
        "category": "parallelism",
        "tags": ["mapping"],
        "kind": "dag",
        "builtin": True,
        "spec": {
            "dag_id": "fan_out_files",
            "schedule": {"type": "preset", "preset": "@daily"},
            "start_date": _START,
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "list",
                    "task_id": "list_files",
                    "function_name": "list_files",
                    "code": "return [f'part-{i}.csv' for i in range(4)]",
                    "position": _pos(0, 0),
                },
                {
                    "kind": "operator",
                    "id": "process",
                    "task_id": "process",
                    "operator": BASH,
                    "params": {},
                    "mapping": {"expand": {"bash_command": {"$type": "xcom", "node": "list"}}},
                    "position": _pos(280, 0),
                },
                {
                    "kind": "operator",
                    "id": "done",
                    "task_id": "aggregate",
                    "operator": EMPTY,
                    "position": _pos(560, 0),
                },
            ],
            "edges": [
                {"source": "list", "target": "process"},
                {"source": "process", "target": "done"},
            ],
        },
    },
    {
        "id": "setup-teardown",
        "title": "Ephemeral resources",
        "description": "Create a resource, use it, and always clean it up (setup/teardown).",
        "category": "infrastructure",
        "tags": ["setup", "teardown"],
        "kind": "dag",
        "builtin": True,
        "spec": {
            "dag_id": "ephemeral_resources",
            "schedule": {"type": "none"},
            "start_date": _START,
            "nodes": [
                {
                    "kind": "operator",
                    "id": "create",
                    "task_id": "create_workspace",
                    "operator": BASH,
                    "is_setup": True,
                    "params": {"bash_command": "mkdir -p /tmp/ws_{{ run_id | replace(':', '_') }}"},
                    "position": _pos(0, 0),
                },
                {
                    "kind": "operator",
                    "id": "work",
                    "task_id": "work",
                    "operator": BASH,
                    "params": {"bash_command": "echo working"},
                    "position": _pos(280, 0),
                },
                {
                    "kind": "operator",
                    "id": "cleanup",
                    "task_id": "cleanup",
                    "operator": BASH,
                    "is_teardown": True,
                    "params": {"bash_command": "rm -rf /tmp/ws_{{ run_id | replace(':', '_') }}"},
                    "position": _pos(560, 0),
                },
            ],
            "edges": [
                {"source": "create", "target": "work"},
                {"source": "work", "target": "cleanup"},
                {"source": "create", "target": "cleanup"},
            ],
        },
    },
    {
        "id": "wait-for-file",
        "title": "Wait for a file",
        "description": "A reschedule-mode sensor waits for a file without holding a worker slot.",
        "category": "sensors",
        "tags": ["sensor"],
        "kind": "dag",
        "builtin": True,
        "spec": {
            "dag_id": "wait_for_file",
            "schedule": {"type": "cron", "expression": "0 * * * *"},
            "start_date": _START,
            "nodes": [
                {
                    "kind": "operator",
                    "id": "wait",
                    "task_id": "wait_for_input",
                    "operator": FILE_SENSOR,
                    "params": {
                        "filepath": "/data/incoming/{{ ds }}.csv",
                        "mode": "reschedule",
                        "poke_interval": 60,
                        "timeout": 3600,
                    },
                    "position": _pos(0, 0),
                },
                {
                    "kind": "operator",
                    "id": "process",
                    "task_id": "process",
                    "operator": BASH,
                    "params": {"bash_command": "wc -l /data/incoming/{{ ds }}.csv"},
                    "position": _pos(280, 0),
                },
            ],
            "edges": [{"source": "wait", "target": "process"}],
        },
    },
    {
        "id": "trigger-downstream",
        "title": "Trigger another DAG",
        "description": "Kick off a downstream DAG and wait for it to finish.",
        "category": "orchestration",
        "tags": ["trigger"],
        "kind": "dag",
        "builtin": True,
        "spec": {
            "dag_id": "trigger_downstream",
            "schedule": {"type": "none"},
            "start_date": _START,
            "nodes": [
                {
                    "kind": "operator",
                    "id": "trigger",
                    "task_id": "trigger_reporting",
                    "operator": TRIGGER,
                    "params": {
                        "trigger_dag_id": "reporting",
                        "wait_for_completion": True,
                        "poke_interval": 30,
                    },
                    "position": _pos(0, 0),
                },
            ],
            "edges": [],
        },
    },
    {
        "id": "fragment-notify-on-failure",
        "title": "Failure alert step",
        "description": "A final step that only runs when something upstream failed.",
        "category": "building-blocks",
        "tags": ["fragment", "alerting"],
        "kind": "fragment",
        "builtin": True,
        "fragment": {
            "nodes": [
                {
                    "kind": "operator",
                    "id": "alert",
                    "task_id": "alert_on_failure",
                    "operator": BASH,
                    "trigger_rule": "one_failed",
                    "params": {"bash_command": "echo 'run {{ run_id }} failed'"},
                    "position": _pos(0, 0),
                },
            ],
            "edges": [],
        },
    },
    {
        "id": "fragment-extract-group",
        "title": "Extract group",
        "description": "Task group with download + validate steps.",
        "category": "building-blocks",
        "tags": ["fragment", "group"],
        "kind": "fragment",
        "builtin": True,
        "fragment": {
            "nodes": [
                {
                    "kind": "task_group",
                    "id": "grp",
                    "group_id": "extract",
                    "position": {"x": 0, "y": 0, "width": 560, "height": 180},
                },
                {
                    "kind": "operator",
                    "id": "dl",
                    "task_id": "download",
                    "operator": BASH,
                    "parent_group": "grp",
                    "params": {"bash_command": "echo download"},
                    "position": _pos(24, 60),
                },
                {
                    "kind": "operator",
                    "id": "val",
                    "task_id": "validate",
                    "operator": BASH,
                    "parent_group": "grp",
                    "params": {"bash_command": "echo validate"},
                    "position": _pos(300, 60),
                },
            ],
            "edges": [{"source": "dl", "target": "val"}],
        },
    },
]
