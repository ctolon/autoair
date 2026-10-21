#!/usr/bin/env node
/**
 * Screenshots and videos of the AutoAir UI for the documentation site.
 *
 *   npm run capture                 # everything (screens light+dark, then videos)
 *   npm run capture:screens         # --only screens
 *   npm run capture:videos          # --only videos
 *   node scripts/capture/run.mjs --flow showcase,ai --modes light   # some flows / videos only
 *   flags: --no-build --no-optimize --keep-server (leave the servers up for another run)
 *
 * Needs the shared Airflow 3 on $AIRFLOW_URL (default http://localhost:8080) whose
 * /auth/token hands out a token (scripts/dev-airflow.sh / scripts/demo.sh), Python deps of
 * the repo (.venv or .venv-allproviders) and Chromium for Playwright (npx playwright install
 * chromium). Ports 8860 (API), 8861 (UI), 8869 (fake LLM): see server.sh.
 *
 * What it does:
 *   1. builds the standalone UI from frontend/ (--no-build reuses the last bundle) and starts
 *      server.sh from a clean scripts/capture/.state (the signed marketplace is kept), first
 *      in git *push* mode to seed the demo DAGs (seed.mjs) onto main, then restarts it in
 *      *PR* mode; an already running capture server is reused as is (stop it for a clean run);
 *   2. runs the screen flows (screens.mjs) once per color mode at 1440x900 into
 *      static/img/screens/<name>-<light|dark>.png;
 *   3. records the videos (videos.mjs) into static/video/<name>.webm + <name>.jpg poster
 *      (re-encoded to VP9 with Playwright's bundled ffmpeg when present);
 *   4. quantizes the PNGs (optimize.py, Pillow through `uv run --with pillow`);
 *   5. stops the servers it started.
 * The shared Airflow is only read: its etl_full DAG and existing runs feed the run overlay,
 * task logs and AI troubleshooting screens; deploys go to the scratch repo in .state only.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, openSync, readdirSync, rmSync, statSync } from "node:fs";
import { resolve } from "node:path";

import { API, HERE, ROOT, SCREENS, STATE, UI, VIDEOS } from "./env.mjs";
import { chromium, newPage, shot as takeShot } from "./lib.mjs";
import { airflowToken, seed } from "./seed.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const flag = (name) => args.includes(`--${name}`);
const only = opt("only", "all");
const flowFilter = opt("flow", null)?.split(",");
const modes = opt("modes", "light,dark").split(",");

const up = async (url) => {
  try {
    return (await fetch(url)).ok;
  } catch {
    return false;
  }
};

let server = null;

function startServer(gitMode, { build = false } = {}) {
  const env = { ...process.env, CAPTURE_GIT_MODE: gitMode, CAPTURE_SKIP_BUILD: build ? "0" : "1" };
  mkdirSync(STATE, { recursive: true });
  const log = openSync(resolve(STATE, `server-${gitMode}.out`), "w");
  const child = spawn("bash", [resolve(HERE, "server.sh")], { env, detached: true, stdio: ["ignore", log, log] });
  server = child;
  return child;
}

async function stopServer() {
  if (!server) return;
  const s = server;
  server = null;
  try {
    process.kill(-s.pid, "SIGTERM");
  } catch {
    /* already gone */
  }
  for (let i = 0; i < 40 && ((await up(`${API}/health`)) || (await up(UI))); i++) await sleep(250);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitReady() {
  for (let i = 0; i < 600; i++) {
    if ((await up(`${API}/health`)) && (await up(UI))) return;
    if (server?.exitCode != null) throw new Error("server.sh exited, see scripts/capture/.state/*.log");
    await sleep(500);
  }
  throw new Error("capture server did not start");
}

function resetState({ rebuild }) {
  if (!existsSync(STATE)) return;
  for (const entry of readdirSync(STATE)) {
    if (entry === "marketplace" || (entry === "ui" && !rebuild)) continue;
    rmSync(resolve(STATE, entry), { recursive: true, force: true });
  }
}

async function ensureServer() {
  const running = (await up(`${API}/health`)) && (await up(UI));
  if (running) {
    console.log("• reusing the running capture server (state as is)");
    return;
  }
  const rebuild = !flag("no-build") || !existsSync(resolve(STATE, "ui/index.html"));
  resetState({ rebuild });
  console.log(`• seeding (git push mode${rebuild ? ", building the UI" : ""})`);
  startServer("push", { build: rebuild });
  await waitReady();
  await seed(await airflowToken());
  await stopServer();
  console.log("• starting the capture server (git PR mode)");
  startServer("pr");
  await waitReady();
}

async function runScreens(browser, token) {
  const { FLOWS } = await import("./screens.mjs");
  const failures = [];
  for (const flow of FLOWS) {
    if (flowFilter && !flowFilter.includes(flow.id)) continue;
    for (const mode of modes) {
      console.log(`• screens: ${flow.id} (${mode})`);
      const { context, page } = await newPage(browser, { mode, token: flow.signedOut ? null : token });
      const shot = (name, o) => takeShot(page, name, mode, o);
      try {
        await flow.run({ page, context, browser, token, mode, shot });
      } catch (e) {
        failures.push(`${flow.id} (${mode}): ${e.message.split("\n")[0]}`);
        console.warn(`  ✗ ${flow.id} (${mode}): ${e.message.slice(0, 600)}`);
        await page.screenshot({ path: resolve(STATE, `failed-${flow.id}-${mode}.png`) }).catch(() => {});
      } finally {
        await context.close();
      }
    }
  }
  return failures;
}

async function runVideos(browser, token) {
  const { VIDEO_SCENES } = await import("./videos.mjs");
  const failures = [];
  for (const scene of VIDEO_SCENES) {
    if (flowFilter && !flowFilter.includes(scene.id)) continue;
    console.log(`• video: ${scene.id}`);
    try {
      await scene.run({ browser, token });
    } catch (e) {
      failures.push(`video ${scene.id}: ${e.message.split("\n")[0]}`);
      console.warn(`  ✗ video ${scene.id}: ${e.message.slice(0, 600)}`);
    }
  }
  return failures;
}

function optimize() {
  return new Promise((done) => {
    const p = spawn("uv", ["run", "--quiet", "--with", "pillow", "python", resolve(HERE, "optimize.py"), SCREENS, VIDEOS], {
      cwd: ROOT,
      stdio: "inherit",
    });
    p.on("exit", done);
    p.on("error", (e) => {
      console.warn(`  optimize.py skipped: ${e.message}`);
      done();
    });
  });
}

function sizeOf(dir) {
  if (!existsSync(dir)) return 0;
  return readdirSync(dir).reduce((n, f) => n + statSync(resolve(dir, f)).size, 0);
}

async function main() {
  await ensureServer();
  const token = await airflowToken();
  const browser = await chromium.launch();
  const failures = [];
  try {
    if (only === "all" || only === "screens") failures.push(...(await runScreens(browser, token)));
    if (only === "all" || only === "videos") failures.push(...(await runVideos(browser, token)));
  } finally {
    await browser.close();
  }
  if (!flag("no-optimize")) await optimize();
  const mb = (n) => `${(n / 1024 / 1024).toFixed(1)} MB`;
  console.log(`• sizes: screens ${mb(sizeOf(SCREENS))}, videos ${mb(sizeOf(VIDEOS))}`);
  if (failures.length) {
    console.warn(`• ${failures.length} failure(s):\n  - ${failures.join("\n  - ")}`);
    process.exitCode = 1;
  }
}

const stop = async () => {
  await stopServer();
  process.exit(130);
};
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
main()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(async () => {
    if (!flag("keep-server")) await stopServer();
    else if (server) {
      console.log(`• capture server left running (kill -- -${server.pid})`);
      server.unref();
    }
  });
