import path from "node:path";

import { defineConfig } from "@playwright/test";

/**
 * Deterministic visual regression suite (`npm run test:visual`, see e2e/visual/README.md).
 *
 * Hermetic: a production build (`<run>/dist`) served by `vite preview`, a throw-away AutoAir
 * backend (`autoair serve --no-auth`, state in `<run>/state`) and Airflow's REST answers mocked
 * in the specs. Fixed viewport, locale, time zone and clock; animations off. Baselines live in
 * `e2e/visual/__screenshots__/` (light + dark project per screen).
 *
 * `<run>` is AUTOAIR_VISUAL_DIR, set per run by e2e/visual/run.mjs (`npm run test:visual`,
 * which also builds `<run>/dist` and picks free ports) so concurrent runs never collide.
 */
const REPO = path.resolve(import.meta.dirname, "..");
// Set once in the main process; Playwright's workers inherit it (the specs mask this path).
process.env.AUTOAIR_VISUAL_DIR ??= path.join(REPO, ".artifacts", "visual", String(process.pid));
const RUN_DIR = path.resolve(process.env.AUTOAIR_VISUAL_DIR);
const STATE = path.join(RUN_DIR, "state");
const DIST = path.join(RUN_DIR, "dist");
const UI_PORT = Number(process.env.AUTOAIR_VISUAL_UI_PORT ?? 8850);
const API_PORT = Number(process.env.AUTOAIR_VISUAL_API_PORT ?? 8851);
const sh = (p: string) => `'${p.replaceAll("'", "'\\''")}'`;

export default defineConfig({
  testDir: "e2e/visual",
  // plugin.spec.ts runs inside Airflow: playwright.visual-plugin.config.ts.
  testMatch: "screens.spec.ts",
  outputDir: path.join(RUN_DIR, "results"),
  snapshotPathTemplate: "{testDir}/__screenshots__/{testFilePath}/{arg}-{projectName}{ext}",
  timeout: 90_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  expect: {
    toHaveScreenshot: { maxDiffPixelRatio: 0.01, animations: "disabled", caret: "hide", scale: "css" },
  },
  use: {
    baseURL: `http://127.0.0.1:${UI_PORT}`,
    channel: "chrome",
    headless: true,
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    locale: "en-US",
    timezoneId: "UTC",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "light", use: { colorScheme: "light" } },
    { name: "dark", use: { colorScheme: "dark" } },
  ],
  webServer: [
    {
      // Run from the state folder: no .env / config file of the checkout is picked up.
      command:
        `rm -rf ${sh(STATE)} && mkdir -p ${sh(STATE)}/dags && cd ${sh(STATE)} && ` +
        `AUTOAIR_AIRFLOW_URL=http://127.0.0.1:9 AUTOAIR_FEATURES__AI=false AUTOAIR_FEATURES__COLLAB=false ` +
        `AUTOAIR_FEATURES__MARKETPLACE=false AUTOAIR_FEATURES__IDE_LANGUAGE_SERVER=false AUTOAIR_DAGBAG_POOL=false AUTOAIR_DISCOVER_AIRFLOW_FOLDERS=false ` +
        `AUTOAIR_POLICY__RULES='[{"id":"owner-required","severity":"warning","check":"require_owner"}]' ` +
        `uv run --project ${sh(REPO)} autoair serve --no-auth --host 127.0.0.1 --port ${API_PORT} ` +
        `--dags-folder dags`,
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      timeout: 180_000,
      reuseExistingServer: false,
      stdout: "ignore",
      stderr: "pipe",
    },
    {
      command: `AUTOAIR_API_PORT=${API_PORT} npx vite preview --mode standalone --outDir ${sh(DIST)} --host 127.0.0.1 --port ${UI_PORT} --strictPort`,
      url: `http://127.0.0.1:${UI_PORT}/`,
      timeout: 60_000,
      reuseExistingServer: false,
    },
  ],
});
