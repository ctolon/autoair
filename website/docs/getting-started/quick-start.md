---
title: Quick start
description: Run AutoAir in local mode, inside an existing Airflow 3, as a Docker Compose demo, or as a standalone server, with the exact prerequisites for each.
---

Pick the path that matches what you have. Local mode needs no Airflow server; the other three connect to Airflow 3.

| You have | Path | Time |
| --- | --- | --- |
| A laptop with Python | [Local mode](#local-mode-no-airflow-server) | minutes |
| An Airflow 3 installation you can add packages to | [Plugin](#plugin-in-an-existing-airflow) | one restart of the API server |
| Docker with Compose v2 | [Compose demo](#docker-compose-demo) | first build takes several minutes |
| An Airflow 3 reachable over HTTP and an Airflow login | [Standalone server](#standalone-server-next-to-airflow) | minutes |

## Prerequisites

- **Python 3.10 to 3.14** for the `autoair` package (CI runs 3.10 to 3.14 against Airflow 3.3.2).
- **Apache Airflow 3.0 or later, below 4,** for the plugin, standalone and Compose paths. CI tests 3.0.6, 3.1.8, 3.2.2 and 3.3.2, plus a 3.4 pre-release.
- **Node.js 22** only when you build the frontend from a source checkout. Released wheels contain the built UI.
- [uv](https://docs.astral.sh/uv/) is used in the examples; `pip` and `pipx` work the same way.

## Local mode (no Airflow server)

`autoair local` starts the builder on `http://127.0.0.1:8765/ui/` with a settings preset for a workstation: the operator catalog comes from the Python packages installed next to AutoAir, validation is static (plus the `DagBag` import check when Airflow is installed), deploys write to `./dags`, and everything that needs an Airflow server (runs, test runs, connection/pool/variable pickers) is switched off. Details: [Local mode](/docs/installation/local).

### From a source checkout

This works today and is what the project's own development uses:

```bash
git clone https://github.com/ctolon/autoair.git
cd autoair
uv sync                                   # includes apache-airflow and uvicorn (dev group)
(cd frontend && npm ci && npm run build)  # writes the UI into src/autoair/static/
uv run autoair local --open --dags-folder ./dags
```

If you skip the frontend build, `/ui/` answers `503` with "Frontend bundle not built. Run `npm run build` in `frontend/`."

### From a published package

`autoair local` needs two things the core package does not install:

1. **uvicorn**, the HTTP server, which comes with the `standalone` extra. Without it the command stops with `ModuleNotFoundError: No module named 'uvicorn'`.
2. **Operators to put in the palette.** The catalog is built from installed packages. With no Airflow and no providers installed, the palette has no operators (only the built-in Python task, task group and note nodes). The `airflow` extra installs `apache-airflow` and `apache-airflow-providers-standard` (its operators and sensors, among them `BashOperator`, `PythonOperator` and `EmptyOperator`) and enables the `DagBag` import check.

So the command to use is:

```bash
uvx --from 'autoair[standalone,airflow]' autoair local --open
```

Add the providers your DAGs use with `--with`:

```bash
uvx --from 'autoair[standalone,airflow]' --with apache-airflow-providers-amazon autoair local --open
```

The package is published to PyPI as `autoair` by the release workflow on every `v*` tag. To try an unreleased commit, run from a source checkout instead (see [Local mode](/docs/installation/local)).

:::note
The plain `uvx autoair local` shown in some older notes does not start a server: `uvicorn` is only in the `standalone` extra. The command then exits with a message naming the extras to install (`uvx --from 'autoair[standalone,airflow]' autoair local --open`). Use the command above.
:::

To require a token (for example when you bind another address), pass `--token` or set `AUTOAIR_LOCAL__TOKEN`; the sign-in page then shows an **Access token** field. Binding a non-loopback address without a token is refused:

```text
refusing to serve 0.0.0.0 without --token: local mode has no other authentication
```

## Plugin in an existing Airflow

AutoAir registers itself through the `airflow.plugins` entry point, so installing the package into the environment of the Airflow **API server** is enough. Install it wherever the API server runs (for the official image, in a derived image), then restart the API server.

```bash
pip install 'autoair[format,git,mcp]'   # or the wheel: pip install dist/autoair-*.whl
airflow api-server                      # restart it
```

- Airflow 3.1 and newer: open **Browse → DAG Builder**. Every DAG page also gets an **AutoAir** tab.
- Airflow 3.0: open `http://<airflow>/autoair/ui/`.

The DAG files are written to Airflow's `[core] dags_folder` under the `autoair/` sub-folder by default. For anything beyond a single machine (several API servers, Git or object storage bundles), read [Plugin installation](/docs/installation/plugin) and [Deployment](/docs/deployment/overview).

## Docker Compose demo

The repository contains a self-contained demo in `deploy/compose`: PostgreSQL, Airflow 3.3.2 split into its components (API server with the AutoAir plugin, scheduler with the LocalExecutor, dag-processor, triggerer), and two standalone AutoAir replicas sharing state in PostgreSQL. It builds the frontend and the wheel from the working tree, so no local Node or Python is needed.

```bash
git clone https://github.com/ctolon/autoair.git
cd autoair/deploy/compose
cp .env.example .env    # optional: every value has a default
docker compose -f docker-compose.demo.yml up --build -d --wait
```

The first build takes a few minutes; later starts take about a minute. When `--wait` returns:

| Service | URL |
| --- | --- |
| Airflow, then **Browse → DAG Builder** (the plugin) | `http://localhost:8080` |
| AutoAir replica A | `http://localhost:8765/ui/` |
| AutoAir replica B | `http://localhost:8766/ui/` |
| AutoAir API docs (replica A) | `http://localhost:8765/api/docs` |

Two demo DAGs (`demo_hello`, `demo_pipeline`) are deployed on first start, and a signed template marketplace is available in the template gallery. Opt-in profiles add a load balancer (`lb`), Redis (`redis`), the separate AI service (`ai`), a local LLM (`ollama`), a Gitea deploy target (`git`), TLS (`tls`) and tracing (`observability`). The default start runs no AI provider.

The demo makes every Airflow visitor an administrator (SimpleAuthManager `all_admins`) and uses public default secrets; it is for a workstation only. See [Docker Compose](/docs/installation/docker-compose) for the profiles, configuration and the workflows to try.

## Standalone server next to Airflow

`autoair serve` runs AutoAir as its own service. Every request carries an Airflow token, which AutoAir verifies against Airflow and forwards, so Airflow's permissions apply.

```bash
pip install 'autoair[standalone,format]'
autoair serve \
  --airflow-url http://airflow.internal:8080 \
  --dags-folder /shared/dags
```

Open `http://127.0.0.1:8765/ui/`. The sign-in page offers, depending on the setup:

- **Sign in with Airflow**: reuses the Airflow UI session when the browser can send Airflow's cookie to AutoAir (same host, or a shared parent domain).
- **Username and password**: exchanged once for an Airflow token through Airflow's `/auth/token` endpoint (`iam.login_enabled`, on by default).
- **Access token**: paste an Airflow API token. To obtain one:

  ```bash
  curl -s -X POST http://airflow.internal:8080/auth/token \
    -H 'Content-Type: application/json' \
    -d '{"username": "admin", "password": "..."}'
  ```

`--dags-folder` must be the folder Airflow's dag-processor reads (a shared volume), or configure a Git or object-storage deploy target instead. `autoair serve` binds `127.0.0.1:8765` by default; use `--host 0.0.0.0` behind a reverse proxy. Production settings (TLS, proxy headers, several workers): [Standalone installation](/docs/installation/standalone).

## Next steps

- [Build your first DAG](/docs/getting-started/first-dag)
- [Concepts](/docs/getting-started/concepts)
- [Troubleshooting FAQ](/docs/troubleshooting/faq)
