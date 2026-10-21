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

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const repo = path.resolve(frontend, "..");
const explicitDir = !!process.env.AUTOAIR_VISUAL_DIR;
const runDir = path.resolve(
  process.env.AUTOAIR_VISUAL_DIR ??
    path.join(repo, ".artifacts", "visual", `${process.pid}-${randomBytes(3).toString("hex")}`),
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

const env = {
  ...process.env,
  AUTOAIR_VISUAL_DIR: runDir,
  AUTOAIR_VISUAL_UI_PORT: process.env.AUTOAIR_VISUAL_UI_PORT ?? String(await freePort()),
  AUTOAIR_VISUAL_API_PORT: process.env.AUTOAIR_VISUAL_API_PORT ?? String(await freePort()),
};
const run = (cmd, args) => spawnSync(cmd, args, { cwd: frontend, env, stdio: "inherit" }).status ?? 1;

console.log(`visual run: ${runDir} (ui :${env.AUTOAIR_VISUAL_UI_PORT}, api :${env.AUTOAIR_VISUAL_API_PORT})`);
let status = run("npx", [
  "vite",
  "build",
  "--mode",
  "standalone",
  "--outDir",
  path.join(runDir, "dist"),
  "--emptyOutDir",
  "--logLevel",
  "warn",
]);
if (status === 0)
  status = run("npx", ["playwright", "test", "-c", "playwright.visual.config.ts", ...process.argv.slice(2)]);
if (status === 0 && !explicitDir && process.env.AUTOAIR_VISUAL_KEEP !== "1") {
  rmSync(runDir, { recursive: true, force: true });
} else if (status !== 0) {
  console.error(`visual run failed; artefacts kept in ${runDir}`);
}
process.exit(status);
