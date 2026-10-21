#!/usr/bin/env bash
# Airflow 3 with the Keycloak auth manager + AutoAir plugin, for live RBAC tests
# (tests/live/test_auth_managers.py). Starts Keycloak in Docker (container autoair-keycloak,
# port 8190), bootstraps realm "airflow" and runs Airflow's api-server on port 8092.
set -euo pipefail
cd "$(dirname "$0")/.."
KC_PORT="${KC_PORT:-8190}"
KC_URL="http://localhost:${KC_PORT}"
if ! docker ps --format '{{.Names}}' | grep -qx autoair-keycloak; then
  docker rm -f autoair-keycloak >/dev/null 2>&1 || true
  docker run -d --name autoair-keycloak -p "${KC_PORT}:8080" \
    -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
    quay.io/keycloak/keycloak:26.3 start-dev >/dev/null
fi
for _ in $(seq 1 90); do curl -sf "${KC_URL}/realms/master" >/dev/null && break; sleep 2; done

export AIRFLOW_HOME="${AIRFLOW_HOME:-$PWD/.airflow-keycloak}"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__CORE__AUTH_MANAGER=airflow.providers.keycloak.auth_manager.keycloak_auth_manager.KeycloakAuthManager
export AIRFLOW__CORE__DAGS_FOLDER="$AIRFLOW_HOME/dags"
export AIRFLOW__API__PORT="${AIRFLOW_PORT:-8092}"
export AIRFLOW__API__BASE_URL="http://localhost:${AIRFLOW__API__PORT}"
export AIRFLOW__API_AUTH__JWT_SECRET="${AIRFLOW__API_AUTH__JWT_SECRET:-autoair-live-keycloak-secret}"
export AIRFLOW__KEYCLOAK_AUTH_MANAGER__SERVER_URL="$KC_URL"
export AIRFLOW__KEYCLOAK_AUTH_MANAGER__REALM=airflow
export AIRFLOW__KEYCLOAK_AUTH_MANAGER__CLIENT_ID=airflow
AIRFLOW__KEYCLOAK_AUTH_MANAGER__CLIENT_SECRET="$(uv run python scripts/keycloak_bootstrap.py "$KC_URL" airflow)"
export AIRFLOW__KEYCLOAK_AUTH_MANAGER__CLIENT_SECRET
mkdir -p "$AIRFLOW__CORE__DAGS_FOLDER"
uv run airflow db migrate >/dev/null
uv run airflow keycloak-auth-manager create-all --username admin --password admin \
  --user-realm master --client-id admin-cli >/dev/null
exec uv run airflow api-server --port "$AIRFLOW__API__PORT"
