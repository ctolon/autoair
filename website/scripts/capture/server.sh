#!/usr/bin/env bash
# Starts a private AutoAir for screenshot/video capture, built from the working tree:
#
#   - a scripted OpenAI-compatible LLM (scripts/e2e_fake_llm.py)     :8869
#   - `autoair serve` (git target in PR mode on a scratch bare repo)  :8860
#   - the standalone UI built into scripts/capture/.state/ui, served by
#     `vite preview` with /api and /collab proxied to :8860           :8861
#     (a preview build has no injected config: pages must set
#      window.__AUTOAIR_CONFIG__ = {apiBase: "/api"} before load, run.mjs does)
#   - password sign-in (iam.login_enabled) and the signed example marketplace are on.
#
# It needs an Airflow 3 API server on $AIRFLOW_URL (default http://localhost:8080) whose
# `GET /auth/token` hands out a token (SimpleAuthManager with all admins, as in
# scripts/dev-airflow.sh). Nothing outside scripts/capture/.state is written.
# Stop with Ctrl-C (or kill the PID printed at start); only this script's children stop.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
STATE="$HERE/.state"
AIRFLOW_URL="${AIRFLOW_URL:-http://localhost:8080}"
API_PORT="${CAPTURE_API_PORT:-8860}"
UI_PORT="${CAPTURE_UI_PORT:-8861}"
LLM_PORT="${CAPTURE_LLM_PORT:-8869}"
VENV="${AIRFLOW_VENV:-$ROOT/.venv-allproviders}"
[ -x "$VENV/bin/autoair" ] || VENV="$ROOT/.venv"

mkdir -p "$STATE"
REPO="$STATE/dags.git"
if [ ! -d "$REPO" ]; then
  # A bare repo with main (dev) and production (prod) branches.
  git init -q --bare -b main "$REPO"
  SEED="$STATE/seed-clone"; rm -rf "$SEED"; git clone -q "$REPO" "$SEED" 2>/dev/null
  mkdir -p "$SEED/dags" && printf '# DAGs\n\nManaged by AutoAir.\n' > "$SEED/README.md" && touch "$SEED/dags/.keep"
  git -C "$SEED" -c user.name="AutoAir docs" -c user.email=docs@example.com add -A
  git -C "$SEED" -c user.name="AutoAir docs" -c user.email=docs@example.com commit -qm "Initial commit"
  git -C "$SEED" push -q origin main && git -C "$SEED" push -q origin main:production
  rm -rf "$SEED"
fi
# Signed example marketplace (as in scripts/demo.sh), built once.
MARKET="$STATE/marketplace"
if [ ! -f "$MARKET/index/index.json" ]; then
  mkdir -p "$MARKET" && cp -r "$ROOT/examples/marketplace" "$MARKET/index"
  "$VENV/bin/autoair" marketplace keygen --out "$MARKET/market.pem" > "$MARKET/market.pub"
  "$VENV/bin/autoair" marketplace build "$MARKET/index" --name examples --key "$MARKET/market.pem" >/dev/null
fi

if [ "${CAPTURE_SKIP_BUILD:-0}" != 1 ] || [ ! -f "$STATE/ui/index.html" ]; then
  (cd "$ROOT/frontend" && ./node_modules/.bin/vite build --mode standalone --outDir "$STATE/ui" --emptyOutDir >/dev/null)
fi

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

"$VENV/bin/python" "$ROOT/scripts/e2e_fake_llm.py" --port "$LLM_PORT" >"$STATE/llm.log" 2>&1 &
pids+=($!)

env \
  AUTOAIR_STORAGE__MODE=git \
  AUTOAIR_STORAGE__GIT__PROVIDER=local \
  AUTOAIR_STORAGE__GIT__REPO="file://$REPO" \
  AUTOAIR_STORAGE__GIT__BASE_BRANCH=main \
  AUTOAIR_STORAGE__GIT__SUBDIR=dags \
  AUTOAIR_STORAGE__GIT__MODE="${CAPTURE_GIT_MODE:-pr}" \
  AUTOAIR_STORAGE__GIT__ENVIRONMENTS='{"dev":"main","prod":"production"}' \
  AUTOAIR_STORAGE__GIT__CACHE_DIR="$STATE/gitcache" \
  AUTOAIR_STORAGE__GIT__COMMITTER_NAME="AutoAir docs" \
  AUTOAIR_STORAGE__GIT__COMMITTER_EMAIL="docs@example.com" \
  AUTOAIR_DAGS_FOLDER="$STATE/dags" \
  AUTOAIR_CACHE_DIR="$STATE/cache" \
  AUTOAIR_ENVIRONMENTS='["dev","prod"]' \
  AUTOAIR_DEFAULT_ENVIRONMENT=dev \
  AUTOAIR_EXPOSE_CONFIG=non-sensitive-only \
  AUTOAIR_IAM__LOGIN_ENABLED=true \
  AUTOAIR_MARKETPLACE_SOURCES="[{\"name\":\"examples\",\"url\":\"file://$MARKET/index/index.json\",\"public_key\":\"$(cat "$MARKET/market.pub")\"}]" \
  AUTOAIR_SERVER__PUBLIC_URL="http://localhost:$UI_PORT" \
  AUTOAIR_AI__PROVIDER=openai \
  AUTOAIR_AI__BASE_URL="http://127.0.0.1:$LLM_PORT/v1" \
  AUTOAIR_AI__API_KEY=fake \
  AUTOAIR_AI__MODEL=fake-gpt \
  AUTOAIR_AI__USER_LLM__ALLOW_PRIVATE_NETWORKS=true \
  "$VENV/bin/autoair" serve --port "$API_PORT" --airflow-url "$AIRFLOW_URL" \
    --dags-folder "$STATE/dags" >"$STATE/autoair.log" 2>&1 &
pids+=($!)

(cd "$ROOT/frontend" && AUTOAIR_API_PORT="$API_PORT" exec ./node_modules/.bin/vite preview --mode standalone \
  --outDir "$STATE/ui" --port "$UI_PORT" --strictPort --host 127.0.0.1) >"$STATE/vite.log" 2>&1 &
pids+=($!)

until curl -sf -o /dev/null "http://127.0.0.1:$API_PORT/api/health"; do sleep 1; done
until curl -sf -o /dev/null "http://127.0.0.1:$UI_PORT/"; do sleep 1; done
echo "capture server ready: UI http://localhost:$UI_PORT/  API http://127.0.0.1:$API_PORT/api (pid $$)"
wait
