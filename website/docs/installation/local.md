---
title: Local mode
description: Run AutoAir on a workstation with autoair local - no Airflow server, a static operator catalog of the installed packages, and deploys to a local folder or Git.
---

`autoair local` runs the standalone server with a settings preset for a single user on a workstation. It needs no Airflow server: you design, validate, preview and deploy DAG files, then run them in an Airflow of your choice.

## Install and start

`autoair local` needs the `standalone` extra (uvicorn) and something to put in the operator palette. The `airflow` extra installs Apache Airflow and the standard provider, which gives you `BashOperator`, `PythonOperator`, `EmptyOperator`, the standard sensors and the `DagBag` import check.

```bash
# one-off, with uv
uvx --from 'autoair[standalone,airflow]' autoair local --open

# or in a virtual environment
python -m venv .venv && . .venv/bin/activate
pip install 'autoair[standalone,airflow,format]'
autoair local --open
```

Released versions come from PyPI (published by the release workflow on `v*` tags). For an unreleased commit, install the wheel built by `make build` (`pip install 'dist/autoair-<version>-py3-none-any.whl[standalone,airflow]'`) or run from a source checkout.

From a source checkout, `uv sync` installs the development group (which already contains Airflow and uvicorn); build the frontend once:

```bash
uv sync
(cd frontend && npm ci && npm run build)
uv run autoair local --open
```

The command prints where it listens and where it writes:

```text
AutoAir (local mode): http://127.0.0.1:8765/ui/ — DAGs in dags
```

:::warning
`uvx autoair local` without extras does not start: the core package has no HTTP server. The command exits with code 1 and says which extras to install: "`autoair local` needs an HTTP server, which comes with the 'standalone' extra (uvicorn). Install it with: pip install 'autoair[standalone,airflow]'", followed by the `uvx --from 'autoair[standalone,airflow]' autoair local --open` form. Without Airflow or providers installed, the palette shows no operators.
:::

## Options

| Option | Environment variable | Default | Effect |
| --- | --- | --- | --- |
| `--dags-folder PATH` | (sets `AUTOAIR_DAGS_FOLDER`) | `./dags` | Folder the spec and generated files are written to. Local mode writes directly into it (no `autoair/` sub-folder). |
| `--host HOST` | | `127.0.0.1` | Bind address. Keep it on loopback. |
| `--port PORT` | | `8765` | Port. |
| `--token TOKEN` | `AUTOAIR_LOCAL__TOKEN` | unset | Require this access token. The sign-in page shows "This local server requires an access token (autoair local --token)." API clients send `Authorization: Bearer <token>`. |
| `--open` / `--no-open` | `AUTOAIR_LOCAL__OPEN_BROWSER` | off | Open the UI in the default browser once `/api/health` answers. |
| `--config FILE`, `-c FILE` | `AUTOAIR_CONFIG_FILE` | none | Configuration file (YAML, JSON, INI, Python); repeatable. |

Binding anything other than a loopback address requires a token:

```bash
autoair local --host 0.0.0.0 --token "$(openssl rand -hex 16)"
```

Without a token the command exits with code 2: `refusing to serve 0.0.0.0 without --token: local mode has no other authentication`.

## What the preset changes

Local mode applies these settings on top of your configuration:

| Setting | Value in local mode | Consequence |
| --- | --- | --- |
| `mode` | `local` | |
| `dags_folder` / `subfolder` | `--dags-folder` (default `dags`) / empty | Files go straight into the folder; AutoAir state goes into `<dags_folder>/.autoair` |
| `features.runs`, `features.test_runs`, `features.airflow` | `false` | No trigger, backfill, clear, test runs, run overlay, task logs, connection/pool/variable pickers, DAG status, or import from Airflow |
| `catalog_strategy` | `static` | Operators are found by parsing installed packages, nothing is imported |
| `import_check_on_deploy` | `true` only when Airflow is importable | Deploys run the `DagBag` import check if Airflow is installed |
| `dagbag_pool` | `false` | One import check at a time, no warm worker pool |
| `sso.enabled` | `false` | No Airflow sign-in |

Everything else (policy rules, environments, templates, AI, a Git deploy target) is configured as usual. For example, to deploy to a Git repository instead of `./dags`:

```bash
export AUTOAIR_STORAGE__MODE=git
export AUTOAIR_STORAGE__GIT__PROVIDER=local
export AUTOAIR_STORAGE__GIT__REPO=git@github.com:acme/airflow-dags.git
export AUTOAIR_STORAGE__GIT__SUBDIR=dags
export AUTOAIR_STORAGE__GIT__MODE=push
autoair local
```

`provider=local` drives the `git` command-line tool against any remote and pushes the pending branch without opening a pull request. `github`, `gitlab`, `bitbucket` and `azure` use the host's REST API, and `gitea` (Gitea or Forgejo) pushes with the `git` CLI and opens pull requests through the Gitea API; these five open pull requests in `storage.git.mode=pr` (the default) with `AUTOAIR_STORAGE__GIT__TOKEN`. See [Deploy](/docs/user-guide/deploy) and [Git bundles](/docs/deployment/git-bundles).

## Adding operators

The palette lists what is installed next to AutoAir. Add providers to the same environment:

```bash
uvx --from 'autoair[standalone,airflow]' \
  --with apache-airflow-providers-amazon \
  --with apache-airflow-providers-common-sql \
  autoair local
```

Custom operators in a folder are found with `AUTOAIR_CUSTOM_OPERATOR_PATHS='["/path/to/include"]'`, and internal operator packages with `AUTOAIR_EXTRA_OPERATOR_PACKAGES` or `AUTOAIR_EXTRA_OPERATOR_DISTRIBUTIONS`. The catalog is cached on disk (`AUTOAIR_CACHE_DIR`, default `$XDG_CACHE_HOME/autoair`) and rebuilt when the installed packages change. See [Catalog sources](/docs/extending/catalog-sources).

## Running the DAGs

Local mode does not talk to Airflow. To run what you built, point an Airflow at the same folder (for example `AIRFLOW__CORE__DAGS_FOLDER=$PWD/dags airflow standalone`) or push to the repository your Airflow reads. When you need runs, logs and pickers inside AutoAir, switch to the [plugin](/docs/installation/plugin) or [standalone](/docs/installation/standalone) mode; specs written in local mode are read unchanged.

## Security notes

- Without a token, anyone who can reach the port has full rights (there are no users and no roles). Keep the default loopback bind on shared machines.
- Python task bodies and inline functions are imported by the `DagBag` check in a subprocess on your machine. Do not deploy specs from untrusted sources without reviewing them.

## Related

- [Quick start](/docs/getting-started/quick-start)
- [Build your first DAG](/docs/getting-started/first-dag)
- [Extras](/docs/installation/extras)
