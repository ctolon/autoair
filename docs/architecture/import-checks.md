# Import checks and the validator service

Website guide: https://ctolon.github.io/autoair/docs/deployment/validator-service

The DagBag import check executes DAG code. This note describes where that code runs, the
protocol between the main server and the validator service, and the trade-offs behind the
defaults.

## Where the check runs

`autoair.validation.dagbag_check.check_source` is the only entry point (called by
`validate_spec` and `validate_raw`; deploys, **Validate in Airflow**, MCP and AI tools all go
through them).

| `validator.url` | `fail_mode` | Validator answers | Validator cannot be asked |
|---|---|---|---|
| empty | - | - | local warm pool (`validation/pool.py`) |
| set | `closed` | its result | `ImportCheckUnavailable` -> error problem `import_service`, deploy refused |
| set | `open` (default) | its result | `ImportCheckUnavailable` -> warning problem `import_service`, no import |
| set | `local` | its result | local warm pool, with a log warning |

A caller that passes `use_pool=` explicitly gets a local check (tests, tooling). "Cannot be
asked" means: no key, connection error or HTTP 429/502/503/504 after the retries, a timeout,
HTTP 401/403, another 4xx/5xx, an invalid or oversized answer, or the circuit below being open.
HTTP 413 is not an outage: it becomes an `import` error ("larger than the validator accepts").

## Protocol

`POST <validator.url>/v1/check`, body `{"source", "filename", "timeout"}`, answer
`ImportCheckResult` (`ok`, `dag_ids`, `task_ids`, `import_errors`).

`POST <validator.run_url or url>/v1/run` (test runs, `validation/service.py::RunRequest`):
`{"kind", "source", "dag_id", "task_id", "conf", "mock_xcoms", "logical_date", "timeout",
"max_log_chars", "max_value_chars"}`, answer a `TaskTestResult` / `DagTestResult`, signed the
same way. The service runs each request in a one-shot `autoair.runner._child` process with the
`run` profile of `validation/sandbox.py` (no subprocess/socket/write denials; `/proc` and
scratch reads denied; optional PID namespace; network unless `validator.run_network=false`),
never forwarding it again (`remote=False`). Client side (`runner._on_validator`): an
unavailable validator fails the run for `fail_mode` `open` and `closed`, and only `local` runs
it on the main server. A 404 (validator older than `/v1/run`) is reported as "upgrade it"
without opening the circuit.

- Authentication reuses the AI service scheme (`ai/access.py`: `sign_identity`,
  `verify_identity`) with its own headers `x-autoair-validator-identity` / `-signature` and
  role `validator`: HMAC-SHA256 over base64 JSON claims binding method, path, query, body hash,
  timestamp (300 s window) and a nonce. A path prefix in `validator.url` is signed and accepted
  by suffix, as for the AI service behind a reverse proxy.
- The answer of `/v1/check` is signed too: header `x-autoair-validator-response-signature`,
  HMAC-SHA256 with the same key over the request's nonce, the HTTP status and the body hash
  (`remote.response_signature`). The client verifies it on every 200 answer; an unsigned or
  mismatched answer counts as an invalid answer (outage), so a host between the two cannot
  return a forged "imports fine" result. 413/422 answers are not signed: they only fail the check.
- The key is the password of the Airflow connection `validator.secret_conn_id`. The main
  server reads it through Airflow (secrets backends, then the Task SDK; the environment
  variable when Airflow is not importable), the validator from `AIRFLOW_CONN_<ID>` only, so it
  never needs a metadata database. No `SecretStr` setting exists for it (invariant 1).
- Replay protection on the validator is an in-memory `ReplayCache` per process: the service
  has no shared state by design. A replay to another replica within the window re-runs a
  side-effect-free check and returns the result to whoever already holds the source.

## Client behaviour (main server)

`validation/remote.py::check_remote`:

- HTTP timeout = check timeout + `validator.request_overhead_seconds`; responses are read up to
  16 MiB (`MAX_RESPONSE_BYTES`).
- Retries (`validator.retries`, exponential `retry_backoff_seconds`) only for connection errors
  and 429/502/503/504; each attempt is signed afresh. Timeouts are not retried: the validator
  already enforces the check's own timeout, so a missing answer means it is stuck or overloaded.
- 413 and 422 are results of that request (import errors); 429/503 after the retries and a
  client timeout fail only that check, so a burst of slow DAGs or one odd request cannot take
  the validator offline for everyone.
- After an outage (connection error, 401/403, 502/504, another error, an invalid or unsigned
  answer) the
  URL is marked down in this process. Until
  `health_check_interval_seconds` passes, checks fail at once with the last reason (the editor
  does not wait on every keystroke); then `GET /ready` decides whether the next check is sent.
  This is derived per-process state with a deadline, so replicas need not agree.
- `autoair_import_checks_total{backend, result}` counts checks on `/api/metrics`.

## Validator service

`validation/service.py::create_validator_app`, started by `autoair validator-serve`:

- builds a `DagBagPool` with a `WorkerSandbox` and an environment from `user_code_env()` minus
  the key's connection variable; no `Services`, persistence, catalog or gateway;
- answers 503 above `pool_size + max_queue` concurrent checks (the client retries, the load
  balancer spreads), 413 above `max_request_bytes` (checked on `Content-Length` and while
  streaming), 422 for an invalid request (file name `^[A-Za-z0-9_][A-Za-z0-9_.-]{0,250}\.py$`);
- caps the requested timeout at `max_check_seconds`;
- is ready once a self-test DAG imported (retried every 10 s while it fails);
- makes its own process non-dumpable (`PR_SET_DUMPABLE=0`): the key sits in its initial
  environment, and `/proc/<pid>/environ` of a non-dumpable process is root-owned, so workers of
  the same user cannot read it even without the audit hook.

### Main server processes

`sandbox.harden_server_process(name, *, enabled)` makes AutoAir's own server processes
non-dumpable the same way: `autoair serve` and `autoair local` (`server.run`), each uvicorn
worker (`server.app_factory`; the flag resets on `exec`), `autoair mcp-serve --http` and
`autoair ai-serve`. DAG code they import in child processes (round-trip imports, previews,
local import checks) runs as the same user and could otherwise read their environment and
memory through `/proc`. `enabled` is `server.harden_process` (`AUTOAIR_SERVER__HARDEN_PROCESS`,
default `true`); `false` is for debugging with py-spy, gdb or core dumps and logs a warning at
start-up. It is never called inside the Airflow plugin, which would change Airflow's own
api-server process; there `validator.url` keeps user code out. On platforms other than Linux
it logs that it could not apply. The validator service always hardens itself
(`service.harden_process`), independent of the setting.

### Versions

The validator and the main server must run the same AutoAir version: there is no
mixed-version compatibility window for the protocol. Upgrade and roll back both together.

## Worker sandbox

`validation/sandbox.py`, applied by each worker before it imports Airflow
(`pool._worker_main`):

1. rlimits: `RLIMIT_CORE=0`, `RLIMIT_AS`, `RLIMIT_CPU` (soft = limit -> `SIGXCPU`, hard = limit
   + 1 s -> `SIGKILL`), `RLIMIT_FSIZE` (with `SIGXFSZ` ignored so writes fail with `EFBIG`),
   `RLIMIT_NOFILE`, optionally `RLIMIT_NPROC`;
2. `PR_SET_NO_NEW_PRIVS`, `PR_SET_DUMPABLE=0` (concurrent checks cannot read each other's
   memory or environment);
3. `unshare(CLONE_NEWUSER | CLONE_NEWNET)` when `network_isolation` is not `off` (`required`
   fails the worker start when it is refused), with an identity uid/gid map where the kernel
   allows writing it (Ubuntu's restricted user namespaces refuse it: the worker then reports the
   overflow uid 65534, while file access still uses its real uid);
4. warm imports (`airflow.sdk`, `DagBag`), then the audit hook, so Airflow's own start-up is not
   subject to it. Because user code could otherwise influence that start-up, every worker gets
   its own `HOME` and `AIRFLOW_HOME` below its scratch folder and `PYTHONNOUSERSITE=1`: an
   `airflow.cfg`, config module or user site package written by one check is never seen by
   another worker.

The audit hook denies process creation, signals, `ctypes` loading and symbol lookup
(`ctypes.dlsym`, which also covers the preloaded `ctypes.pythonapi`), socket `connect`/`bind`/
`sendto`/`sendmsg` and name resolution, writes outside the worker's own scratch folder
(`TMPDIR`, below a per-pool root), reads below the pool root outside the own folder, and any
`/proc/<pid>` other than its own. Audit hooks cannot be removed but can be bypassed from native
code, which is why the container (read-only root, non-root, no capabilities, no egress, no
credentials) is the boundary and the hook is a second line.

Workers run in their own process group (`start_new_session=True`); kill and stop signal the
whole group. `max_tasks_per_child=1` gives each check a fresh interpreter; a spare worker is
started as soon as one is taken, so the import of Airflow (about 1 s) overlaps the previous
check.

## Pipe protocol

Parent and worker exchange JSON over a socket pair (`Connection.send_bytes` /
`recv_bytes(MAX_REPLY_BYTES)`), never pickles. The worker runs user code, which can write to its
end of the pipe; unpickling that in the parent would execute code in the AutoAir (or validator)
process. A reply that is not JSON, too long or of the wrong shape counts as a crashed worker.
This applies to the local pool too.

## Local pool sandbox

Without a validator the process's own pool (`get_pool`) starts its workers with
`local_pool_sandbox()` (`runner.local_check_sandbox`): the **run** profile of the audit hook when
`runner.audit_hook` is on (no reads of other processes' `/proc/<pid>` entries, of other workers'
and test runs' folders below `<tmp>/autoair-runs-<uid>` or of Airflow's secret files; no writes
into Airflow's DAG and plugin folders or AutoAir's output folder), `PR_SET_NO_NEW_PRIVS`,
non-dumpable, and a private `AIRFLOW_HOME` with a copy of `airflow.cfg` without its secrets
(Airflow's DAG and plugin folders are passed on as `AIRFLOW__CORE__*`, so helper modules still
import). Not the check profile's process, network and write denials, no PID namespace and no
private `HOME`: those would change what parses locally. Workers stay warm as before.

## Known limits

- Workers of one pod share a uid. Code that bypasses the audit hook can signal siblings and
  the service process or read a concurrent worker's scratch folder (non-dumpable stops `/proc`
  and ptrace, not signals). Strict separation between authors: `pool_size=1` per pod.
- The HMAC key must be at least 16 bytes (`MIN_KEY_BYTES`). `--no-auth` is refused on a
  non-loopback `--host`.
- The Compose overlay's internal network is shared with the callers; only the Helm
  NetworkPolicy (`egress: []`, on a CNI that enforces it) gives a validator with no egress.

- Only the DagBag import check is isolated. Python DAG import into a spec
  (`importer/python_dag.py`) and test runs (`runner/`) still execute in subprocesses of the main
  server.
- With `max_tasks_per_child > 1` a check's code can leave state (or a thread) behind for the
  next check in the same worker, including writing a fake reply to the pipe. The default of 1
  avoids that; the local pool keeps its previous default of 50 for speed.
- Unprivileged user namespaces are usually blocked in containers (seccomp `RuntimeDefault`,
  AppArmor); `/ready` reports `network_isolated: false` and the NetworkPolicy is what denies
  egress.
