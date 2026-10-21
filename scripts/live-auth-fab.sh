#!/usr/bin/env bash
# Airflow 3 with the FAB auth manager + AutoAir plugin, for live RBAC tests
# (tests/live/test_auth_managers.py). Users: admin/admin (Admin), viewer/viewer (Viewer),
# editor/editor (User = can edit DAGs).
set -euo pipefail
cd "$(dirname "$0")/.."
export AIRFLOW_HOME="${AIRFLOW_HOME:-$PWD/.airflow-fab}"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__CORE__AUTH_MANAGER=airflow.providers.fab.auth_manager.fab_auth_manager.FabAuthManager
export AIRFLOW__CORE__DAGS_FOLDER="$AIRFLOW_HOME/dags"
export AIRFLOW__API__PORT="${AIRFLOW_PORT:-8091}"
export AIRFLOW__API__BASE_URL="http://localhost:${AIRFLOW__API__PORT}"
export AIRFLOW__API_AUTH__JWT_SECRET="${AIRFLOW__API_AUTH__JWT_SECRET:-autoair-live-fab-secret}"
mkdir -p "$AIRFLOW__CORE__DAGS_FOLDER"
uv run airflow db migrate >/dev/null
for u in "admin Admin" "viewer Viewer" "editor User"; do
  set -- $u
  uv run airflow users create --username "$1" --password "$1" --role "$2" \
    --firstname "$1" --lastname live --email "$1@example.com" >/dev/null 2>&1 || true
done
exec uv run airflow api-server --port "$AIRFLOW__API__PORT"
