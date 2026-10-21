---
title: Airflow compatibility
description: Which AutoAir extras can be installed next to each supported Airflow version, the dependency pins behind each gap, and how to run the MCP server when it cannot share Airflow's environment.
sidebar_label: Compatibility
---

AutoAir supports Apache Airflow 3.0 or newer, below 4, on Python 3.10 to 3.14. In plugin mode AutoAir runs inside Airflow's Python environment, so every extra has to resolve against the versions Airflow pins. Most extras install everywhere; two gaps come from Airflow itself.

## Extras per Airflow version

| Extra | 3.0 | 3.1 | 3.2 | 3.3 | 3.4 (pre-release) |
| --- | --- | --- | --- | --- | --- |
| `all` (see [Extras](/docs/installation/extras#all)) | yes | yes | yes | yes | yes |
| `mcp` | yes | **no** | yes | yes | yes |
| `sqlite` | yes (SQLAlchemy 1.4) | yes | yes | yes | yes |
| `postgres`, `kb-pgvector` | yes (SQLAlchemy 1.4, psycopg2) | yes | yes | yes | yes |
| `ai`, `ai-*` | yes | yes | yes | yes | yes |
| `otel`, `collab`, `collab-redis`, `standalone`, `format`, `shell`, `yaml`, `git` | yes | yes | yes | yes | yes |

CI installs exactly these combinations: the Airflow 3.1 job runs without `mcp` and its MCP tests are skipped with the reason; every other job installs `all,mcp,sqlite,postgres,kb-pgvector`. The versions tested are 3.0.6, 3.1.8, 3.2.2, 3.3.2 and 3.4.0b1.

## The pins behind it

What `apache-airflow-core` requires, and what a fresh resolver picks today:

| Airflow | SQLAlchemy | FastAPI | Starlette (resolved) | Pydantic |
| --- | --- | --- | --- | --- |
| 3.0.6 | `>=1.4.49,<2.0` (1.4.54) | `>=0.115` | 1.x | `>=2.11` |
| 3.1.8 | `>=1.4.54` (2.x) | `>=0.116,<0.118` | `<0.49` (0.48) | `>=2.11` |
| 3.2.2 | `>=2.0.48` | `>=0.129` | `>=1.0.1` | `>=2.11` |
| 3.3.2 | `>=2.0.50` | `>=0.129,<0.137` | `>=1.0.1` | `>=2.11` |
| 3.4.0b1 | `>=2.0.50,!=2.1.0` | `>=0.129,<0.137` | `>=1.3.1` | `>=2.11` |

- **MCP on Airflow 3.1.** Every FastMCP 4 release requires Starlette 1.0.1 or newer. Airflow 3.1 caps FastAPI below 0.118, and those FastAPI releases cap Starlette below 0.49, so `autoair[mcp]` cannot be resolved next to Airflow 3.1. Older FastMCP lines are not an option: FastMCP 3 and 2 are built on the `mcp` SDK 1.x and `httpx` (AutoAir uses `mcp` 2.x and `httpx2`), and the newest FastMCP 3 releases need Starlette 1.0.1 as well.
- **SQLAlchemy on Airflow 3.0.** Airflow 3.0 keeps SQLAlchemy below 2.0. AutoAir's SQL state backends and the pgvector store use only APIs that SQLAlchemy 1.4 and 2.x share, so the `sqlite`, `postgres` and `kb-pgvector` extras accept SQLAlchemy 1.4.49 or newer and use whatever Airflow has installed.

## MCP next to Airflow 3.1

Without `autoair[mcp]` the plugin keeps working; only MCP is off:

- `GET /autoair/api/meta` reports `mcp_enabled: false` and `mcp_unavailable_reason`, which names the Starlette conflict;
- the admin panel's **Features** tab shows `mcp` as not installed with the same reason, and it cannot be switched on;
- `/autoair/mcp/` is not mounted.

To give agents MCP anyway, run the MCP server from a separate environment or container that does not contain Airflow 3.1:

- **Standalone MCP server.** Install `autoair[standalone,mcp]` (plus the providers your DAGs use, or point `AUTOAIR_CATALOG_FILE` at a catalog built with `autoair catalog build` in the Airflow image) and run `autoair mcp-serve --http --host 0.0.0.0 --port 8766` with `AUTOAIR_AIRFLOW_URL` set to the Airflow API server. Clients authenticate with their Airflow token. See [MCP](/docs/ai/mcp).
- **Through the AI service.** Run [`autoair ai-serve`](/docs/deployment/ai-service) in its own environment with `autoair[standalone,ai,mcp]` and set `AUTOAIR_AI__SERVICE__PROXY_MCP=true` on the plugin: `/autoair/mcp/` is then proxied to the service, the plugin needs no FastMCP, and clients keep the plugin URL.

Either way the MCP tools validate and generate code for the Airflow installed in the MCP server's own environment (the newest version AutoAir knows when none is installed), not for the 3.1 API server. A DAG that uses a feature newer than 3.1 is therefore not flagged by MCP validation; the plugin's validation flags it when the DAG is validated or deployed there, and Airflow 3.1 reports an import error for generated code that uses a symbol it lacks.

## PostgreSQL on Airflow 3.0

SQLAlchemy 1.4 has no psycopg 3 dialect, so on Airflow 3.0 AutoAir drives PostgreSQL through psycopg2. A `postgresql://`, `postgres://` or `postgresql+psycopg://` URL in `database.url`, `kb.pgvector.url` or a `kb.pgvector.conn_id` connection is switched to `postgresql+psycopg2://` automatically. psycopg2 comes with `apache-airflow-providers-postgres`, which an Airflow running on a PostgreSQL metadata database already has; otherwise install `psycopg2-binary`. Without a driver the error reads *cannot load the postgresql driver (...): SQLAlchemy 1.4 (Airflow 3.0) drives PostgreSQL through psycopg2: pip install psycopg2-binary (or apache-airflow-providers-postgres)*.

On SQLAlchemy 2 (Airflow 3.1 and newer, and standalone installs) PostgreSQL uses psycopg 3 as before.

## Airflow constraints files

Airflow's [constraints files](https://airflow.apache.org/docs/apache-airflow/stable/installation/installing-from-pypi.html) pin every package to the versions an Airflow release was tested with. They are meant for installing Airflow and do not leave room for AutoAir: the 3.0 to 3.2 files pin packages below AutoAir's core requirements (for example `idna` below what `httpx2` needs), the 3.0 and 3.1 files pin OpenTelemetry 1.27 below the `otel` extra, the 3.2 file pins `pydantic-ai-slim` 1.x while `ai` needs 2.43 or newer, and the 3.3 file pins an `openai` SDK older than `ai-openai` needs. Install Airflow with its constraints file if you use one, then install AutoAir in a second `pip install` without `--constraint`, keeping `apache-airflow==<version>` on that command line so pip does not change Airflow.
