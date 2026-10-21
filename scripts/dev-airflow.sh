#!/usr/bin/env bash
# Run a local Airflow 3 (standalone) with the AutoAir plugin installed (editable).
set -euo pipefail
cd "$(dirname "$0")/.."
export AIRFLOW_HOME="${AIRFLOW_HOME:-$PWD/.airflow-dev}"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS="${ALL_ADMINS:-True}"
export AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL=5
export AIRFLOW__DAG_PROCESSOR__MIN_FILE_PROCESS_INTERVAL=5
export AIRFLOW__API__PORT="${AIRFLOW_PORT:-8080}"
# Optional: also track a git repository (GitDagBundle) — used to test AutoAir's git mode.
if [ -n "${AUTOAIR_DEV_GIT_REPO:-}" ]; then
  export AIRFLOW__DAG_PROCESSOR__DAG_BUNDLE_CONFIG_LIST="[
    {\"name\": \"dags-folder\", \"classpath\": \"airflow.dag_processing.bundles.local.LocalDagBundle\", \"kwargs\": {}},
    {\"name\": \"git-dags\", \"classpath\": \"airflow.providers.git.bundles.git.GitDagBundle\",
     \"kwargs\": {\"repo_url\": \"${AUTOAIR_DEV_GIT_REPO}\", \"tracking_ref\": \"main\", \"subdir\": \"dags\", \"refresh_interval\": 5}}]"
fi
# AIRFLOW_VENV: run from another virtualenv (e.g. .venv-allproviders with every provider).
if [ -n "${AIRFLOW_VENV:-}" ]; then
  export PATH="${AIRFLOW_VENV}/bin:${PATH}"  # standalone spawns its components as `airflow`
  exec "${AIRFLOW_VENV}/bin/airflow" standalone
fi
exec uv run airflow standalone
