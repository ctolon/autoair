---
title: Observability
description: Logging, the health and readiness endpoints, Prometheus metrics, OpenTelemetry tracing, and what to alert on when running AutoAir in production.
---

AutoAir exposes four kinds of signals: logs, health and readiness endpoints, Prometheus metrics and, optionally, OpenTelemetry traces. The admin panel adds a live view of replicas, backends and caches ([Admin panel](/docs/admin/admin-panel)).

Paths below are for the standalone server (`/api/...`). In plugin mode the API is mounted inside Airflow's API server at `/autoair/api/...` (for example `/autoair/api/health`).

## Logging

| Setting | Default | Description |
|---|---|---|
| `AUTOAIR_LOGGING__LEVEL` | `INFO` | Root log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `AUTOAIR_LOGGING__FORMAT` | `text` | `text`: human-readable lines; `json`: one JSON object per line |
| `AUTOAIR_LOGGING__LEVELS` | `{}` | Per-logger levels, for example `{"autoair.storage": "DEBUG", "httpx2": "WARNING"}` |
| `AUTOAIR_SERVER__ACCESS_LOG` | `true` | `autoair serve`: log every request (uvicorn access log) |

* **Standalone and local mode** (`autoair serve`, `autoair local`) configure the process's logging: one handler on standard error, the chosen format, and uvicorn's error and access loggers routed through the same format.
* **Plugin mode**: Airflow owns handlers and formats. Only `logging.levels` is applied; set the rest in Airflow's logging configuration.

Text format:

```text
2026-10-19 08:41:12,512 INFO     autoair.audit: audit {"timestamp": "...", "action": "deploy", ...}
```

JSON format (for log shippers): `timestamp` (UTC ISO 8601), `level`, `logger`, `message`, `exception` (when there is one) and any extra fields of the record:

```json
{"timestamp": "2026-10-19T08:41:12.512000+00:00", "level": "WARNING", "logger": "autoair.api.app", "message": "ignoring unusable deploy target from /opt/airflow/dags/autoair/.autoair/deploy-target.json"}
```

Useful loggers: `autoair` (everything), `autoair.audit` (every audit record while the `log` audit sink is on, which is the default; see [Audit](/docs/admin/audit#audit-sinks)), `autoair.storage` (deploy targets and git), `autoair.iam` (authorization), `uvicorn.access` (requests), `httpx2` (outgoing HTTP calls; noisy at `DEBUG`). Secrets are never logged: passwords and tokens are `SecretStr`, and URL credentials are masked in error messages.

## Health and readiness

| Endpoint | Auth | Answers |
|---|---|---|
| `GET /api/health` | none | Always `200` while the process serves requests: `{"status": "ok", "version": "<AutoAir version>"}` |
| `GET /api/ready` | none | `200` with `{"ready": true, "checks": {...}}` when every check passes, `503` otherwise |

`/api/ready` checks:

| Check | When | Passes when |
|---|---|---|
| `catalog` | always | The operator catalog is loaded (`ok (N operators)`) |
| `storage` | `server.ready_check_storage=true` (default) | The deploy target is reachable and writable (`ok (local)`, `ok (object)` or `ok (git)`); `read-only` fails the check |
| `airflow` | `server.ready_check_airflow=true` (default `false`); never in local mode | Airflow's `/api/v2/monitor/health` answers `200` |

Each check may take at most `server.ready_timeout` seconds (5). Example of a failing response:

```json
{"ready": false, "checks": {"catalog": "ok (1874 operators)", "storage": "error: 401 Unauthorized"}}
```

### Degraded startup

AutoAir never refuses to start because its deploy target cannot be built. When building the target fails at startup (an Airflow connection named by `storage.git.conn_id` or `storage.git.forced_conn_id` that does not exist yet, an unreadable GitDagBundle configuration, an unreachable object store), the server starts degraded:

| Where | What you see |
|---|---|
| Log | `WARNING deploy target (storage.mode=git) unavailable, starting degraded: <reason>` |
| `GET /api/health` | `200`, unchanged: the process is alive |
| `GET /api/ready` | `503`, with `"storage": "error: <reason>"` (only when `server.ready_check_storage=true`, the default) |
| Deploy-target panel and admin panel | The target as `(unavailable)`, not writable, with the error |
| Editor | Loads normally; the DAG list from the target is empty |
| Deploy, delete, reading a stored version | Fail with `the <mode> deploy target is unavailable: <reason> (AutoAir retries building it on the next deploy)` |

AutoAir retries building the target on every deploy, and on reads (including each `/api/ready` call) at most every 10 seconds. Once the cause is fixed, the next retry succeeds, the log shows `deploy target (storage.mode=git) is available now`, and `/api/ready` returns `200` without a restart. Team targets (`team_storage`) behave the same way and are rebuilt on the next write for that team.

A deploy target chosen in the UI that can no longer be built is ignored at startup (`ignoring unusable deploy target from ...`) and the target from the settings is used instead.

Kubernetes probes:

```yaml
livenessProbe:
  httpGet: {path: /api/health, port: 8765}
  periodSeconds: 10
readinessProbe:
  httpGet: {path: /api/ready, port: 8765}
  periodSeconds: 15
  timeoutSeconds: 10
startupProbe:            # the first catalog build can take minutes without AUTOAIR_CATALOG_FILE
  httpGet: {path: /api/health, port: 8765}
  failureThreshold: 30
  periodSeconds: 10
```

Keep `ready_check_airflow` off unless AutoAir is useless without Airflow: an Airflow outage would otherwise take every AutoAir replica out of the load balancer. Use `scheme: HTTPS` in the probes when AutoAir serves TLS itself.

## Prometheus metrics

`GET /api/metrics` serves Prometheus text format. It needs no authentication; restrict it at the network level (an internal port, a `NetworkPolicy`, or a proxy rule). Switch it off with `AUTOAIR_SERVER__METRICS_ENABLED=false` (it then answers `404`).

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `autoair_requests_total` | counter | `method`, `route` (route template, for example `/api/specs/{dag_id}`; `unmatched` for unknown paths), `status` | API requests |
| `autoair_request_seconds` | histogram | `route` | API request latency; buckets 5 ms to 10 s |
| `autoair_deploys_total` | counter | `result` | Deploy attempts: `ok`, `proposed` (stored for approval), `invalid` (validation failed), `conflict` (someone else saved first), `ejected` (the generated file was edited by hand), `error` (the write failed) |

The registry is AutoAir's own, so the endpoint contains only these metrics (no process or Python runtime metrics, and in plugin mode none of Airflow's).

:::warning Several workers in one container
Counters live in each process. With `autoair serve --workers N`, a scrape reaches one worker and sees only its share. For accurate metrics, run one worker per container and scale with replicas, or sum over many scrapes and treat the values as samples.
:::

Scrape configuration:

```yaml
scrape_configs:
  - job_name: autoair
    metrics_path: /api/metrics
    kubernetes_sd_configs: [{role: pod}]
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_label_app]
        regex: autoair
        action: keep
```

## OpenTelemetry tracing

```bash
pip install 'autoair[otel]'
AUTOAIR_OTEL_ENABLED=true
OTEL_SERVICE_NAME=autoair                         # default "autoair"
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
```

* The API is instrumented with the OpenTelemetry FastAPI instrumentation: one span per HTTP request.
* AutoAir adds its own spans: `validate` (attributes `autoair.dag_id`, `autoair.import_check`) and `render` (`autoair.dag_id`, `autoair.output_mode`).
* Spans are exported over OTLP/HTTP when `OTEL_EXPORTER_OTLP_ENDPOINT` (or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`) is set; the standard `OTEL_*` variables configure the exporter. Without an endpoint, spans are created but not exported.
* An already-configured global tracer provider is reused instead of replaced, so inside Airflow AutoAir's spans join Airflow's traces.
* `AUTOAIR_SERVER__OTEL_EXCLUDED_URLS` (default `["health", "ready", "metrics"]`) keeps probe and scrape traffic out of the traces.
* With the `otel` audit sink (`AUTOAIR_AUDIT__SINKS`), every audit record is also emitted as a span named `audit <action>`; see [Audit](/docs/admin/audit#audit-sinks).
* If the extra is not installed, AutoAir logs "AUTOAIR_OTEL_ENABLED is set but opentelemetry is not installed" and runs without tracing.

## What to alert on

| Signal | Condition (example) | Likely cause |
|---|---|---|
| Readiness | `/api/ready` failing for more than 5 minutes on any replica | Deploy target unreachable (git token expired, bucket permissions, full disk), catalog not loaded |
| Server errors | `sum(rate(autoair_requests_total{status=~"5.."}[5m])) / sum(rate(autoair_requests_total[5m])) > 0.02` | Airflow or git host failing, database unreachable, bugs |
| Deploy failures | `increase(autoair_deploys_total{result="error"}[15m]) > 0` | Write to the deploy target failed: credentials, branch protection, storage |
| Validation-blocked deploys | sustained `autoair_deploys_total{result="invalid"}` | A policy change blocks teams; usually informational |
| Edit conflicts | rising `autoair_deploys_total{result="conflict"}` | Concurrent editors without co-editing, or a stuck automation |
| Latency | `histogram_quantile(0.95, sum by (le, route) (rate(autoair_request_seconds_bucket[5m]))) > 5` on non-AI routes | Slow Airflow API, slow git host, catalog rebuilds |
| Throttling | rising `autoair_requests_total{status="429"}` | Rate limits too low for real use, or a misbehaving client |
| Denials | many `permission_denied` audit records for one user | Misconfigured role mapping, or probing |
| Replicas | a replica shown as stale in the admin panel **Replicas** list | Crash loop or a hung process (SQL backends) |
| Database | `autoair db current` reports pending migrations after a rollout; database connection errors in logs | Migration job did not run; database down |
| Audit storage | log lines `failed to write audit record` | Audit store unreachable or full: the trail has gaps |
| AI | errors in the admin panel's **AI usage** tab; AI cost growth | Provider outage or quota, runaway agents |

Example Prometheus rules:

```yaml
groups:
  - name: autoair
    rules:
      - alert: AutoAirDeployErrors
        expr: increase(autoair_deploys_total{result="error"}[15m]) > 0
        labels: {severity: warning}
        annotations: {summary: "AutoAir deploys are failing"}
      - alert: AutoAir5xx
        expr: |
          sum(rate(autoair_requests_total{status=~"5.."}[5m]))
            / sum(rate(autoair_requests_total[5m])) > 0.02
        for: 10m
        labels: {severity: warning}
      - alert: AutoAirNotReady
        expr: kube_pod_status_ready{condition="true", pod=~"autoair-.*"} == 0
        for: 5m
        labels: {severity: critical}
```

## Related pages

* [Audit](/docs/admin/audit): the audit trail and AI audit log
* [Admin panel](/docs/admin/admin-panel): replicas, backends, caches
* [TLS and HTTP headers](/docs/admin/tls-and-headers): probes over HTTPS
* [Configuration reference](/docs/reference/configuration#logging)
