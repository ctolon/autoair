import { readFileSync } from "node:fs";
import path from "node:path";

import { defineConfig } from "@playwright/test";

/**
 * Visual regression inside the Airflow UI (`npm run test:visual:plugin`, see
 * e2e/visual/README.md): a private Airflow (`scripts/dev-airflow.sh`, AIRFLOW_HOME in
 * `<run>/airflow`, its own API and log-server ports) with the AutoAir plugin, and the
 * `<run>/plugin-dist` bundle served in place of Airflow's `/autoair/static/` through a route, so
 * the bundles in src/autoair/static (which the shared demo serves) are never rewritten.
 *
 * The browser runs in the Playwright Docker image (`mcr.microsoft.com/playwright:v<version>`,
 * the version of @playwright/test): Chromium and fonts are the same on every machine and in
 * CI, which is what makes the baselines portable. It reaches the host's Airflow through
 * Playwright's `exposeNetwork` tunnel. `AUTOAIR_VISUAL_BROWSER=local` uses the installed Google
 * Chrome instead (fast iteration; its screenshots do not match the baselines).
 *
 * Set by e2e/visual/run.mjs:
 *   AUTOAIR_VISUAL_DIR             run folder
 *   AUTOAIR_VISUAL_AIRFLOW_PORT    Airflow API server port (+ AUTOAIR_VISUAL_LOG_PORTS "a,b")
 *   AUTOAIR_VISUAL_PW_PORT         Playwright browser server port (Docker)
 *   AUTOAIR_VISUAL_PLUGIN_DIST     plugin bundle served as /autoair/static/ (default <run>/plugin-dist)
 * Optional:
 *   AUTOAIR_VISUAL_AIRFLOW_URL     reuse a running private Airflow with the plugin (nothing started);
 *                                  it needs the environment the webServer below sets
 */
const REPO = path.resolve(import.meta.dirname, "..");
process.env.AUTOAIR_VISUAL_DIR ??= path.join(REPO, ".artifacts", "visual", `plugin-${process.pid}`);
const RUN_DIR = path.resolve(process.env.AUTOAIR_VISUAL_DIR);
const AIRFLOW_HOME = path.join(RUN_DIR, "airflow");
// Read by e2e/visual/pluginBundle.ts in the workers.
process.env.AUTOAIR_VISUAL_PLUGIN_DIST = path.resolve(
  process.env.AUTOAIR_VISUAL_PLUGIN_DIST ?? path.join(RUN_DIR, "plugin-dist"),
);
const AIRFLOW_PORT = Number(process.env.AUTOAIR_VISUAL_AIRFLOW_PORT ?? 8852);
const [WORKER_LOG_PORT, TRIGGER_LOG_PORT] = (process.env.AUTOAIR_VISUAL_LOG_PORTS ?? "8853,8854").split(",");
const PW_PORT = Number(process.env.AUTOAIR_VISUAL_PW_PORT ?? 8855);
const REUSE = process.env.AUTOAIR_VISUAL_AIRFLOW_URL;
const BASE_URL = REUSE ?? `http://127.0.0.1:${AIRFLOW_PORT}`;
const LOCAL_BROWSER = process.env.AUTOAIR_VISUAL_BROWSER === "local";
const PW_VERSION = (
  JSON.parse(readFileSync(path.join(import.meta.dirname, "node_modules/@playwright/test/package.json"), "utf8")) as {
    version: string;
  }
).version;
const PW_IMAGE = `mcr.microsoft.com/playwright:v${PW_VERSION}-noble`;
const PW_CONTAINER = `autoair-visual-pw-${PW_PORT}`;
const sh = (p: string) => `'${p.replaceAll("'", "'\\''")}'`;

// Same AutoAir settings as the standalone suite: AI, collaboration, marketplace and the
// language server off, one policy rule so the Problems tab has content.
const AUTOAIR_ENV =
  `AUTOAIR_FEATURES__AI=false AUTOAIR_FEATURES__COLLAB=false AUTOAIR_FEATURES__MARKETPLACE=false ` +
  `AUTOAIR_FEATURES__IDE_LANGUAGE_SERVER=false AUTOAIR_DAGBAG_POOL=false ` +
  `AUTOAIR_POLICY__RULES='[{"id":"owner-required","severity":"warning","check":"require_owner"}]'`;

export default defineConfig({
  testDir: "e2e/visual",
  testMatch: "plugin.spec.ts",
  outputDir: path.join(RUN_DIR, "results"),
  snapshotPathTemplate: "{testDir}/__screenshots__/{testFilePath}/{arg}-{projectName}{ext}",
  globalTeardown: "./e2e/visual/plugin-teardown.ts",
  timeout: 120_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  expect: {
    toHaveScreenshot: { maxDiffPixelRatio: 0.01, animations: "disabled", caret: "hide", scale: "css" },
  },
  use: {
    baseURL: BASE_URL,
    ...(LOCAL_BROWSER
      ? { channel: "chrome" }
      : { connectOptions: { wsEndpoint: `ws://127.0.0.1:${PW_PORT}/`, exposeNetwork: "<loopback>" } }),
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
    ...(REUSE
      ? []
      : [
          {
            // A throw-away Airflow home per run: no example DAGs, simple auth manager (all admins).
            command:
              `rm -rf ${sh(AIRFLOW_HOME)} && mkdir -p ${sh(AIRFLOW_HOME)}/dags && ` +
              `AIRFLOW_HOME=${sh(AIRFLOW_HOME)} AIRFLOW_PORT=${AIRFLOW_PORT} ` +
              `AIRFLOW__LOGGING__WORKER_LOG_SERVER_PORT=${WORKER_LOG_PORT} ` +
              `AIRFLOW__LOGGING__TRIGGER_LOG_SERVER_PORT=${TRIGGER_LOG_PORT} ` +
              `${AUTOAIR_ENV} exec ${sh(path.join(REPO, "scripts", "dev-airflow.sh"))}`,
            url: `${BASE_URL}/autoair/api/health`,
            timeout: 300_000,
            reuseExistingServer: false,
            gracefulShutdown: { signal: "SIGTERM" as const, timeout: 20_000 },
            stdout: "ignore" as const,
            stderr: "pipe" as const,
          },
        ]),
    ...(LOCAL_BROWSER
      ? []
      : [
          {
            // The browser server of the Playwright image; node_modules is mounted so the server is
            // exactly the client's version (no download inside the container).
            command:
              `docker run --rm --init --ipc=host --name ${PW_CONTAINER} -p 127.0.0.1:${PW_PORT}:${PW_PORT} ` +
              `-v ${sh(path.join(import.meta.dirname, "node_modules"))}:/pw/node_modules:ro -w /pw ${PW_IMAGE} ` +
              `node /pw/node_modules/playwright/cli.js run-server --port ${PW_PORT} --host 0.0.0.0`,
            port: PW_PORT,
            timeout: 600_000,
            reuseExistingServer: false,
            gracefulShutdown: { signal: "SIGTERM" as const, timeout: 10_000 },
          },
        ]),
  ],
});
