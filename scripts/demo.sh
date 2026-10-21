#!/usr/bin/env bash
# Demo: Airflow 3 with every provider (.venv-allproviders) + the AutoAir plugin on :8080,
# standalone AutoAir (git mode, signed example marketplace) on :8765. Ctrl-C stops both.
#
#   uv venv -p 3.12 .venv-allproviders   # once; see docs/deployment.md "Demo"
#   ./scripts/demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
VENV="${AIRFLOW_VENV:-$ROOT/.venv-allproviders}"
export AIRFLOW_HOME="${AIRFLOW_HOME:-$ROOT/.airflow-dev}"
export AIRFLOW__API_AUTH__JWT_EXPIRATION_TIME="${AIRFLOW__API_AUTH__JWT_EXPIRATION_TIME:-604800}"
export AIRFLOW__CORE__TEST_CONNECTION="${AIRFLOW__CORE__TEST_CONNECTION:-Enabled}"
GIT_REPO="$AIRFLOW_HOME/git/dags.git"
MARKET="$AIRFLOW_HOME/marketplace"

[ -x "$VENV/bin/airflow" ] || { echo "missing $VENV (see docs/deployment.md)"; exit 1; }
[ -d "$GIT_REPO" ] || git init -q --bare -b main "$GIT_REPO"
if [ ! -f "$MARKET/index/index.json" ]; then
  mkdir -p "$MARKET" && cp -r examples/marketplace "$MARKET/index"
  "$VENV/bin/autoair" marketplace keygen --out "$MARKET/market.pem" > "$MARKET/market.pub"
  "$VENV/bin/autoair" marketplace build "$MARKET/index" --name examples --key "$MARKET/market.pem"
fi

AIRFLOW_VENV="$VENV" AUTOAIR_DEV_GIT_REPO="file://$GIT_REPO" ./scripts/dev-airflow.sh &
AIRFLOW_PID=$!
trap 'kill $AIRFLOW_PID 2>/dev/null; kill $(jobs -p) 2>/dev/null' EXIT INT TERM
until curl -sf -o /dev/null http://localhost:8080/api/v2/version; do sleep 2; done

AUTOAIR_MARKETPLACE_SOURCES="[{\"name\":\"examples\",\"url\":\"file://$MARKET/index/index.json\",\"public_key\":\"$(cat "$MARKET/market.pub")\"}]" \
AUTOAIR_STORAGE__MODE=git AUTOAIR_STORAGE__GIT__PROVIDER=local \
AUTOAIR_STORAGE__GIT__REPO="file://$GIT_REPO" AUTOAIR_STORAGE__GIT__BASE_BRANCH=main \
AUTOAIR_STORAGE__GIT__SUBDIR=dags AUTOAIR_STORAGE__GIT__MODE=push \
AUTOAIR_EXPOSE_CONFIG=true AUTOAIR_IAM__LOGIN_ENABLED=true \
AUTOAIR_STORAGE__GIT__CACHE_DIR="$AIRFLOW_HOME/gitcache" \
  "$VENV/bin/autoair" serve --port 8765 --airflow-url http://localhost:8080 &

TOKEN=$(curl -s http://localhost:8080/auth/token | sed -E 's/.*"access_token":"([^"]+)".*/\1/')
echo "$TOKEN" > "$AIRFLOW_HOME/demo-token.txt"
cat <<MSG

  Airflow + AutoAir plugin : http://localhost:8080   (no login: all users are admins)
                             Browse → DAG Builder, or http://localhost:8080/plugin/dag-builder
  Standalone AutoAir       : http://localhost:8765/ui/
  Token (paste once)       : $AIRFLOW_HOME/demo-token.txt   (valid 7 days)

MSG
wait
