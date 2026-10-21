// Short screen recordings (light mode, 1440x900, Playwright .webm) + a JPEG poster each.
// Headless Chromium draws no pointer, so showCursor() overlays one that follows the real
// mouse events the scenes send.
import { execFileSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import { homedir } from "node:os";
import { resolve } from "node:path";

import { STATE, UI, VIDEOS } from "./env.mjs";
import {
  VIEWPORT,
  bottomPanelTop,
  clickPane,
  connect,
  expect,
  fitView,
  glideClick,
  hideMinimap,
  newPage,
  node,
  openApp,
  sanitize,
  showCursor,
  toasts,
} from "./lib.mjs";
import { SHOWCASE } from "./seed.mjs";

const beat = (page, ms = 700) => page.waitForTimeout(ms);

/** Records `scene(page)` into static/video/<name>.webm; `poster(page)` is called once. */
async function record(browser, token, name, scene) {
  const dir = resolve(STATE, "video-tmp", name);
  rmSync(dir, { recursive: true, force: true });
  mkdirSync(VIDEOS, { recursive: true });
  const { context, page } = await newPage(browser, {
    mode: "light",
    token,
    recordVideo: { dir, size: VIEWPORT },
  });
  await showCursor(page);
  const started = Date.now();
  let from = 0; // seconds cut from the start (page loading is not worth watching)
  const markStart = () => (from = (Date.now() - started) / 1000);
  let posterTaken = false;
  const poster = async () => {
    await sanitize(page);
    await page.screenshot({ path: resolve(VIDEOS, `${name}.jpg`), type: "jpeg", quality: 80 });
    posterTaken = true;
  };
  let ok = false;
  try {
    await scene(page, { poster, context, markStart });
    if (!posterTaken) await poster();
    ok = true;
  } finally {
    const video = page.video();
    await context.close();
    if (video && ok) {
      const raw = resolve(dir, "raw.webm");
      await video.saveAs(raw);
      transcode(raw, resolve(VIDEOS, `${name}.webm`), Math.max(0, from - 0.3));
    }
    await video?.delete().catch(() => {});
    rmSync(dir, { recursive: true, force: true });
  }
  console.log(`    ✓ ${name}.webm (${Math.round((Date.now() - started) / 1000 - from)} s)`);
}

function dropPendingBranches() {
  const repo = resolve(STATE, "dags.git");
  const refs = execFileSync("git", ["--git-dir", repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/autoair/"])
    .toString()
    .split("\n")
    .filter(Boolean);
  for (const ref of refs) execFileSync("git", ["--git-dir", repo, "branch", "-D", ref], { stdio: "ignore" });
}

/** Playwright's own ffmpeg build (VP9 encoder included); there is no system ffmpeg. */
function findFfmpeg() {
  if (process.env.CAPTURE_FFMPEG) return process.env.CAPTURE_FFMPEG;
  const base = process.env.PLAYWRIGHT_BROWSERS_PATH || resolve(homedir(), ".cache/ms-playwright");
  if (!existsSync(base)) return null;
  for (const dir of readdirSync(base).filter((d) => d.startsWith("ffmpeg-")).sort().reverse()) {
    for (const exe of ["ffmpeg-linux", "ffmpeg-mac", "ffmpeg-win64.exe"]) {
      if (existsSync(resolve(base, dir, exe))) return resolve(base, dir, exe);
    }
  }
  return null;
}

/** Playwright records VP8 at a high bitrate: re-encode to VP9 (UI content shrinks ~5x). */
function transcode(src, dest, from = 0) {
  const ffmpeg = findFfmpeg();
  if (!ffmpeg) {
    copyFileSync(src, dest);
    return;
  }
  const args = ["-hide_banner", "-loglevel", "error", "-y", "-ss", from.toFixed(2), "-i", src];
  args.push("-c:v", "libvpx-vp9", "-crf", "34", "-b:v", "0");
  execFileSync(ffmpeg, [...args, "-deadline", "good", "-cpu-used", "4", "-row-mt", "1", "-an", dest], {
    stdio: "inherit",
  });
}

/** Types into the palette search like a person, then double-clicks the matching row. */
async function insert(page, query, name = query) {
  const search = page.getByPlaceholder(/Search operators/);
  await glideClick(page, search);
  await search.fill("");
  await page.keyboard.type(query, { delay: 40 });
  await beat(page, 300);
  const row = page
    .locator('[role="option"]')
    .filter({ has: page.getByText(name, { exact: true }) })
    .first();
  await glideClick(page, row, { dbl: true });
  await beat(page, 400);
  await search.fill("");
}

async function typeTaskId(page, id) {
  const field = page.getByRole("textbox", { name: /^Task id/ }).first();
  await glideClick(page, field);
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.type(id, { delay: 50 });
  await field.press("Tab");
  await beat(page, 250);
}

async function typeCode(page, field, text) {
  const editor = page.locator(`[data-autoair-field*="${field}"] .cm-content`).first();
  await glideClick(page, editor);
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.press("Backspace");
  // The first character typed into an empty field puts the caret back at the start (UI
  // bug, reported): type it, then continue from the end of the text.
  await page.keyboard.type(text[0]);
  await beat(page, 400);
  await page.keyboard.press("ControlOrMeta+End");
  await page.keyboard.type(text.slice(1), { delay: 12 });
  await beat(page, 250);
}

/** build-dag: an empty canvas becomes extract → transform → load with code generated live. */
async function buildDag(page, { poster, markStart }) {
  await openApp(page);
  markStart();
  await beat(page, 800);
  const dagId = page.getByRole("textbox", { name: "DAG id" });
  await glideClick(page, dagId);
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.type("orders_daily", { delay: 80 });
  await beat(page);

  await insert(page, "BashOperator");
  await typeTaskId(page, "extract");
  await typeCode(page, "bash_command", 'curl -sf -o "/data/orders/{{ ds }}.json" "$ORDERS_API"');

  await insert(page, "Python task");
  await typeTaskId(page, "transform");
  const fn = page.getByRole("textbox", { name: /^Function name/ });
  await fn.fill("transform");
  await fn.press("Tab");
  await insert(page, "SQLExecuteQueryOperator");
  await typeTaskId(page, "load");
  await typeCode(page, "sql", "INSERT INTO sales.daily_orders SELECT * FROM staging.orders");

  for (const [a, b] of [
    ["extract", "transform"],
    ["transform", "load"],
  ]) {
    await connect(page, a, b, { steps: 30 });
    await beat(page, 500);
  }
  await glideClick(page, page.getByRole("button", { name: "Auto layout" }));
  await beat(page, 900);
  await fitView(page);
  await clickPane(page, { x: 30, y: 30 });
  await glideClick(page, page.getByRole("tab", { name: "Schedule" }));
  await page.getByRole("combobox", { name: /^Schedule/ }).selectOption({ label: "Preset (@daily, …)" });
  await beat(page, 500);
  const preset = page.getByRole("combobox", { name: /preset/i });
  if (await preset.count()) await preset.first().selectOption("@daily");
  await beat(page, 900);
  await bottomPanelTop(page, 330);
  await hideMinimap(page);
  await fitView(page);
  await glideClick(page, page.getByRole("tab", { name: /^Code/ }).first());
  await expect(page.getByText("with DAG(").first()).toBeVisible({ timeout: 20_000 });
  await beat(page, 600);
  await page
    .locator(".cm-editor")
    .filter({ hasText: "Generated by AutoAir" })
    .last()
    .evaluate((el) => {
      const s = el.querySelector(".cm-scroller");
      const line = [...el.querySelectorAll(".cm-line")].find((l) => l.textContent?.includes("with DAG("));
      if (s && line) s.scrollTo({ top: s.scrollTop + line.getBoundingClientRect().top - s.getBoundingClientRect().top - 8, behavior: "smooth" });
    });
  await beat(page, 1500);
  await poster();
  await beat(page, 1500);
}

const BASE_CODE = `import datetime

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

with DAG(
    dag_id="orders_daily",
    schedule="@daily",
    start_date=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
) as dag:
    extract = BashOperator(task_id="extract", bash_command="echo extract")
    load = BashOperator(task_id="load", bash_command="echo load")
    extract >> load
`;

/** code-sync: edit the generated file in code mode, apply it, the graph follows. */
async function codeSync(page, { poster, markStart }) {
  await openApp(page);
  markStart();
  await beat(page, 600);
  await glideClick(page, page.getByTestId("mode-code"));
  await expect(page.getByTestId("code-mode")).toBeVisible();
  const editor = page.locator("[data-code-mode-editor] .cm-content");
  await editor.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.insertText(BASE_CODE);
  await expect(page.getByTestId("code-status")).toContainText(/ready/, { timeout: 60_000 });
  await beat(page, 900);
  await glideClick(page, page.getByTestId("code-apply"));
  await beat(page, 900);
  await glideClick(page, page.getByTestId("mode-visual"));
  await expect(page.locator(".react-flow__node")).toHaveCount(2);
  await fitView(page, { zoomOut: 1 });
  await beat(page, 1600);

  await glideClick(page, page.getByTestId("mode-code"));
  await editor.waitFor();
  await beat(page, 500);
  // The file is AutoAir's own rendering now: add a task after t_load and chain it.
  const line = (text) => page.locator("[data-code-mode-editor] .cm-line").filter({ hasText: text }).first();
  await glideClick(page, line("t_load = BashOperator"));
  await page.keyboard.press("End");
  await page.keyboard.press("Enter");
  // the editor auto-indents by its own unit (2): top up to the file's 4 spaces
  const indent = await page.evaluate(() => {
    const lines = [...document.querySelectorAll("[data-code-mode-editor] .cm-line")];
    const i = lines.findIndex((l) => l.textContent?.includes("t_load = BashOperator"));
    return (lines[i + 1]?.textContent ?? "").length;
  });
  await page.keyboard.type(" ".repeat(Math.max(0, 4 - indent)));
  await page.keyboard.type('t_notify = BashOperator(task_id="notify", bash_command="echo orders loaded")', {
    delay: 40,
  });
  await beat(page, 400);
  await glideClick(page, line("t_extract >> t_load"));
  await page.keyboard.press("End");
  await page.keyboard.type(" >> t_notify", { delay: 90 });
  await beat(page, 400);
  await expect(page.getByTestId("code-status")).toContainText(/ready/, { timeout: 60_000 });
  await beat(page, 1200);
  await poster();
  await glideClick(page, page.getByTestId("code-apply"));
  await beat(page, 900);
  await glideClick(page, page.getByTestId("mode-visual"));
  await expect(page.locator(".react-flow__node")).toHaveCount(3);
  await beat(page, 500);
  await glideClick(page, page.getByRole("button", { name: "Auto layout" }));
  await beat(page, 800);
  await fitView(page, { zoomOut: 1 });
  await beat(page, 2500);
}

/** deploy-review: change a DAG, deploy it as a pull request with the context preview, review it. */
async function deployReview(page, { poster, markStart }) {
  dropPendingBranches(); // a re-run starts without the pull request of the previous run
  await openApp(page, { dag: SHOWCASE });
  await page.getByRole("button", { name: /^Hide palette/ }).click();
  await fitView(page);
  markStart();
  await beat(page, 600);
  await glideClick(page, node(page, "load_warehouse"));
  await glideClick(page, page.getByRole("tab", { name: "Execution", exact: true }));
  const retries = page.locator('[data-autoair-field="retries"] input').first();
  await glideClick(page, retries);
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.type("5", { delay: 120 });
  await retries.press("Tab");
  await beat(page, 800);

  await glideClick(page, page.getByRole("button", { name: "More deploy options" }));
  await beat(page, 500);
  await glideClick(page, page.getByRole("menuitem", { name: "Deploy…" }));
  const dialog = page.getByRole("dialog");
  await expect(page.getByTestId("deploy-governance")).toBeVisible({ timeout: 15_000 });
  await beat(page, 600);
  await glideClick(page, dialog.getByRole("textbox", { name: "Message" }));
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.type("Retry the warehouse load five times", { delay: 30 });
  await glideClick(page, dialog.getByTestId("pr-description"));
  await page.keyboard.type("Transient connection drops hit the nightly load twice last week.", { delay: 15 });
  await glideClick(page, dialog.getByTestId("context-preview-toggle"));
  await expect(dialog.getByTestId("commit-preview")).toContainText("AutoAir deploy context", { timeout: 15_000 });
  await beat(page, 600);
  await dialog.getByTestId("commit-preview").evaluate((el) => el.scrollIntoView({ block: "center", behavior: "smooth" }));
  await beat(page, 1500);
  await poster();
  await toasts(page, true);
  await glideClick(page, dialog.getByRole("button", { name: "Deploy", exact: true }));
  await expect(dialog).toBeHidden({ timeout: 60_000 });
  await beat(page, 1500);

  await bottomPanelTop(page, 420);
  await fitView(page);
  await glideClick(page, page.getByRole("tab", { name: /^Versions/ }).first());
  await glideClick(page, page.getByRole("tab", { name: "Reviews" }));
  await beat(page, 800);
  const comment = page.getByTestId("reviews-panel").getByRole("textbox").first();
  await glideClick(page, comment);
  await page.keyboard.type("LGTM, five retries with the 60 s rule for dropped connections.", { delay: 15 });
  await glideClick(page, page.getByTestId("reviews-panel").getByRole("button", { name: "Approve" }));
  await beat(page, 2500);
}

/**
 * collaboration: two real browser windows of the app side by side in one recording — a
 * wrapper page with two iframes on different origins (127.0.0.1 and localhost), so each
 * has its own storage and user name, both connected to the same co-editing room.
 */
async function collaboration(page, { poster, context, markStart }) {
  const left = `${UI}/?dag=${SHOWCASE}`;
  const right = left.replace("127.0.0.1", "localhost");
  await context.addInitScript(() => {
    try {
      localStorage.setItem("autoair.collab.name", location.hostname === "localhost" ? "Bob Roe" : "Alice Doe");
    } catch {
      /* top-level about:blank */
    }
  });
  await page.setContent(`<!doctype html><html><head><style>
    html,body{margin:0;height:100%;background:#e9ecf1;font:600 13px system-ui,sans-serif;color:#334}
    .row{display:flex;gap:8px;height:100%;padding:8px;box-sizing:border-box}
    .win{flex:1;display:flex;flex-direction:column;border-radius:8px;overflow:hidden;box-shadow:0 2px 10px #0002;background:#fff}
    .bar{padding:6px 10px;background:#f6f7f9;border-bottom:1px solid #dde}
    iframe{flex:1;border:0;width:100%}
  </style></head><body><div class="row">
    <div class="win"><div class="bar">Alice — window 1</div><iframe id="a" src="${left}"></iframe></div>
    <div class="win"><div class="bar">Bob — window 2</div><iframe id="b" src="${right}"></iframe></div>
  </div></body></html>`);
  const a = page.frameLocator("#a");
  const b = page.frameLocator("#b");
  for (const f of [a, b]) {
    await f.locator(".react-flow__node").first().waitFor({ timeout: 60_000 });
  }
  markStart();
  await beat(page, 600);
  for (const f of [a, b]) {
    // narrow windows (the inspector folds away by itself): hide palette and bottom panel
    for (const [key, shown] of [
      ["b", /^Show palette/],
      ["j", /^Show bottom panel/],
    ]) {
      for (let i = 0; i < 3 && !(await f.getByRole("button", { name: shown }).isVisible()); i++) {
        await f.locator(".react-flow__pane").first().click({ position: { x: 300, y: 40 } });
        await page.keyboard.press(`ControlOrMeta+${key}`);
        await beat(page, 500);
      }
    }
    await beat(page, 500);
    await f.getByRole("button", { name: "Fit view" }).click();
    await glideClick(page, f.getByRole("button", { name: "Collaborate" }));
    await glideClick(page, f.getByRole("dialog").getByRole("button", { name: "Collaborate" }));
    await expect(f.getByTestId("collab-status")).toHaveAttribute("data-status", "connected", { timeout: 20_000 });
    await page.keyboard.press("Escape");
    await beat(page, 500);
  }
  await expect(a.getByTestId("collab-peer")).toBeVisible({ timeout: 15_000 });
  for (const f of [a, b]) await f.getByRole("button", { name: "Fit view" }).click();
  await beat(page, 1000);

  // Bob points at the graph and selects a task; Alice sees his cursor and selection.
  await glideClick(page, b.locator(".react-flow__node").filter({ hasText: "build_daily_sales" }));
  const lb = await b.locator(".react-flow__node").filter({ hasText: "load_warehouse" }).boundingBox();
  await page.mouse.move(lb.x + lb.width / 2, lb.y + lb.height + 50, { steps: 25 });
  await beat(page, 1200);

  // Alice moves notify_team; Bob's window follows.
  const n = await a.locator(".react-flow__node").filter({ hasText: "notify_team" }).boundingBox();
  await page.mouse.move(n.x + n.width / 2, n.y + n.height / 2, { steps: 25 });
  await page.mouse.down();
  await page.mouse.move(n.x + n.width / 2 - 30, n.y + n.height / 2 + 110, { steps: 35 });
  await page.mouse.up();
  await beat(page, 1500);
  await poster();

  // Bob renames a task in his window (inspector opened for the edit).
  await glideClick(page, b.locator(".react-flow__node").filter({ hasText: "build_daily_sales" }));
  await page.keyboard.press("ControlOrMeta+i");
  await beat(page, 600);
  const field = b.getByRole("textbox", { name: /^Task id/ }).first();
  await glideClick(page, field);
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.type("build_sales_report", { delay: 90 });
  await field.press("Tab");
  await expect(a.locator(".react-flow__node").filter({ hasText: "build_sales_report" })).toBeVisible({ timeout: 10_000 });
  await beat(page, 2500);
}

export const VIDEO_SCENES = [
  { id: "build-dag", run: ({ browser, token }) => record(browser, token, "build-dag", buildDag) },
  { id: "code-sync", run: ({ browser, token }) => record(browser, token, "code-sync", codeSync) },
  { id: "deploy-review", run: ({ browser, token }) => record(browser, token, "deploy-review", deployReview) },
  { id: "collaboration", run: ({ browser, token }) => record(browser, token, "collaboration", collaboration) },
];
