# Airflow 3 best practices for AutoAir DAGs

## Idempotency and data intervals
Every task must produce the same result when re-run for the same logical date. Read the
run's interval from templates (`{{ data_interval_start }}`, `{{ data_interval_end }}`,
`{{ ds }}`, `{{ logical_date }}`) or from `get_current_context()` - never from the wall
clock (`datetime.now()`, `NOW()`, `$(date)`). Write with overwrite/merge semantics
(`INSERT ... ON CONFLICT`, `MERGE`, partition overwrite, deterministic object keys) so
retries and backfills do not duplicate data.

## Removed in Airflow 3
- Template variables `execution_date`, `next_ds`, `prev_ds`, `tomorrow_ds`, `yesterday_ds`,
  `next_execution_date`, `prev_execution_date` (and `_nodash` variants) are gone. Use
  `logical_date`, `data_interval_start`, `data_interval_end`,
  `prev_data_interval_start_success`.
- Task-level `sla` and `sla_miss_callback` are removed: use DAG **deadlines**
  (Deadline Alerts, `deadlines` in the spec).
- Tasks cannot open metadata DB sessions (`create_session`, `provide_session`,
  `airflow.settings.Session`). Use the Task SDK (`from airflow.sdk import Variable,
  Connection, get_current_context`) or the REST API.
- Core operators moved to `airflow.providers.standard` (`BashOperator`,
  `PythonOperator`, `EmptyOperator`, sensors). `DummyOperator` is gone.
- `schedule_interval` and `timetable` arguments are replaced by `schedule`.
- SubDAGs are removed: use task groups.

## Retries and timeouts
- Default to `retries: 2` and `retry_delay_seconds: 300` in `default_args`; disable retries
  for non-idempotent steps. Use `retry_exponential_backoff` for rate-limited APIs.
- Always bound work: `execution_timeout_seconds` on tasks (Airflow has no default) and
  `dagrun_timeout_seconds` on scheduled DAGs. Sensors default to a 7-day `timeout`.
- Size timeouts from run history: about 3x the p95 duration and at least 1.5x the slowest
  successful run.

## Waiting efficiently
Sensors and long-running remote jobs should be **deferrable** (`deferrable: true`) when the
operator supports it: the wait moves to the triggerer and frees the worker slot. Otherwise
use `mode: "reschedule"` with a `poke_interval` of minutes, not seconds.

## Concurrency and shared resources
- Use **pools** for shared external systems (databases, APIs with quotas); set
  `pool_slots` for heavy tasks.
- `max_active_runs` caps overlapping runs (set it to 1 when runs must not overlap or with
  `catchup: true`); `max_active_tasks` caps parallel tasks of a DAG.
- Bound dynamic task mapping with `max_active_tis_per_dagrun`.

## Secrets
- Never hard-code credentials in parameters, SQL, shell commands or code. Reference
  Airflow **connections** by `*_conn_id`; read variables at runtime.
- Do not render secrets into templated fields (`{{ conn.x.password }}`): rendered fields
  are stored in the metadata DB and shown in the UI.
- Do not log or `return` secrets: logs are readable by anyone with log access and return
  values become XCom rows.
- `set -x` in shell commands prints expanded secrets.

## XCom
Return small values (ids, paths, counts). Put large data in object storage and pass the
URI; XCom is stored in the metadata database.

## Observability and ownership
Set `owner`, `tags`, `description` and `doc_md` (what the DAG does, inputs, outputs, how to
recover). Add `on_failure` notifiers or deadlines so failures reach the owning team.

## Assets (data-aware scheduling)
Declare `outlets` on tasks that produce data and schedule consumers on assets instead of
time-based polling sensors.
