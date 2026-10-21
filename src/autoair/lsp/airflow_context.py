"""Typed names of Airflow's task context, for the editor's language services only.

Task code may use the context (``ti``, ``ds``, ``params``, ``dag_run`` …). The language
services wrap a task body in its function signature and import these names, so completion
(``ti.xcom_pull``), hover and signature help know their types. The module is never imported
at run time: the names are annotations without values (``from … import *`` in an analysed
wrapper is all that reads them).
"""

# ruff: noqa: F401
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from airflow.sdk import Connection, Variable
    from airflow.sdk.definitions.context import Context
    from airflow.sdk.types import (
        DagRunProtocol,
        OutletEventAccessorsProtocol,
        RuntimeTaskInstanceProtocol,
    )
    from pendulum import DateTime

ti: RuntimeTaskInstanceProtocol
task_instance: RuntimeTaskInstanceProtocol
dag_run: DagRunProtocol
params: dict[str, Any]
ds: str
ds_nodash: str
ts: str
ts_nodash: str
ts_nodash_with_tz: str
logical_date: DateTime
data_interval_start: DateTime
data_interval_end: DateTime
prev_data_interval_start_success: DateTime | None
prev_data_interval_end_success: DateTime | None
prev_start_date_success: DateTime | None
prev_end_date_success: DateTime | None
run_id: str
task_instance_key_str: str
try_number: int
test_mode: bool
templates_dict: dict[str, Any] | None
outlet_events: OutletEventAccessorsProtocol
triggering_asset_events: Any
inlets: list[Any]
outlets: list[Any]
conn: Connection
var: Variable

__all__ = [
    "conn",
    "dag_run",
    "data_interval_end",
    "data_interval_start",
    "ds",
    "ds_nodash",
    "inlets",
    "logical_date",
    "outlet_events",
    "outlets",
    "params",
    "prev_data_interval_end_success",
    "prev_data_interval_start_success",
    "prev_end_date_success",
    "prev_start_date_success",
    "run_id",
    "task_instance",
    "task_instance_key_str",
    "templates_dict",
    "test_mode",
    "ti",
    "triggering_asset_events",
    "try_number",
    "ts",
    "ts_nodash",
    "ts_nodash_with_tz",
    "var",
]
