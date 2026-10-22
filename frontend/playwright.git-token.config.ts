import { resolve } from "node:path";

import { defineConfig } from "@playwright/test";

/**
 * Git token typed per deploy (storage.git.request_token=required), end to end on its own
 * servers (`npx playwright test -c playwright.git-token.config.ts`):
 *
 * - a production build served by `vite preview` (AUTOAIR_GIT_TOKEN_UI_PORT, default 8900);
 * - `autoair serve --no-auth` (AUTOAIR_GIT_TOKEN_API_PORT, default 8901) with a local git
 *   target pushing to a bare repository in AUTOAIR_GIT_TOKEN_STATE (recreated every run).
 *
 * Light and dark project; each runs the flow in English and Turkish. Screenshots are
 * written to AUTOAIR_GIT_TOKEN_SHOTS (default e2e/.results/git-token).
 */
const UI_PORT = Number(process.env.AUTOAIR_GIT_TOKEN_UI_PORT ?? 8900);
const API_PORT = Number(process.env.AUTOAIR_GIT_TOKEN_API_PORT ?? 8901);
const STATE = resolve(process.env.AUTOAIR_GIT_TOKEN_STATE ?? "e2e/.results/git-token-state");
const DIST = resolve(process.env.AUTOAIR_GIT_TOKEN_DIST ?? "e2e/.results/git-token-dist");
process.env.AUTOAIR_GIT_TOKEN_STATE = STATE;

const seed =
  `rm -rf ${STATE} && mkdir -p ${STATE}/dags && git init --bare -q -b main ${STATE}/remote.git && ` +
  `git clone -q ${STATE}/remote.git ${STATE}/seed && echo seed > ${STATE}/seed/README.md && ` +
  `git -C ${STATE}/seed add . && git -C ${STATE}/seed -c user.name=Seed -c user.email=s@x commit -qm seed && ` +
  `git -C ${STATE}/seed push -q origin HEAD:main`;

export default defineConfig({
  testDir: "e2e",
  testMatch: "git-token.spec.ts",
  outputDir: "e2e/.results/git-token-output",
  timeout: 120_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${UI_PORT}`,
    channel: "chrome",
    headless: true,
    viewport: { width: 1440, height: 900 },
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
      command:
        `${seed} && cd ${STATE} && ` +
        `AUTOAIR_AIRFLOW_URL=http://127.0.0.1:9 AUTOAIR_IAM__ANONYMOUS_ROLES='["Admin"]' AUTOAIR_FEATURES__AI=false AUTOAIR_FEATURES__COLLAB=false ` +
        `AUTOAIR_FEATURES__MARKETPLACE=false AUTOAIR_FEATURES__IDE_LANGUAGE_SERVER=false ` +
        `AUTOAIR_DAGBAG_POOL=false AUTOAIR_DISCOVER_AIRFLOW_FOLDERS=false AUTOAIR_IMPORT_CHECK_ON_DEPLOY=false ` +
        `AUTOAIR_STORAGE__MODE=git AUTOAIR_STORAGE__GIT__PROVIDER=local ` +
        `AUTOAIR_STORAGE__GIT__REPO=file://${STATE}/remote.git AUTOAIR_STORAGE__GIT__MODE=push ` +
        `AUTOAIR_STORAGE__GIT__CACHE_DIR=${STATE}/cache AUTOAIR_STORAGE__GIT__REQUEST_TOKEN=required ` +
        `uv run --project ${resolve("..")} autoair serve --no-auth --host 127.0.0.1 --port ${API_PORT} ` +
        `--dags-folder dags`,
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      timeout: 180_000,
      reuseExistingServer: false,
      stdout: "ignore",
      stderr: "pipe",
    },
    {
      command:
        `npx vite build --mode standalone --outDir ${DIST} --emptyOutDir --logLevel warn && ` +
        `AUTOAIR_API_PORT=${API_PORT} npx vite preview --mode standalone --outDir ${DIST} ` +
        `--host 127.0.0.1 --port ${UI_PORT} --strictPort`,
      url: `http://127.0.0.1:${UI_PORT}/`,
      timeout: 240_000,
      reuseExistingServer: false,
    },
  ],
});
