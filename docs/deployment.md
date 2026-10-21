# Deployment

Reference for the run modes, their endpoints and the supported versions. Installation and
operation guides are on the documentation site:

| Task | Guide |
|---|---|
| Install as an Airflow plugin (image, Helm values) | [Airflow plugin](https://ctolon.github.io/autoair/docs/installation/plugin), [Helm](https://ctolon.github.io/autoair/docs/installation/helm), [Helm with the plugin](https://ctolon.github.io/autoair/docs/deployment/helm-plugin) |
| Run the standalone server next to Airflow | [Standalone](https://ctolon.github.io/autoair/docs/installation/standalone), [Behind a reverse proxy](https://ctolon.github.io/autoair/docs/deployment/standalone-proxy) |
| Try AutoAir on a laptop without Airflow | [Local mode](https://ctolon.github.io/autoair/docs/installation/local) |
| Docker Compose demo stack | [Docker Compose](https://ctolon.github.io/autoair/docs/installation/docker-compose) (`deploy/compose/README.md`) |
| Choose a deploy target (folder, object storage, git) | [Deploy](https://ctolon.github.io/autoair/docs/user-guide/deploy), [Git bundles](https://ctolon.github.io/autoair/docs/deployment/git-bundles), [Object storage](https://ctolon.github.io/autoair/docs/deployment/object-storage) |
| Run several replicas | [Scaling](https://ctolon.github.io/autoair/docs/admin/scaling), [PostgreSQL](https://ctolon.github.io/autoair/docs/deployment/postgres), [state-and-scaling.md](architecture/state-and-scaling.md) |
| Air-gapped installation | [Air-gapped](https://ctolon.github.io/autoair/docs/deployment/air-gapped) |

## Run modes and endpoints

| Mode | Start | UI | API | MCP |
|---|---|---|---|---|
| Airflow plugin (entry point `airflow.plugins`) | installed into the Airflow image | Airflow 3.1+: **Browse → DAG Builder** and an **AutoAir** tab on every DAG page; Airflow 3.0: `/autoair/ui/` | `/autoair/api` (OpenAPI `/autoair/api/docs`) | `/autoair/mcp/` |
| Standalone | `autoair serve --dags-folder <dir> --airflow-url <url>` | `/ui/` | `/api` | `/mcp` |
| Local mode | `autoair local` | `/ui/` | `/api` | `/mcp` |
| AI service | `autoair ai-serve` | none | `/api/ai` | `/mcp` |

Health, readiness and Prometheus metrics are `<api>/health`, `<api>/ready` and `<api>/metrics`.

## Compatibility
Python 3.10+ · Airflow 3.0 or later, below 4 (CI matrix: 3.0.6, 3.1.8, 3.2.2, 3.3.2; the 3.4.0b1
pre-release is tested in a separate nox session). Features that need a newer Airflow (Airflow 3.1: deadline alerts, the `all_done_min_one_success`
trigger rule; 3.2: asset partitions; 3.3: retry policies, `rerun_with_latest_version`, `@result`,
`@task.stub`; 3.4: `AssetAndTimeSchedule`, `batch_asset_events`, task loops) are
validated against the installed — or `AUTOAIR_TARGET_AIRFLOW_VERSION` — version.

## Live verification against real services
Contract tests use in-memory fake servers; the `live` suites run against real ones (skipped
unless their environment variables are set):

| Suite | Start | Run |
|---|---|---|
| FAB auth manager RBAC | `./scripts/live-auth-fab.sh` (port 8091) | `AUTOAIR_LIVE_AIRFLOW_URL=http://localhost:8091 pytest tests/live/test_auth_managers.py -m live` |
| Keycloak auth manager RBAC | `./scripts/live-auth-keycloak.sh` (Keycloak in Docker on 8190, Airflow on 8092) | `AUTOAIR_LIVE_AIRFLOW_URL=http://localhost:8092 pytest tests/live/test_auth_managers.py -m live` |
| GitLab deploy target | `./scripts/live-gitlab.sh` (GitLab CE in Docker on 8929; prints the env vars) | `pytest tests/live/test_gitlab.py -m live` |

The RBAC suite checks: anonymous → 401; viewer can read but gets 403 on deploy and the task runner;
editor/admin can deploy; collaboration tickets are refused to viewers (403) and anonymous
callers (401), and the WebSocket only accepts a valid ticket (forged tickets or raw JWTs → 403).
With Keycloak, the client's authorization *decision strategy* must be **Affirmative** (the
bootstrap script sets it) — with the default *Unanimous*, overlapping role permissions deny
every request, including Airflow's own API.
