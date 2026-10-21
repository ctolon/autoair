---
title: Airflow plugin
description: Install AutoAir into an Airflow 3 environment as a plugin, how it integrates with the Airflow 3.0 and 3.1+ UI, the URLs it serves, and how to configure and verify it.
sidebar_label: Airflow plugin
---

In plugin mode AutoAir runs inside the Airflow **API server**. Users open it from the Airflow UI with their Airflow session; there is no second service and no second sign-in.

## How the plugin is loaded

The package declares an Airflow plugin entry point:

```toml
[project.entry-points."airflow.plugins"]
autoair = "autoair.plugin:AutoAirPlugin"
```

Airflow discovers it as soon as `autoair` is installed in the Python environment of the API server. No file has to be copied into `plugins/`. The plugin contributes:

- `fastapi_apps`: the AutoAir application mounted under `/autoair` (API, UI assets, MCP, collaboration WebSocket);
- `react_apps` (Airflow 3.1 and newer only): the navigation entry and the DAG page tab.

The plugin never takes the API server down. If the configuration is invalid it logs `AutoAir: invalid configuration` with the reason and registers nothing; if the application fails to build it logs `AutoAir plugin failed to initialise`. In both cases Airflow keeps running without AutoAir.

## Install

Install AutoAir in the same environment as Airflow, together with the providers your DAGs use. The providers also feed the operator palette.

### Into a derived image

This is the recommended way and what `deploy/docker/Dockerfile` does:

```dockerfile
ARG AIRFLOW_VERSION=3.3.2
FROM apache/airflow:${AIRFLOW_VERSION}

# Providers your DAGs use go here as well (they also feed AutoAir's operator palette).
COPY dist/autoair-*.whl /tmp/
RUN pip install --no-cache-dir "$(ls /tmp/autoair-*.whl)[git,mcp,ai,ai-anthropic,format]" \
        apache-airflow-providers-git apache-airflow-providers-common-messaging \
    && rm /tmp/autoair-*.whl

# Precompute the operator catalog for the providers baked into this image, so every
# api-server replica starts warm and serves an identical palette.
RUN autoair catalog build --output /opt/airflow/autoair-catalog.json
ENV AUTOAIR_CATALOG_FILE=/opt/airflow/autoair-catalog.json \
    AUTOAIR_CACHE_DIR=/tmp/autoair-cache
```

On Airflow 3.1 leave `mcp` out of the extras: it cannot be installed next to Airflow 3.1 ([Airflow compatibility](/docs/installation/compatibility)).

Build the wheel first (`make build` produces `dist/autoair-*.whl` with the frontend included), then:

```bash
docker build -f deploy/docker/Dockerfile --build-arg AIRFLOW_VERSION=3.3.2 \
  -t registry.example.com/my-airflow-autoair:3.3.2 .
```

Use this image for every Airflow component so that the API server, dag-processor and workers have the same providers. For Kubernetes, continue with [Helm](/docs/installation/helm).

### Into an existing virtual environment

```bash
pip install 'autoair[format,git,mcp]' "apache-airflow==$(airflow version)"
airflow plugins            # the list now contains "autoair"
```

Restart the API server (`airflow api-server`, or `airflow standalone` in development). Pinning `apache-airflow` to the installed version prevents pip from upgrading Airflow while resolving AutoAir's requirements.

Do not install the `airflow` extra (Airflow is already there) or `standalone` (the API server serves AutoAir). Choose the others with [Extras](/docs/installation/extras).

### Which Airflow components need AutoAir

| Component | Needs `autoair` installed | Why |
| --- | --- | --- |
| API server | Yes | Runs the plugin |
| dag-processor, workers, triggerer | Only for DAGs in **loader** output mode | A loader stub does `from autoair.loader import load_dag`. The default **Python** output imports only Airflow and providers. |
| Scheduler | No | |

Using one image everywhere avoids the question.

## UI integration

### Airflow 3.1 and newer

Airflow 3.1 added `react_apps` to plugins. AutoAir uses it to render its UI natively inside Airflow's:

| Entry | Default | Setting |
| --- | --- | --- |
| Navigation entry | **DAG Builder** under **Browse**, at `/plugin/dag-builder` | `plugin.nav_name` (`AUTOAIR_PLUGIN__NAV_NAME`), `plugin.nav_category` (`AUTOAIR_PLUGIN__NAV_CATEGORY`: `browse`, `admin`, `docs` or `user`; any other value puts the entry in Airflow's top-level navigation) |
| DAG page tab | **AutoAir** on every DAG page; opens that DAG, or offers to import it when it was not built with AutoAir | `plugin.dag_tab_name` (`AUTOAIR_PLUGIN__DAG_TAB_NAME`), `plugin.dag_tab` (`AUTOAIR_PLUGIN__DAG_TAB=false` hides it), `plugin.dag_tab_tags` (Airflow 3.4 and newer: only on DAGs with one of these tags) |

On Airflow 3.4 and newer the tab can be limited to the DAGs you manage with AutoAir. New DAGs created in the builder carry the tag `autoair` (**DAG settings → Tags**); give imported or older DAGs the same tag and set:

```bash
AUTOAIR_PLUGIN__DAG_TAB_TAGS='["autoair"]'
```

Airflow's UI then offers the tab only on DAGs that carry at least one of the tags. Other DAGs no longer show it, so they cannot be imported from their DAG page; open **DAG Builder** and import them from there. The scoping only hides the tab: the builder route stays reachable, and AutoAir's own permission checks still apply. Before Airflow 3.4 the setting is ignored (the API server logs that) and the tab is on every DAG page.

The bundle is served from `/autoair/static/main.js`. The full-page UI at `/autoair/ui/` is also available.

### Airflow 3.0

Airflow 3.0 has no `react_apps`, so there is no menu entry or DAG tab. Open the builder as a full page at:

```text
https://airflow.example.com/autoair/ui/
```

Everything else (API, validation, deploy, runs) behaves the same.

## URLs

All paths are relative to Airflow's base URL.

| Path | Purpose | Authentication |
| --- | --- | --- |
| `/autoair/ui/` | Full-page UI | Airflow session |
| `/autoair/api/...` | REST API used by the UI (reference: [REST API](/api)) | Airflow session or bearer token |
| `/autoair/api/health` | Liveness | None |
| `/autoair/api/ready` | Readiness (checks the deploy target; Airflow too with `server.ready_check_airflow`) | None |
| `/autoair/api/metrics` | Prometheus metrics (`server.metrics_enabled`) | None |
| `/autoair/mcp/` | MCP endpoint (streamable HTTP), with the `mcp` extra and `features.mcp` | Airflow token |
| `/autoair/collab/<dag_id>` | Collaboration WebSocket, with the `collab` extra and `features.collab` | Short-lived ticket |

The health, ready and metrics routes are unauthenticated so that probes and Prometheus can reach them; restrict them at the network level if needed.

## Authentication and permissions

In plugin mode Airflow's auth manager authenticates every request; AutoAir reads the user from the Airflow session and calls Airflow's REST API with the caller's token, so per-DAG permissions are Airflow's. AutoAir's own roles (Viewer, Author, Reviewer, Deployer, CodeDeployer, Operator, TemplateManager, Admin) are derived according to `iam.strategies`:

- `airflow_permissions` (default): from the user's Airflow DAG and configuration permissions.
- `role_mapping`: from Airflow role names, for example an Airflow role `AutoAir Deployer`.
- `custom_view`: from AutoAir-specific permissions created in the auth manager.

With the FAB auth manager (`apache-airflow-providers-fab`), the `custom_view` permissions must exist in FAB's database. Run once, where Airflow runs:

```bash
autoair iam sync-fab --create-roles
```

This creates the AutoAir permissions and, with `--create-roles`, roles named `AutoAir <Role>` (prefix `iam.role_prefix`) holding them. With Keycloak, the client's authorization decision strategy must be **Affirmative**. Details for each auth manager: [IAM](/docs/admin/iam).

## Configuration

The plugin reads the same settings as every other mode:

- environment variables `AUTOAIR_*` on the API server (in Helm, the chart's `env` and `secret` lists);
- configuration files named by `AUTOAIR_CONFIG_FILE`, or by `config_file` in an `[autoair]` section of `airflow.cfg` (`AIRFLOW__AUTOAIR__CONFIG_FILE`).

Defaults that come from Airflow in plugin mode:

| Setting | Default in plugin mode |
| --- | --- |
| `dags_folder` | Airflow's `[core] dags_folder` |
| `subfolder` | `autoair`, so files are written to `<dags_folder>/autoair/` |
| `airflow_url` | Airflow's `[api] base_url` |
| `collab_secret` | derived from Airflow's `[api_auth] jwt_secret` |

A minimal production configuration that deploys through Git instead of the local folder:

```bash
AUTOAIR_STORAGE__MODE=git
AUTOAIR_STORAGE__GIT__USE_AIRFLOW_BUNDLE=true   # repo, branch, subdir, credentials from the GitDagBundle
AUTOAIR_STORAGE__GIT__SUBDIR=dags/autoair
AUTOAIR_STORAGE__GIT__MODE=pr
AUTOAIR_EXPOSE_CONFIG=non-sensitive-only
```

All options: [Configuration](/docs/admin/configuration) and the [configuration reference](/docs/reference/configuration).

## Several API server replicas

Each API server replica runs its own copy of AutoAir. Replicas need shared state for templates, audit, proposals and the deploy target chosen in the UI, and the DAG files must reach every dag-processor. Use a Git or object-storage deploy target and PostgreSQL state (`AUTOAIR_DATABASE__BACKEND=postgresql`), and run `autoair db upgrade` once before the replicas start. See [Scaling](/docs/admin/scaling), [Database](/docs/admin/database) and the [Kubernetes deployment](/docs/deployment/kubernetes).

## Verify the installation

1. `airflow plugins` lists `autoair`.
2. `curl -s https://airflow.example.com/autoair/api/health` answers `200`.
3. In the Airflow UI (3.1+), **Browse** contains **DAG Builder**; on 3.0, `/autoair/ui/` loads the builder.
4. The palette lists the providers installed in the image. If it is empty or stale, see [Troubleshooting](/docs/troubleshooting/faq).
