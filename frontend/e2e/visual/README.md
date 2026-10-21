# Visual regression suite

`npm run test:visual` (from `frontend/`) screenshots the main screens in light and dark mode
and compares them with the baselines in `__screenshots__/`:

| Screen             | What it shows                                                                                   |
| ------------------ | ----------------------------------------------------------------------------------------------- |
| `builder-empty`    | an empty document: palette, canvas start card, DAG settings                                     |
| `canvas-inspector` | a 4-task DAG with a task selected in the inspector                                              |
| `problems`         | the Problems tab with `policy.rules` warnings                                                   |
| `code`             | the generated Python file                                                                       |
| `environments`     | DAG settings → Environments → `prod` with the connection/variable mapping and its Airflow check |
| `deploy-dialog`    | the deploy dialog (target, message, effect)                                                     |
| `bulk-import`      | the DAG repository import result                                                                |
| `airflow-endpoint` | the Airflow tab with the trigger endpoint card                                                  |

## How it stays deterministic

- a production build (`<run>/dist`, `vite build --mode standalone`) served by
  `vite preview` — the build the demo serves (`src/autoair/static`) is never touched;
- a throw-away backend: `autoair serve --no-auth` with its state in `<run>/state`
  (recreated on every run), Airflow unreachable, AI / collaboration / marketplace / language
  server off, one `policy.rules` entry so the Problems tab has content;
- Airflow's REST answers (connections, variables, capabilities, DAG status, mapping check…)
  and the bulk scan are mocked in `fixtures.ts`;
- fixed viewport (1440×900, scale 1), locale `en-US`, time zone UTC, a fixed clock,
  animations and the caret disabled, the deploy folder (inside the run folder) rewritten to
  `/visual-run` and masked;
- `maxDiffPixelRatio: 0.01` absorbs anti-aliasing noise.

Every run gets its own folder `<run>`, so concurrent runs in one checkout never collide:
`npm run test:visual` (`e2e/visual/run.mjs`) uses `AUTOAIR_VISUAL_DIR` or
`<repo>/.artifacts/visual/<pid>-<random>/` (gitignored), builds into `<run>/dist`, and removes
the folder after a passing run (`AUTOAIR_VISUAL_KEEP=1` keeps it; a folder given in
`AUTOAIR_VISUAL_DIR` is always kept). Both servers are started by Playwright
(`playwright.visual.config.ts`) on free ports picked by `run.mjs`; set
`AUTOAIR_VISUAL_UI_PORT` / `AUTOAIR_VISUAL_API_PORT` to choose them (8850 / 8851 when the
config is used without `run.mjs`).
The browser is the installed Google Chrome (`channel: "chrome"`), like the other E2E tests.

## Inside the Airflow UI (plugin mode)

`npm run test:visual:plugin` (`run.mjs --plugin`, `playwright.visual-plugin.config.ts`,
`plugin.spec.ts`) takes the same screens inside Airflow (Airflow ≥ 3.1 `react_apps`,
**Browse → DAG Builder**, `/plugin/dag-builder`), with Airflow's navigation around the
builder and the builder following Airflow's theme, plus the dialogs that need Airflow:

| Screen            | What it shows                                                        |
| ----------------- | -------------------------------------------------------------------- |
| `connection-form` | **New connection** from a `conn_id` parameter, typed `postgres` form |
| `clear-dialog`    | Runs tab → a run → **Clear run…**                                    |
| `backfill-dialog` | Runs tab → **Backfill…**                                             |
| `asset-actions`   | Data tab → an outlet Airflow knows → **Send test event** opened      |

Baselines are in `__screenshots__/plugin.spec.ts/`. How it stays deterministic:

- a private Airflow (`scripts/dev-airflow.sh`, simple auth manager with all admins) with its
  `AIRFLOW_HOME` in `<run>/airflow` and its API and both log-server ports picked free by
  `run.mjs`, so it never meets the shared demo; the same AutoAir settings as the standalone
  suite (AI, collaboration, marketplace and language server off, one `policy.rules` entry);
- the plugin bundle is built into `<run>/plugin-dist` and served in place of Airflow's
  `/autoair/static/` through a Playwright route: `src/autoair/static` is neither needed nor
  rewritten;
- **the browser runs in the Playwright Docker image** (`mcr.microsoft.com/playwright:v<version>-noble`,
  `<version>` = the installed `@playwright/test`): Chromium and its fonts are identical on
  every Linux machine and in CI, so baselines made locally match CI. The container mounts
  `frontend/node_modules` read-only and reaches the host's Airflow through Playwright's
  `exposeNetwork` tunnel; it is removed after the run (`plugin-teardown.ts`). Docker is
  required;
- Airflow's theme (`localStorage.theme`) is pinned to the project's colour scheme; Airflow's
  REST answers the builder asks for are mocked as in the standalone suite (`prepare`,
  `preparePlugin` in `fixtures.ts`);
- volatile text: the deploy folder (inside `<run>`) and the free Airflow port in URLs are
  rewritten to fixed placeholders before each screenshot, provider version badges are masked.

Options: `AUTOAIR_VISUAL_PLUGIN_DIST` reuses an existing plugin build;
`AUTOAIR_VISUAL_AIRFLOW_URL` reuses a running private Airflow started with the same
environment (nothing is started); `AUTOAIR_VISUAL_BROWSER=local` uses the installed Chrome
for quick iteration (its screenshots do not match the baselines). A run takes about 2 minutes
(Airflow start included).

Airflow 3.0 has no `react_apps`; it serves the standalone bundle as a full page at
`/autoair/ui/`, which the standalone suite above covers.

## Updating the baselines

After an intended UI change:

```bash
cd frontend
npm run test:visual -- --update-snapshots          # all screens, light + dark
npm run test:visual -- --update-snapshots -g "deploy dialog" --project=dark   # one screen
```

Review the changed PNGs in `__screenshots__/screens.spec.ts/` before committing them. A failing
comparison keeps the run folder and leaves `*-actual.png` / `*-diff.png` next to the trace in
`<run>/results/` (the path is printed at the end).
Fonts and the Chrome version influence rendering: regenerate the baselines on the machine (or CI
image) that runs the suite.

Plugin baselines, on any Linux machine with Docker (the browser is the Playwright image's, so
the result is the one CI computes):

```bash
cd frontend
npm run test:visual:plugin -- --update-snapshots                       # all screens, light + dark
npm run test:visual:plugin -- --update-snapshots -g "backfill" --project=dark   # one screen
```

Upgrading `@playwright/test` changes the image tag and with it the rendering: regenerate the
plugin baselines in the same change. So does an Airflow upgrade in `uv.lock` (Airflow's
navigation is part of every full-page plugin screenshot).
