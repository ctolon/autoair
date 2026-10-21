#!/usr/bin/env node
/**
 * `npm run test:visual`: builds the standalone bundle and runs the visual suite in a folder of
 * its own, so concurrent runs (agents, CI shards) never share state, build output or ports.
 *
 *   AUTOAIR_VISUAL_DIR       run folder (default <repo>/.artifacts/visual/<pid>-<random>/);
 *                            holds dist/, state/ and results/
 *   AUTOAIR_VISUAL_UI_PORT   vite preview port (default: a free port)
 *   AUTOAIR_VISUAL_API_PORT  autoair serve port (default: a free port)
 *   AUTOAIR_VISUAL_KEEP=1    keep the run folder after a passing run
 *
 * `--plugin` (`npm run test:visual:plugin`): the suite inside the Airflow UI instead
 * (playwright.visual-plugin.config.ts): builds the plugin bundle into <run>/plugin-dist, starts a
 * private Airflow with the plugin and the Playwright Docker browser on free ports.
 *   AUTOAIR_VISUAL_PLUGIN_DIST   reuse an existing plugin build (e.g. CI's ui-bundles) instead
 *   AUTOAIR_VISUAL_AIRFLOW_URL   reuse a running private Airflow instead of starting one
 *   AUTOAIR_VISUAL_BROWSER=local installed Chrome instead of Docker (does not match baselines)
 *
 * Extra arguments go to Playwright (`npm run test:visual -- --update-snapshots`). A failing
 * run keeps its folder (diffs in results/) and prints where it is.
 */
import { spawnSync } from "node:child_process";
import console from "node:console";
import { randomBytes } from "node:crypto";
import { rmSync } from "node:fs";
import { createServer } from "node:net";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
const plugin = args[0] === "--plugin";
if (plugin) args.shift();
const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const repo = path.resolve(frontend, "..");
const explicitDir = !!process.env.AUTOAIR_VISUAL_DIR;
const runDir = path.resolve(
  process.env.AUTOAIR_VISUAL_DIR ??
    path.join(
      repo,
      ".artifacts",
      "visual",
      `${plugin ? "plugin-" : ""}${process.pid}-${randomBytes(3).toString("hex")}`,
    ),
);

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

const port = async (name) => process.env[name] ?? String(await freePort());
const env = { ...process.env, AUTOAIR_VISUAL_DIR: runDir };
if (plugin) {
  env.AUTOAIR_VISUAL_AIRFLOW_PORT = await port("AUTOAIR_VISUAL_AIRFLOW_PORT");
  env.AUTOAIR_VISUAL_LOG_PORTS = process.env.AUTOAIR_VISUAL_LOG_PORTS ?? `${await freePort()},${await freePort()}`;
  env.AUTOAIR_VISUAL_PW_PORT = await port("AUTOAIR_VISUAL_PW_PORT");
} else {
  env.AUTOAIR_VISUAL_UI_PORT = await port("AUTOAIR_VISUAL_UI_PORT");
  env.AUTOAIR_VISUAL_API_PORT = await port("AUTOAIR_VISUAL_API_PORT");
}
const run = (cmd, argv) => spawnSync(cmd, argv, { cwd: frontend, env, stdio: "inherit" }).status ?? 1;

console.log(
  plugin
    ? `visual run (plugin): ${runDir} (airflow :${env.AUTOAIR_VISUAL_AIRFLOW_PORT}, browser :${env.AUTOAIR_VISUAL_PW_PORT})`
    : `visual run: ${runDir} (ui :${env.AUTOAIR_VISUAL_UI_PORT}, api :${env.AUTOAIR_VISUAL_API_PORT})`,
);
const build = (mode, outDir) =>
  run("npx", ["vite", "build", "--mode", mode, "--outDir", outDir, "--emptyOutDir", "--logLevel", "warn"]);
let status = 0;
if (!plugin) status = build("standalone", path.join(runDir, "dist"));
else if (!process.env.AUTOAIR_VISUAL_PLUGIN_DIST) status = build("plugin", path.join(runDir, "plugin-dist"));
if (status === 0) {
  const config = plugin ? "playwright.visual-plugin.config.ts" : "playwright.visual.config.ts";
  status = run("npx", ["playwright", "test", "-c", config, ...args]);
}
if (status === 0 && !explicitDir && process.env.AUTOAIR_VISUAL_KEEP !== "1") {
  rmSync(runDir, { recursive: true, force: true });
} else if (status !== 0) {
  console.error(`visual run failed; artefacts kept in ${runDir}`);
}
process.exit(status);
