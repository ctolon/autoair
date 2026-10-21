---
paths:
  - "frontend/e2e/**"
  - "frontend/playwright*.config.ts"
---

# E2E, crawler and visual tests

Running them: skill `run-e2e`; baselines: skill `update-visual-baselines`.

- Point every target at private servers (the private stack, or a private Airflow with its own
  `AIRFLOW_HOME` and `AIRFLOW_PORT`). E2E and screenshots run against `vite build` output served
  by `vite preview`, not the dev server.
- A spec that deploys DAGs tracks and deletes them with `DeployedDags` from `e2e/cleanup.ts`
  (`track()` right before the deploy, `cleanup(request)` in `afterAll`).
- The crawler (`e2e/crawl.spec.ts`) deploys `crawl_<spec>_<target>_<suffix>` DAGs and deletes
  them in `afterAll`; a "DAGs left behind" warning means cleanup failed.
- Visual suite (`e2e/visual/`, `npm run test:visual`): each run builds and serves from its own
  folder on free ports and compares the `light` and `dark` projects with
  `e2e/visual/__screenshots__/` (`maxDiffPixelRatio: 0.01`). Baselines are rendered on Linux
  with Google Chrome; update only screens whose change is intended, review every PNG in both
  themes, never raise the threshold. A new screen uses deterministic data from `fixtures.ts`
  and gets a row in `e2e/visual/README.md`. The CI `visual` job is non-blocking (`continue-on-error`).
- Validate with explicit specs and the crawler, not with fuzzing. Read the trace in
  `frontend/e2e/.results/` before retrying a failing spec.
