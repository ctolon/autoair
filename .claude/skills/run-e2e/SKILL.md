---
name: run-e2e
description: Run AutoAir's Playwright end-to-end specs or the UI crawler without disturbing the shared demo - private ports, a production build served by vite preview, a private state folder, and cleanup of only your own processes. Use for "run e2e", "crawl", Playwright specs under frontend/e2e, or any browser test.
---

# Run E2E tests safely

The shared demo (Airflow :8080, AutoAir :8765, Vite :5173, docs :3000, Airflow log servers
:8793/:8794) belongs to everyone in this session. Never restart it, never kill its PIDs, never
deploy test DAGs to it.

## Suites

| Suite | Config | Servers |
|---|---|---|
| Specs (`frontend/e2e/*.spec.ts`) | `playwright.config.ts`, base URL `$AIRFLOW_URL` (default :8080) | none started; plugin specs need an Airflow with the plugin |
| Crawler `e2e/crawl.spec.ts` (~25 min, `--workers 6`) | same | targets plugin (`$AIRFLOW_URL`), standalone (`$AUTOAIR_STANDALONE_URL`), dev (`$AUTOAIR_DEV_URL`) |
| Visual `e2e/visual/` | `playwright.visual.config.ts` via `npm run test:visual` (`e2e/visual/run.mjs`) | starts its own `autoair serve --no-auth` + `vite preview` on free ports, in `.artifacts/visual/<run>/` |

The crawler **deploys** `crawl_<spec>_<target>_<suffix>` DAGs through each target's API and
deletes them again in `afterAll` (best effort; leftovers are logged as "DAGs left behind").
Every spec that deploys does the same through `e2e/cleanup.ts` (`DeployedDags`: `track()`
right before the deploy, `cleanup(request)` in `afterAll`); use it in new specs. Still point
every target at private servers: a killed run cleans up nothing.

## Standalone specs and the crawler on a private stack

```bash
eval "$("${CLAUDE_SKILL_DIR}"/scripts/private-stack.sh start)"   # prints the exports it sets
cd frontend
npx playwright test e2e/<name>.spec.ts --reporter=list
CRAWL_DEBUG=1 npx playwright test e2e/crawl.spec.ts -g standalone --workers 2
"${CLAUDE_SKILL_DIR}"/scripts/private-stack.sh stop
```

`private-stack.sh` picks a free port pair in 18000-18899 (or `E2E_PORT_BASE`), builds the
current tree with `vite build --mode standalone` into a private folder, starts `autoair serve`
(no auth, Airflow unreachable; `--auth --airflow-url URL` to verify real tokens) and
`vite preview`, and records process-group IDs so `stop` kills only those. `E2E_SKIP_BUILD=1`
reuses the last build.

## Specs that need Airflow (plugin mode)

Use a private Airflow, not the shared one:

```bash
AIRFLOW_HOME="$(mktemp -d)" AIRFLOW_PORT=18080 ./scripts/dev-airflow.sh > /tmp/af.log 2>&1 &
echo $! > /tmp/af.pid      # kill only this PID (and its children) when done
cd frontend && AIRFLOW_URL=http://localhost:18080 npx playwright test e2e/builder.spec.ts
```

Airflow standalone also binds its log-server ports; if they clash, set
`AIRFLOW__LOGGING__WORKER_LOG_SERVER_PORT` and `AIRFLOW__LOGGING__TRIGGER_LOG_SERVER_PORT`.
A token for scripts: `curl -s http://localhost:18080/auth/token` (simple auth manager, all
admins). Reading from the shared Airflow (`GET :8080/auth/token`, REST reads) is fine; writes
are not.

## Pitfalls

- Never `pkill -f vite|autoair|airflow`, never kill by port: you would hit other sessions.
- `npm run test:visual` runs are isolated: each builds into and serves from its own
  `.artifacts/visual/<pid>-<random>/` (`AUTOAIR_VISUAL_DIR` overrides it) on free ports
  (`AUTOAIR_VISUAL_UI_PORT` / `AUTOAIR_VISUAL_API_PORT` override them), so concurrent runs
  are fine. A passing run removes its folder; a failing one keeps it and prints the path.
- A failing spec leaves traces in `frontend/e2e/.results/`; read the trace before retrying.
- Validate with specs and the crawler, not with fuzzing.

## Done when

- [ ] Specs pass on private servers; crawler clean for the screens you changed
- [ ] `private-stack.sh stop` run; no stray processes (`private-stack.sh status`)
- [ ] No test DAG left on any target (no "DAGs left behind" warning in the output)
