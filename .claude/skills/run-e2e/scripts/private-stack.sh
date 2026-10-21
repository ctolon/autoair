#!/usr/bin/env bash
# A private, throw-away standalone stack for E2E runs and screenshots: `autoair serve` with its
# own state folder plus a production build served by `vite preview`, on ports nobody else uses.
# It never touches the shared demo (:8080 :8765 :5173 :3000) and kills only its own PIDs.
#
#   .claude/skills/run-e2e/scripts/private-stack.sh start [--auth] [--airflow-url URL]
#   .claude/skills/run-e2e/scripts/private-stack.sh status
#   .claude/skills/run-e2e/scripts/private-stack.sh stop
#
# start prints `export` lines (AUTOAIR_STANDALONE_URL, AUTOAIR_DEV_URL, ...) to eval.
# Default is `--no-auth` with the Airflow URL pointed at a closed port (hermetic, like the visual
# suite). --auth verifies bearer tokens against --airflow-url (default http://localhost:8080,
# read-only use of the shared Airflow: token checks and REST reads).
# Env: E2E_PORT_BASE (default: a free base in 18000-18890), E2E_STATE (default: mktemp -d),
#      E2E_SKIP_BUILD=1 to reuse the last build in $E2E_STATE/dist.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
RUN_FILE="${E2E_RUN_FILE:-${TMPDIR:-/tmp}/autoair-private-stack-$(id -u)-$(basename "$ROOT").env}"
SHARED_PORTS=" 8080 8765 5173 3000 8793 8794 "

port_free() { ! ss -Hltn "sport = :$1" 2>/dev/null | grep -q .; }

cmd="${1:-}"; shift || true
case "$cmd" in
start)
  if [ -f "$RUN_FILE" ]; then echo "already running (see $RUN_FILE); run stop first" >&2; exit 1; fi
  auth=0; airflow="http://localhost:8080"
  while [ $# -gt 0 ]; do
    case "$1" in
      --auth) auth=1 ;;
      --airflow-url) airflow="$2"; shift ;;
      *) echo "unknown option $1" >&2; exit 2 ;;
    esac
    shift
  done
  base="${E2E_PORT_BASE:-}"
  if [ -z "$base" ]; then
    for _ in $(seq 1 50); do
      cand=$((18000 + (RANDOM % 89) * 10))
      if port_free "$cand" && port_free $((cand + 1)); then base=$cand; break; fi
    done
  fi
  [ -n "$base" ] || { echo "no free port base found" >&2; exit 1; }
  api=$base; ui=$((base + 1))
  for p in $api $ui; do
    case "$SHARED_PORTS" in *" $p "*) echo "port $p is a shared demo port" >&2; exit 1 ;; esac
    port_free "$p" || { echo "port $p is in use" >&2; exit 1; }
  done
  state="${E2E_STATE:-$(mktemp -d "${TMPDIR:-/tmp}/autoair-e2e-XXXXXX")}"
  mkdir -p "$state/dags" "$state/home"
  if [ "${E2E_SKIP_BUILD:-0}" != 1 ] || [ ! -f "$state/dist/index.html" ]; then
    (cd "$ROOT/frontend" && npx vite build --mode standalone --outDir "$state/dist" --emptyOutDir \
      --logLevel warn) >&2
  fi
  serve_args=(serve --host 127.0.0.1 --port "$api" --dags-folder "$state/dags")
  if [ "$auth" = 1 ]; then
    serve_args+=(--airflow-url "$airflow")
  else
    serve_args+=(--no-auth --airflow-url http://127.0.0.1:9)
  fi
  # setsid makes each server the leader of its own process group; the leader writes its PID
  # (= the group id) before exec'ing, so stop can kill the whole tree and nothing else.
  (cd "$state" && AIRFLOW_HOME="$state/home" AUTOAIR_DAGBAG_POOL=false \
    AUTOAIR_DISCOVER_AIRFLOW_FOLDERS=false \
    setsid bash -c 'echo $$ > "$0"; exec "$@"' "$state/serve.pid" \
    uv run --project "$ROOT" autoair "${serve_args[@]}" > "$state/serve.log" 2>&1 < /dev/null &)
  (cd "$ROOT/frontend" && AUTOAIR_API_PORT="$api" \
    setsid bash -c 'echo $$ > "$0"; exec "$@"' "$state/preview.pid" \
    npx vite preview --mode standalone --outDir "$state/dist" --host 127.0.0.1 --port "$ui" \
    --strictPort > "$state/preview.log" 2>&1 < /dev/null &)
  for _ in $(seq 1 120); do
    if curl -sf -o /dev/null "http://127.0.0.1:$api/api/health" && \
       curl -sf -o /dev/null "http://127.0.0.1:$ui/"; then
      {
        echo "export E2E_STATE='$state'"
        echo "export AUTOAIR_STANDALONE_URL='http://127.0.0.1:$api'"
        echo "export AUTOAIR_DEV_URL='http://127.0.0.1:$ui'"
        echo "export AUTOAIR_E2E_API_BASE='http://127.0.0.1:$api/api'"
      } > "$RUN_FILE"
      cat "$RUN_FILE"
      echo "# UI http://127.0.0.1:$ui/ (vite preview) and http://127.0.0.1:$api/ui/ ; logs in $state" >&2
      exit 0
    fi
    for f in serve preview; do
      if [ -f "$state/$f.pid" ] && ! kill -0 "$(cat "$state/$f.pid")" 2>/dev/null; then
        echo "$f exited:" >&2; tail -5 "$state/$f.log" >&2; break 2
      fi
    done
    sleep 1
  done
  echo "stack did not become healthy; logs in $state" >&2
  "$0" stop --state "$state" || true
  exit 1
  ;;
status)
  [ -f "$RUN_FILE" ] && cat "$RUN_FILE" || echo "not running"
  ;;
stop)
  state=""
  if [ "${1:-}" = "--state" ]; then state="$2"; fi
  if [ -z "$state" ] && [ -f "$RUN_FILE" ]; then
    state="$(sed -n "s/^export E2E_STATE='\(.*\)'$/\1/p" "$RUN_FILE")"
  fi
  [ -n "$state" ] || { echo "not running"; exit 0; }
  for f in "$state/preview.pid" "$state/serve.pid"; do
    [ -f "$f" ] || continue
    pid="$(cat "$f")"
    # The PID is the process group of the server's whole tree (see start).
    kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 -- "-$pid" 2>/dev/null || break; sleep 0.5; done
    kill -9 -- "-$pid" 2>/dev/null || true
    rm -f "$f"
  done
  rm -f "$RUN_FILE"
  echo "stopped (state kept in $state; remove it when done)"
  ;;
*)
  sed -n '2,16p' "$0"; exit 2 ;;
esac
