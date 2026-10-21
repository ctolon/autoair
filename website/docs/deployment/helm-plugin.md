---
title: Plugin with the Airflow Helm chart
description: Build an Airflow image with AutoAir and deploy it as a plugin with the official Apache Airflow Helm chart using deploy/helm/values-autoair.yaml.
sidebar_label: Helm chart (plugin)
---

AutoAir runs **inside Airflow's API server**, deployed with the official Apache Airflow Helm chart (1.22 or later) and two files from the repository:

- `deploy/docker/Dockerfile`: the official `apache/airflow` image plus the AutoAir wheel and a precomputed operator catalog.
- `deploy/helm/values-autoair.yaml`: chart values for a git-based setup (pull-request mode, `GitDagBundle`) with stateless API servers.

For a first Helm installation without these production concerns, see [Installation with Helm](/docs/installation/helm). For the overall layout, see [Kubernetes reference architecture](/docs/deployment/kubernetes).

## Building the image

1. Build the frontend bundles and the wheel from the repository root:

   ```bash
   make build          # npm ci + npm run build in frontend/, then uv build -> dist/autoair-*.whl
   ```

   The wheel contains the compiled UI (`autoair/static/plugin` and `autoair/static/standalone`). Building only the Python package without the frontend produces a server with no UI.

2. Build the image:

   ```bash
   docker build -f deploy/docker/Dockerfile -t registry.example.com/my-airflow-autoair:3.3.2 .
   docker push registry.example.com/my-airflow-autoair:3.3.2
   ```

The Dockerfile does three things:

```dockerfile
ARG AIRFLOW_VERSION=3.3.2
FROM apache/airflow:${AIRFLOW_VERSION}

COPY dist/autoair-*.whl /tmp/
RUN pip install --no-cache-dir "$(ls /tmp/autoair-*.whl)[git,mcp,ai,ai-anthropic,format]" \
        apache-airflow-providers-git apache-airflow-providers-common-messaging \
    && rm /tmp/autoair-*.whl

RUN autoair catalog build --output /opt/airflow/autoair-catalog.json
ENV AUTOAIR_CATALOG_FILE=/opt/airflow/autoair-catalog.json \
    AUTOAIR_CACHE_DIR=/tmp/autoair-cache
```

- **Installs AutoAir with extras.** `git` (an empty, stable install name: `provider=local` uses the `git` CLI of the image), `mcp` (the MCP endpoint), `ai` plus a provider extra such as `ai-anthropic` (the pydantic-ai runtime and the provider SDK; without them AI is off and the admin panel says which extra to install), `format` (ruff, used to format generated files).
- **Installs providers.** `apache-airflow-providers-git` supplies `GitDagBundle`. Every provider your DAGs use must be installed here too: the same image runs the DAG processor and workers, and the installed providers are what fill AutoAir's operator palette.
- **Precomputes the operator catalog.** `autoair catalog build` parses the installed providers statically (nothing is imported) and writes a JSON file; `AUTOAIR_CATALOG_FILE` makes every API-server replica start warm with an identical palette. The catalog's cache key includes the installed distributions and a fingerprint of AutoAir's catalog code, so an upgraded image never serves a stale catalog.

Add extras for the features you enable:

| Feature | Extra | Why |
|---|---|---|
| PostgreSQL state (`database.backend=postgresql`) | `postgres` | SQLAlchemy + psycopg 3 |
| Live collaboration (`features.collab`, default on) | `collab` | `pycrdt`, `pycrdt-websocket`; without it the `/collab` WebSocket endpoint is not mounted |
| Redis collaboration fan-out | `collab-redis` | `redis` client |
| ShellCheck diagnostics in shell editors | `shell` | ships the `shellcheck` binary |
| OpenTelemetry tracing | `otel` | instrumentation and OTLP exporter |
| AI with another provider | `ai-openai`, `ai-google`, `ai-ollama`, `ai-bedrock`, `ai-groq`, `ai-mistral`, `ai-cohere` or `ai-all` | the provider SDK through `pydantic-ai-slim` |
| IDE language servers | `lsp` or `lsp-pyright` | `features.ide_language_server` |

Object-storage URLs (`s3://`, `gs://`) for state or the deploy target need the matching fsspec implementation (`s3fs`, `gcsfs`), which AutoAir does not pull in by itself.

A production line therefore often looks like:

```dockerfile
RUN pip install --no-cache-dir "$(ls /tmp/autoair-*.whl)[git,mcp,ai,ai-anthropic,format,postgres,collab,shell]" \
        apache-airflow-providers-git s3fs \
        apache-airflow-providers-amazon apache-airflow-providers-snowflake
```

Pin versions with a constraints file in real builds, exactly as you would for Airflow itself.

## Chart values

`deploy/helm/values-autoair.yaml` (abridged; the file in the repository has comments for every entry):

```yaml
images:
  airflow:
    repository: registry.example.com/my-airflow-autoair
    tag: "3.3.2"

apiServer:
  replicas: 2

config:
  dag_processor:
    dag_bundle_config_list: >-
      [{"name": "dags", "classpath": "airflow.providers.git.bundles.git.GitDagBundle",
        "kwargs": {"git_conn_id": "git_dags", "tracking_ref": "main", "subdir": "dags",
                   "refresh_interval": 60}}]

env:
  - { name: AUTOAIR_STORAGE__MODE, value: "git" }
  - { name: AUTOAIR_STORAGE__GIT__PROVIDER, value: "github" }
  - { name: AUTOAIR_STORAGE__GIT__REPO, value: "acme/airflow-dags" }
  - { name: AUTOAIR_STORAGE__GIT__BASE_BRANCH, value: "main" }
  - { name: AUTOAIR_STORAGE__GIT__SUBDIR, value: "dags/autoair" }
  - { name: AUTOAIR_STORAGE__GIT__MODE, value: "pr" }
  - { name: AUTOAIR_STORAGE__GIT__USE_AIRFLOW_BUNDLE, value: "true" }
  - { name: AUTOAIR_TEMPLATES_URL, value: "s3://acme-airflow/autoair/templates" }
  - { name: AUTOAIR_AUDIT_URL, value: "s3://acme-airflow/autoair/audit" }
  - { name: AUTOAIR_REQUIRE_OWNER, value: "true" }
  - { name: AUTOAIR_MIN_RETRIES, value: "1" }
  - { name: AUTOAIR_ENVIRONMENTS, value: '["dev","prod"]' }
  - { name: AUTOAIR_DEFAULT_ENVIRONMENT, value: "prod" }
  - { name: AUTOAIR_IAM__STRATEGIES, value: '["airflow_permissions","role_mapping","custom_view"]' }
  - { name: AUTOAIR_IAM__DIRECT_PUSH_ROLES, value: '["Admin"]' }
  - { name: AUTOAIR_IAM__REQUIRE_APPROVAL, value: "true" }
  - { name: AUTOAIR_FEATURES__MARKETPLACE, value: "false" }
  - { name: AUTOAIR_AI__PROVIDER, value: "anthropic" }
  - { name: AUTOAIR_AI__MODEL, value: "claude-sonnet-5-5" }
  - { name: AUTOAIR_AI__LLM_MODE, value: "central" }
  - { name: AUTOAIR_EXPOSE_CONFIG, value: "non-sensitive-only" }

secret:
  - envName: AUTOAIR_STORAGE__GIT__TOKEN
    secretName: autoair-git
    secretKey: token
  - envName: AUTOAIR_AI__API_KEY
    secretName: autoair-ai
    secretKey: api-key
```

The chart passes `env` and `secret` entries to every Airflow component. AutoAir only reads them where it is loaded (the API server), so this is harmless, but it does mean secrets such as the git token reach scheduler and worker pods too. If that matters, move AutoAir's variables to `apiServer.env`, which chart 1.22 adds to the API server container only. Its entries take either `value` or `valueFrom.secretKeyRef` (or `configMapKeyRef`):

```yaml
apiServer:
  env:
    - { name: AUTOAIR_STORAGE__MODE, value: "git" }
    - name: AUTOAIR_STORAGE__GIT__TOKEN
      valueFrom:
        secretKeyRef: { name: autoair-git, key: token }
```

Install or upgrade:

```bash
kubectl create secret generic autoair-git --from-literal=token="$GITHUB_TOKEN"
kubectl create secret generic autoair-ai --from-literal=api-key="$ANTHROPIC_API_KEY"
helm upgrade --install airflow apache-airflow/airflow -f deploy/helm/values-autoair.yaml
```

### What each block does

| Block | Effect |
|---|---|
| `dag_bundle_config_list` | Airflow's DAG processor reads DAGs from the `dags` folder of the repository behind the `git_dags` connection, at `main`, refreshed every 60 s |
| `AUTOAIR_STORAGE__GIT__USE_AIRFLOW_BUNDLE=true` | AutoAir fills repository, branch, folder and credentials from that `GitDagBundle` (`storage.git.bundle_name` selects one when there are several; default: the first) |
| `AUTOAIR_STORAGE__GIT__SUBDIR=dags/autoair` | AutoAir writes into a sub-folder of the bundle's `dags` folder, so hand-written DAGs and AutoAir DAGs never collide |
| `AUTOAIR_STORAGE__GIT__MODE=pr` | Each DAG change opens or updates a pull request; it goes live when merged |
| `AUTOAIR_TEMPLATES_URL`, `AUTOAIR_AUDIT_URL` | Templates and the audit trail on object storage, so they survive pod restarts |
| `AUTOAIR_IAM__STRATEGIES`, `AUTOAIR_IAM__DIRECT_PUSH_ROLES`, `AUTOAIR_IAM__REQUIRE_APPROVAL` | AutoAir roles from Airflow permissions, Airflow roles and custom views, direct push for Admins, four-eyes approvals; see [IAM](/docs/admin/iam) |
| `AUTOAIR_FEATURES__MARKETPLACE=false` | Example of a feature switch; see [Feature flags](/docs/admin/feature-flags) |
| `AUTOAIR_AI__PROVIDER`, `AUTOAIR_AI__MODEL`, `AUTOAIR_AI__LLM_MODE=central` (key in the `AUTOAIR_AI__API_KEY` secret) | Central model; see [AI providers](/docs/ai/providers) |
| `AUTOAIR_EXPOSE_CONFIG=non-sensitive-only` | Admins can read the effective configuration without secrets |

## Make the API servers stateless

The shipped values move templates and the audit trail to object storage, but the rest of AutoAir's file-backed state (reviews, deploy proposals, the deploy target chosen in the UI, custom icons, AI settings, the collaboration key file) still defaults to `<dags_folder>/autoair/.autoair/` inside each pod. With a `GitDagBundle` that folder is not shared and not persistent. For two or more API-server replicas, add PostgreSQL:

```yaml
env:
  - { name: AUTOAIR_DATABASE__BACKEND, value: "postgresql" }
  - { name: AUTOAIR_DATABASE__URL, value: "postgresql+psycopg://autoair@autoair-db:5432/autoair" }
  - { name: AUTOAIR_DATABASE__SSL_MODE, value: "verify-full" }
  - { name: AUTOAIR_DATABASE__MIGRATE_ON_START, value: "false" }
secret:
  - { envName: AUTOAIR_DATABASE__PASSWORD, secretName: autoair-db, secretKey: password }
  - { envName: AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY, secretName: autoair-ai, secretKey: fernet-key }
```

Then run the migration once per release, before the API servers roll, for example as a Job using the same image:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: autoair-db-upgrade
spec:
  backoffLimit: 2
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: registry.example.com/my-airflow-autoair:3.3.2
          command: ["autoair", "db", "upgrade"]
          env:
            - { name: AUTOAIR_DATABASE__BACKEND, value: postgresql }
            - { name: AUTOAIR_DATABASE__URL, value: "postgresql+psycopg://autoair@autoair-db:5432/autoair" }
            - name: AUTOAIR_DATABASE__PASSWORD
              valueFrom: { secretKeyRef: { name: autoair-db, key: password } }
```

Details are on [PostgreSQL for AutoAir state](/docs/deployment/postgres).

In plugin mode the collaboration ticket key is derived from Airflow's `[api_auth] jwt_secret`, which all API-server replicas already share. Live rooms are shared through PostgreSQL (`collab.sync=database`) once the database backend is configured.

## Verify

1. `kubectl port-forward svc/airflow-api-server 8080` and open Airflow. **Browse → DAG Builder** opens AutoAir (the navigation entry is `plugin.nav_name` in the `plugin.nav_category` menu; Airflow 3.0 shows the full page at `/autoair/ui/`).
2. `curl -s localhost:8080/autoair/api/ready` returns `{"ready": true, "checks": {"catalog": "ok (<n> operators)", "storage": "ok (git)"}}`.
3. In AutoAir, **Versions & Git → Deploy target…** shows the bundle as the current target ("from the DAG bundle").
4. Build a two-task DAG, deploy it and confirm a pull request appears in the repository. After merging, the DAG processor picks the DAG up on the next bundle refresh.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No **DAG Builder** entry in Airflow | Plugin not installed in the API-server image, or the wheel was built without the UI | Check `airflow plugins` in the API-server pod; rebuild with `make build` |
| Palette differs between replicas | Different images or no `AUTOAIR_CATALOG_FILE` | Use one image tag for every replica |
| Deploy succeeds but nothing appears in Airflow | PR not merged yet (PR mode), or AutoAir writes to a folder the bundle does not read | Merge the PR; check that `storage.git.subdir` lies inside the bundle's `subdir` |
| Templates or reviews disappear after a restart | File-backed state in the pod's ephemeral DAG folder | Configure PostgreSQL (above) or object-storage URLs |
| `s3://` URLs fail with "protocol not known" | `s3fs` not installed | Add `s3fs` (or `gcsfs`) to the image |

## Related pages

- [Git bundles and git-sync](/docs/deployment/git-bundles)
- [Kubernetes reference architecture](/docs/deployment/kubernetes)
- [Separate AI service](/docs/deployment/ai-service)
- [Configuration reference](/docs/reference/configuration)
