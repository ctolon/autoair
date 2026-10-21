#!/usr/bin/env bash
# Entrypoint of the standalone AutoAir replicas: picks up the signed example marketplace
# written by the marketplace-seed job, then runs the given command (default: autoair serve).
set -euo pipefail
SOURCES=/opt/airflow/marketplace/sources.json
if [[ -z "${AUTOAIR_MARKETPLACE_SOURCES:-}" && -s "$SOURCES" ]]; then
  AUTOAIR_MARKETPLACE_SOURCES="$(cat "$SOURCES")"
  export AUTOAIR_MARKETPLACE_SOURCES
fi
exec "$@"
