---
name: update-visual-baselines
description: Run or update the visual regression baselines (frontend/e2e/visual, light and dark). Use when npm run test:visual fails after an intended UI change, or when adding a screen to the visual suite.
---

# Update visual baselines

The suite (`frontend/playwright.visual.config.ts`, `frontend/e2e/visual/README.md`; started by
`npm run test:visual` = `e2e/visual/run.mjs`) works in a run folder of its own, `<run>` =
`$AUTOAIR_VISUAL_DIR` or `.artifacts/visual/<pid>-<random>/` at the repository root: it builds
the standalone UI into `<run>/dist`, starts `autoair serve --no-auth` with state in
`<run>/state` and `vite preview` on free ports, mocks Airflow in `fixtures.ts`, and compares each screen in the `light`
and `dark` projects with `e2e/visual/__screenshots__/screens.spec.ts/<screen>-<project>.png`
(`maxDiffPixelRatio: 0.01`).

The plugin-mode suite (`npm run test:visual:plugin`, `playwright.visual-plugin.config.ts`,
`e2e/visual/plugin.spec.ts`) takes the same screens plus the connection form, clear, backfill
and asset-action dialogs inside a private Airflow with the plugin; its browser runs in the
Playwright Docker image, so its baselines (`__screenshots__/plugin.spec.ts/`) are the same on
every Linux machine with Docker and in CI. The steps below apply to both suites: use
`test:visual:plugin` for the plugin screens.

## Steps

1. Run it first and read the diff images:

   ```bash
   cd frontend
   npm run test:visual              # prints "visual run: <run> (ui :<port>, api :<port>)"
   ls <run>/results/                # kept on failure: *-actual.png, *-diff.png per failing screen
   ```

2. Decide per screen: an intended change, or a regression to fix. Only intended changes get
   new baselines.
3. Update only what changed:

   ```bash
   npm run test:visual -- --update-snapshots -g "<screen title>"            # both themes
   npm run test:visual -- --update-snapshots -g "<screen title>" --project=dark
   ```

4. Open the new PNGs (light and dark) and compare with the old ones (`git diff --stat`
   lists them). Commit baselines together with the UI change that caused them.

## Baseline policy

- Baselines are rendered on Linux with the installed Google Chrome; fonts and Chrome versions
  shift pixels. Regenerate on Linux, never on macOS/Windows.
- Plugin baselines need Docker (the browser is in `mcr.microsoft.com/playwright:v<version>-noble`);
  `AUTOAIR_VISUAL_BROWSER=local` is for iterating only, never for updating baselines. A
  `@playwright/test` or Airflow upgrade (`uv.lock`) regenerates them in the same change.
- The CI `visual` job is non-blocking (`continue-on-error`) until baselines prove stable on
  hosted runners; a red visual job still needs an explanation in the PR.
- Never update all baselines to make a failure go away. Never raise `maxDiffPixelRatio`.
- A new screen: add it to `screens.spec.ts` with deterministic data from `fixtures.ts`, add a
  row to `e2e/visual/README.md`, generate both themes.

## Pitfalls

- Concurrent runs are isolated (own folder, own free ports). `AUTOAIR_VISUAL_DIR`,
  `AUTOAIR_VISUAL_UI_PORT` and `AUTOAIR_VISUAL_API_PORT` pin them; a folder you pass is kept.
- The machine-specific deploy folder (inside `<run>`) is masked in the screenshots.

## Done when

- [ ] Every changed PNG reviewed in light and dark
- [ ] Only screens affected by the intended change updated
- [ ] Suite passes locally
