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
