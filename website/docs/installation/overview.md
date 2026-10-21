---
title: Installation overview
description: Choose between the Airflow plugin, the standalone server and local mode, and find the installation method that fits your environment.
sidebar_label: Overview
---

AutoAir is one Python package, `autoair`. Its wheel contains the backend, the CLI and the built frontend. What you install around it, and where, depends on the run mode.

## Requirements

| Component | Requirement |
| --- | --- |
| Python | 3.10 or newer (CI runs 3.10 and 3.12) |
| Apache Airflow | 3.0 or newer, below 4. CI matrix: 3.0.6, 3.1.8, 3.2.2, 3.3.2, plus the 3.4.0b2 pre-release (allowed to fail). The `mcp` extra cannot be installed next to Airflow 3.1: see [Airflow compatibility](/docs/installation/compatibility) |
| Airflow REST API | API v2 (`/api/v2/...`), reachable from AutoAir in standalone mode |
| Node.js | 22, only to build the frontend from a source checkout |
| Optional services | PostgreSQL for shared state, Redis for collaboration fan-out, a Git host or object storage as deploy target, an LLM provider for AI |

Features that need a newer Airflow than the one installed (or the one named by `AUTOAIR_TARGET_AIRFLOW_VERSION`) are reported as validation problems rather than failing at parse time.

## Choosing a mode

| | Plugin | Standalone | Local |
| --- | --- | --- | --- |
| Start | Loaded by the Airflow API server through the `airflow.plugins` entry point | `autoair serve` | `autoair local` |
| UI location | Airflow ≥ 3.1: **Browse → DAG Builder** and an **AutoAir** tab on DAG pages; Airflow 3.0: `/autoair/ui/` | `http://<host>:8765/ui/` | `http://127.0.0.1:8765/ui/` |
| API base | `/autoair/api` | `/api` | `/api` |
| Authentication | Airflow's auth manager (the UI session) | Airflow token verified against Airflow: Airflow session cookie, username/password, or pasted token | None on loopback; optional static token (`--token`) |
| Installs into | Airflow's Python environment (API server, and the same packages everywhere DAGs are parsed) | Its own environment or container | Any environment, e.g. `uvx` |
| Operator palette | Providers installed in Airflow's image | Providers installed next to AutoAir (install the same ones as Airflow) | Packages installed next to AutoAir |
| Runs, logs, pickers | Yes | Yes, through Airflow's REST API | No (`features.runs`, `features.test_runs`, `features.airflow` off) |
| Scaling | With the API server replicas | Independent workers/replicas | Single process |
| Upgrade | Rebuild the Airflow image | Independent of Airflow | Reinstall |
| Best for | Teams running Airflow 3 who want the builder inside the Airflow UI | Separate lifecycle, separate scaling, or an Airflow you cannot add packages to | Evaluation, offline authoring |

Rules of thumb:

- If you can change the Airflow image, start with the **plugin**: no second service, no second sign-in, and the operator catalog matches Airflow exactly.
- Choose **standalone** when AutoAir must be upgraded, scaled or secured separately from Airflow, or when Airflow is managed by someone else and you only have API access.
- Use **local** to try AutoAir or to author DAG files without an Airflow server.

All modes read the same settings (`AUTOAIR_*` environment variables or configuration files); see [Configuration](/docs/admin/configuration).

## Installation methods

| Method | Mode | Page |
| --- | --- | --- |
| `uvx` / `pip` on a workstation | Local | [Local mode](/docs/installation/local) |
| `pip install` into the Airflow environment or a derived Airflow image | Plugin | [Airflow plugin](/docs/installation/plugin) |
| `pip install` or a container running `autoair serve` | Standalone | [Standalone server](/docs/installation/standalone) |
| Docker Compose (demo: Airflow + plugin + two standalone replicas + PostgreSQL) | Plugin and standalone | [Docker Compose](/docs/installation/docker-compose) |
| Official Apache Airflow Helm chart with a derived image | Plugin | [Helm](/docs/installation/helm) |

Pick optional features with [extras](/docs/installation/extras). For production topologies (Kubernetes, Git bundles, object storage, a reverse proxy in front of standalone, PostgreSQL, air-gapped installs) see [Deployment](/docs/deployment/overview).

## Getting the package

- **Python package**: `pip install autoair` with the extras you need. The release workflow (`.github/workflows/release.yml`) publishes the wheel and sdist to PyPI as `autoair` when a `v*` tag matching the package version is pushed, and attaches them with `SHA256SUMS` to the GitHub release.
- **Published container image**: the same workflow pushes `ghcr.io/ctolon/autoair` (linux/amd64 and linux/arm64, with SBOM and build provenance), built from `deploy/docker/Dockerfile` on the official `apache/airflow` image. Tags: `<version>`, `<version>-airflow<airflow version>` (currently Airflow 3.3.2), and for final releases `<major>.<minor>` and `latest`. Check `autoair --version` (or `pip show autoair`) to see which version you run.
- **From source**: build the frontend and the wheel with `make build` (runs `npm ci && npm run build` in `frontend/`, then `uv build`), which produces `dist/autoair-<version>-py3-none-any.whl`. A wheel built without the frontend still installs, but `/ui/` answers `503` ("Frontend bundle not built").
- **Building your own image**: `deploy/docker/Dockerfile` installs a prebuilt wheel on top of `apache/airflow` and precomputes the operator catalog; `deploy/docker/Dockerfile.demo` builds the frontend and wheel inside Docker.

## After installing

1. Open the UI and check the version and the target Airflow version in the toolbar.
2. Decide on the deploy target (local folder, object storage or Git) so that the files land where Airflow reads DAGs: [Deploy](/docs/user-guide/deploy).
3. Configure access: [IAM](/docs/admin/iam).
4. For more than one replica, move state to PostgreSQL: [Database](/docs/admin/database), [Scaling](/docs/admin/scaling).
5. Optionally enable AI: [AI providers](/docs/ai/providers).
