#!/usr/bin/env node
/**
 * Screenshots of the AutoAir UI in light and dark mode, in plugin and/or standalone mode.
 * Read-only: it opens pages and never deploys, saves or deletes anything.
 *
 *   node .claude/skills/verify-ui-screenshots/scripts/shoot.mjs \
 *     --modes standalone --standalone-url http://127.0.0.1:18773/ --dag my_dag
 *
 * Options (all optional):
 *   --modes plugin,standalone   which hosts to shoot (default: both)
 *   --themes light,dark         (default: both)
 *   --airflow-url URL           plugin host and token source (default $AIRFLOW_URL or :8080)
 *   --standalone-url URL        standalone UI: `autoair serve` /ui/ or a vite preview root
 *                               (default http://127.0.0.1:8765/ui/)
 *   --dag ID                    open this DAG
 *   --lang en|tr                UI language (default en)
 *   --viewport 1600x1000
 *   --wait MS                   settle time after load (default 1500)
 *   --out DIR                   default .artifacts/screenshots/<timestamp> (gitignored)
 *   --click "TEXT"              click an element with this text before shooting
 *   --ignore REGEX              console errors matching REGEX are not failures (e.g. "502"
 *                               when the backend runs with Airflow unreachable)
 *
 * Exit code 1 when a page throws (pageerror) or logs a console error: a broken panel fails.
 */
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../../..");
const { chromium } = createRequire(resolve(root, "frontend/package.json"))("@playwright/test");

const args = {};
for (let i = 2; i < process.argv.length; i++) {
  const key = process.argv[i].replace(/^--/, "");
  const next = process.argv[i + 1];
  args[key] = next !== undefined && !next.startsWith("--") ? process.argv[++i] : "true";
}
const list = (v, d) => (v ?? d).split(",").map((s) => s.trim()).filter(Boolean);
const modes = list(args.modes, "plugin,standalone");
const themes = list(args.themes, "light,dark");
const airflow = (args["airflow-url"] ?? process.env.AIRFLOW_URL ?? "http://localhost:8080").replace(/\/$/, "");
const standalone = args["standalone-url"] ?? "http://127.0.0.1:8765/ui/";
const [width, height] = (args.viewport ?? "1600x1000").split("x").map(Number);
const lang = args.lang ?? "en";
const wait = Number(args.wait ?? 1500);
const ignore = args.ignore ? new RegExp(args.ignore) : null;
const stamp = new Date().toISOString().replace(/[:.]/g, "-");
const out = resolve(args.out ?? resolve(root, ".artifacts/screenshots", stamp));
mkdirSync(out, { recursive: true });

async function token() {
  try {
    const r = await fetch(`${airflow}/auth/token`);
    return r.ok ? (await r.json()).access_token : null;
  } catch {
    return null;
  }
}

function url(mode) {
  const dag = args.dag ? encodeURIComponent(args.dag) : null;
  if (mode === "plugin") return `${airflow}/plugin/dag-builder${dag ? `#dag=${dag}` : ""}`;
  return `${standalone.replace(/\/?$/, "/")}${dag ? `?dag=${dag}` : ""}`;
}

const bearer = await token();
const browser = await chromium.launch({ channel: "chrome", headless: true });
let failures = 0;
try {
  for (const mode of modes) {
    for (const theme of themes) {
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: theme });
      await context.addInitScript(
        ([t, m, l]) => {
          try {
            if (t) sessionStorage.setItem("autoair.airflowToken", t);
            localStorage.setItem("autoair.colorMode", m); // standalone toggle
            localStorage.setItem("theme", m); // Airflow UI (next-themes) inside the plugin
            localStorage.setItem("autoair.lang", l);
          } catch {
            /* storage unavailable */
          }
        },
        [bearer, theme, lang],
      );
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
      page.on("console", (m) => {
        if (m.type() === "error" && !(ignore && ignore.test(m.text()))) errors.push(`console: ${m.text()}`);
      });
      const target = url(mode);
      try {
        await page.goto(target, { waitUntil: "networkidle", timeout: 60_000 });
        if (args.click) await page.getByText(args.click, { exact: false }).first().click();
        await page.waitForTimeout(wait);
        const file = resolve(out, `${mode}-${theme}${args.dag ? `-${args.dag}` : ""}.png`);
        await page.screenshot({ path: file, fullPage: false });
        console.log(`${errors.length ? "FAIL" : "ok  "} ${mode}/${theme} ${target} -> ${file}`);
      } catch (e) {
        errors.push(`navigation: ${e.message.split("\n")[0]}`);
        console.log(`FAIL ${mode}/${theme} ${target}`);
      }
      for (const e of errors) console.log(`     ${e}`);
      failures += errors.length ? 1 : 0;
      await context.close();
    }
  }
} finally {
  await browser.close();
}
process.exit(failures ? 1 : 0);
