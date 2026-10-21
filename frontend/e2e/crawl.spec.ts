import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { type Page, expect, test } from "@playwright/test";

import { DeployedDags } from "./cleanup";

/**
 * UI crawler: opens every toolbar menu and menu item (and the dialogs they open, with all their
 * tabs), every inspector / settings / bottom-panel tab, every menu and select inside panels,
 * context menus, the command palette and the shortcuts sheet — for several feature-rich DAGs,
 * in the Airflow plugin and in standalone mode. Any page error or console error fails the test.
 */
const STANDALONE = process.env.AUTOAIR_STANDALONE_URL ?? "http://127.0.0.1:8765";
const LANG = process.env.CRAWL_LANG ?? "en";
const DEV = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";
const SPECS = ["etl_full", "v2_features", "e2e_pipeline", "loops_34"];
const SUFFIX = Date.now().toString(36);
// Items whose action is irreversible or leaves the page; their dialogs/confirms are still opened.
// Signing out, switching the language or hiding panels (remembered across the reloads) would
// derail the rest of the crawl.
const NO_CLICK =
  /^(Open in Airflow|Report issue|Sign out|Çıkış yap|English|Türkçe|(Hide|Show) (palette|inspector|bottom panel))/i;

type Target = { name: string; base: string; open: (dagId: string) => string; api: string };
const TARGETS: Target[] = [
  { name: "plugin", base: "", open: (id) => `/plugin/dag-builder#dag=${id}`, api: "/autoair/api" },
  { name: "standalone", base: STANDALONE, open: (id) => `${STANDALONE}/ui/?dag=${id}`, api: `${STANDALONE}/api` },
  // Vite dev server (proxying to the standalone API): React's development warnings are errors here.
  { name: "dev", base: DEV, open: (id) => `${DEV}/?dag=${id}`, api: `${STANDALONE}/api` },
];

const step = {
  current: "load",
  get label() {
    return this.current;
  },
  set label(v: string) {
    this.current = v;
    if (process.env.CRAWL_DEBUG) console.log(`step: ${v}`);
  },
};

test.use({ actionTimeout: 5000, navigationTimeout: 30000 });

function track(page: Page): string[] {
  const errors: string[] = [];
  const add = (msg: string) => {
    errors.push(msg);
    if (process.env.CRAWL_DEBUG) console.log(`ERR ${msg}`);
  };
  page.on("pageerror", (e) =>
    add(`[${step.label}] pageerror: ${e.message}\n${e.stack?.split("\n").slice(0, 6).join("\n")}`),
  );
  page.on("response", (r) => {
    if (r.status() >= 400) add(`[${step.label}] http ${r.status()} ${r.request().method()} ${r.url()}`);
  });
  page.on("console", (m) => {
    if (m.type() === "error") add(`[${step.label}] console: ${m.text().slice(0, 1500)}`);
  });
  // Leaving with unsaved work asks first (beforeunload): accept it; dismiss everything else.
  page.on("dialog", (d) => void (d.type() === "beforeunload" ? d.accept() : d.dismiss()));
  return errors;
}

/** An interaction the crawler could not perform is noted, not fatal (errors are what count). */
async function soft(page: Page, fn: () => Promise<unknown>) {
  try {
    await fn();
  } catch (e) {
    test.info().annotations.push({ type: "skipped-action", description: `${step.label}: ${String(e).slice(0, 200)}` });
    await closeOverlays(page);
  }
}

async function settle(page: Page) {
  await page.waitForTimeout(250);
}

async function closeOverlays(page: Page) {
  for (let i = 0; i < 4; i++) {
    const open = page.locator('[role="dialog"]:visible, [role="menu"]:visible, [role="listbox"]:visible');
    if (!(await open.count())) return;
    await page.keyboard.press("Escape");
    await settle(page);
  }
}

/** Clicks every visible tab (re-querying, as tabs can appear/disappear). */
async function clickTabs(page: Page, scope = page.locator("body"), limit = 60) {
  const seen = new Set<string>();
  for (let n = 0; n < limit; n++) {
    const tabs = scope.locator('[role="tab"]:visible');
    const count = await tabs.count();
    let clicked = false;
    for (let i = 0; i < count; i++) {
      const tab = tabs.nth(i);
      const key = `${(await tab.getAttribute("id")) ?? ""}|${(await tab.textContent())?.trim()}`;
      if (seen.has(key) || (await tab.isDisabled())) continue;
      seen.add(key);
      await soft(page, () => tab.click());
      await settle(page);
      clicked = true;
      break;
    }
    if (!clicked) return;
  }
}

/** Opens every menu trigger and select/combobox inside `scope`, then closes it. */
async function poke(page: Page, scope = page.locator("body")) {
  const triggers = scope.locator(
    '[aria-haspopup="menu"]:visible, [role="combobox"]:visible, [aria-haspopup="listbox"]:visible',
  );
  const count = Math.min(await triggers.count(), 40);
  for (let i = 0; i < count; i++) {
    const t = triggers.nth(i);
    if (!(await t.isVisible()) || (await t.isDisabled())) continue;
    await t.click({ timeout: 2000 }).catch(() => undefined);
    await settle(page);
    await page.keyboard.press("Escape");
    await settle(page);
  }
}

/** Picks each option (up to `perSelect`) of every select/combobox in `scope`. */
async function pickOptions(page: Page, scope = page.locator("body"), perSelect = 8) {
  if (!process.env.CRAWL_INPUTS) return;
  const triggers = scope.locator('[role="combobox"]:visible, [aria-haspopup="listbox"]:visible');
  const count = Math.min(await triggers.count(), 25);
  for (let i = 0; i < count; i++) {
    for (let o = 0; o < perSelect; o++) {
      const trigger = scope.locator('[role="combobox"]:visible, [aria-haspopup="listbox"]:visible').nth(i);
      if (!(await trigger.count()) || (await trigger.isDisabled().catch(() => true))) break;
      const opened = await trigger
        .click({ timeout: 1500 })
        .then(() => true)
        .catch(() => false);
      if (!opened) break;
      await settle(page);
      const options = page.locator('[role="option"]:visible').filter({ hasNot: page.locator("input") });
      const n = await options.count();
      if (o >= n) {
        await page.keyboard.press("Escape");
        break;
      }
      await soft(page, () => options.nth(o).click({ timeout: 1500 }));
      await settle(page);
      await closeOverlays(page);
    }
  }
}

/** Types into every visible field and toggles every switch in `scope` (exercises onChange paths). */
async function exerciseInputs(page: Page, scope = page.locator("body"), limit = 40) {
  if (!process.env.CRAWL_INPUTS) return;
  const fields = scope.locator(
    'input:visible:not([type="file"]):not([type="checkbox"]):not([type="radio"]):not([readonly]):not([disabled]), textarea:visible:not([disabled])',
  );
  const n = Math.min(await fields.count(), limit);
  for (let i = 0; i < n; i++) {
    const f = fields.nth(i);
    const type = (await f.getAttribute("type")) ?? "text";
    const role = await f.getAttribute("role");
    if (role === "combobox") continue; // pickers are poked separately
    const value = type === "number" ? "3" : "x1";
    await soft(page, async () => {
      await f.fill(value, { timeout: 1500 });
      await f.press("Tab", { timeout: 1500 });
    });
    await page.waitForTimeout(80);
  }
  const switches = scope.locator('[role="switch"]:visible, [data-scope="switch"][data-part="root"]:visible');
  const m = Math.min(await switches.count(), 20);
  for (let i = 0; i < m; i++) await soft(page, () => switches.nth(i).click({ timeout: 1500 }));
  const editors = scope.locator(".cm-content:visible");
  const k = Math.min(await editors.count(), 8);
  for (let i = 0; i < k; i++) {
    await soft(page, async () => {
      await editors.nth(i).click({ timeout: 1500 });
      await page.keyboard.type("{{ ds }}", { delay: 5 });
      await page.keyboard.press("Escape");
    });
  }
  await closeOverlays(page);
}

async function crawlDialog(page: Page) {
  const dialog = page.getByRole("dialog").last();
  if (!(await dialog.isVisible().catch(() => false))) return;
  await clickTabs(page, dialog);
  const segments = dialog.locator('[data-scope="segment-group"][data-part="item"]:visible');
  for (let i = 0; i < (await segments.count()); i++) {
    await soft(page, () => segments.nth(i).click());
    await settle(page);
    await clickTabs(page, dialog);
  }
  await poke(page, dialog);
}

async function crawlMenus(page: Page, reload: () => Promise<void>) {
  const home = page.url();
  const homeNodes = await page.locator(".react-flow__node").count();
  const header = page.locator("header").first();
  const triggers = header.locator('[aria-haspopup="menu"]');
  const menus = await triggers.count();
  for (let m = 0; m < menus; m++) {
    step.label = `menu ${m}`;
    await soft(page, () => triggers.nth(m).click());
    await settle(page);
    const names = (await page.getByRole("menuitem").filter({ visible: true }).allTextContents()).map((s) => s.trim());
    await page.keyboard.press("Escape");
    for (const name of names) {
      if (!name || NO_CLICK.test(name)) continue;
      step.label = `menu ${m}: ${name}`;
      await soft(page, () => triggers.nth(m).click());
      await settle(page);
      const item = page.getByRole("menuitem", { name, exact: true }).filter({ visible: true }).first();
      if (!(await item.count()) || (await item.getAttribute("data-disabled")) !== null) {
        await page.keyboard.press("Escape");
        continue;
      }
      const sub = (await item.getAttribute("aria-haspopup")) === "menu";
      await soft(page, () => item.click());
      await page.waitForTimeout(600);
      if (sub) {
        const subItems = page.getByRole("menuitem").filter({ visible: true });
        test.info().annotations.push({ type: "submenu", description: `${name}: ${await subItems.count()}` });
      }
      await crawlDialog(page);
      await closeOverlays(page);
      // Only reload when the action changed the document or navigated away.
      const nodes = await page.locator(".react-flow__node").count();
      if (page.url() !== home || nodes !== homeNodes || (await page.getByText("unsaved").count())) await reload();
    }
  }
}

async function crawlCanvas(page: Page) {
  const nodes = page.locator(".react-flow__node");
  const count = await nodes.count();
  for (let i = 0; i < count; i++) {
    const node = nodes.nth(i);
    step.label = `node ${i}`;
    if (!(await node.isVisible())) continue;
    await soft(page, () => node.click({ position: { x: 12, y: 8 } }));
    await settle(page);
    await clickTabs(page);
    await poke(page, page.locator('[data-panel-id="inspector"], [id*="inspector"]').first());
    await exerciseInputs(page, page.locator('[data-panel-id="inspector"], [id*="inspector"]').first());
    await pickOptions(page, page.locator('[data-panel-id="inspector"], [id*="inspector"]').first());
    await soft(page, () => node.click({ button: "right", position: { x: 12, y: 8 } }));
    await settle(page);
    await closeOverlays(page);
  }
  step.label = "edge/pane/settings/palette";
  const edge = page.locator(".react-flow__edge").first();
  if (await edge.count()) {
    await soft(page, () => edge.click({ button: "right", force: true }));
    await settle(page);
    await closeOverlays(page);
  }
  await soft(page, () =>
    page
      .locator(".react-flow__pane")
      .first()
      .click({ button: "right", position: { x: 20, y: 20 } }),
  );
  await settle(page);
  await closeOverlays(page);
  await soft(page, () =>
    page
      .locator(".react-flow__pane")
      .first()
      .click({ position: { x: 20, y: 20 } }),
  );
  await settle(page);
  await clickTabs(page); // DAG settings tabs, bottom panel tabs, palette tabs
  await poke(page);
  // DAG settings: every tab, every field, every option (schedule types, params, alerts…).
  const settings = page.locator('[data-panel-id="inspector"], [id*="inspector"]').first();
  for (const name of ["General", "Schedule", "Params", "Defaults", "Alerts", "Environments"]) {
    step.label = `dag settings: ${name}`;
    await soft(page, () => settings.getByRole("tab", { name, exact: true }).first().click({ timeout: 1500 }));
    await settle(page);
    await pickOptions(settings.page(), settings);
    await exerciseInputs(page, settings);
  }
  await closeOverlays(page);
  await page.keyboard.press("ControlOrMeta+k");
  await settle(page);
  await page.keyboard.type("lay");
  await settle(page);
  await closeOverlays(page);
  await page.keyboard.press("Shift+?");
  await settle(page);
  await closeOverlays(page);
}

// Every crawl_* DAG is deleted again after the crawl (best effort; leftovers are logged).
const deployedDags = new DeployedDags("crawl");
test.afterAll(async ({ request }) => {
  await deployedDags.cleanup(request);
});

for (const target of TARGETS) {
  test.describe(`crawl · ${target.name}`, () => {
    for (const name of SPECS) {
      test(name, async ({ page, request }) => {
        test.setTimeout(600_000);
        const health = await request.get(`${target.api}/health`).catch(() => null);
        test.skip(!health?.ok(), `${target.name} server not running`);
        if (target.name === "dev") {
          const dev = await request.get(DEV).catch(() => null);
          test.skip(!dev?.ok(), "vite dev server not running");
        }
        const token = (await (await request.get("/auth/token")).json()).access_token as string;
        const spec = JSON.parse(
          readFileSync(resolve(process.cwd(), `../tests/golden/specs/${name}.autoair.json`), "utf8"),
        );
        const dagId = `crawl_${name}_${target.name}_${SUFFIX}`;
        const headers = { Authorization: `Bearer ${token}` };
        const put = await request.put(`${target.api}/specs/${dagId}`, {
          headers,
          data: { spec: { ...spec, dag_id: dagId }, force: true },
        });
        deployedDags.track(target.api, dagId, headers);
        expect(put.ok(), await put.text()).toBeTruthy();
        // Specs the server refuses to deploy (e.g. task loops on Airflow < 3.4) are opened
        // through File → Import → AutoAir spec instead, as an unsaved document.
        const deployed = (await put.json()).deployed as boolean;
        const specFile = resolve(process.cwd(), `../tests/golden/specs/${name}.autoair.json`);

        const errors = track(page);
        await page.addInitScript(
          ([t, lang]) => {
            try {
              sessionStorage.setItem("autoair.airflowToken", t);
              localStorage.setItem("autoair.lang", lang);
            } catch {
              /* about:blank has no storage */
            }
          },
          [token, LANG] as const,
        );
        const reload = async () => {
          await page.goto("about:blank");
          if (deployed) {
            await page.goto(target.open(dagId));
          } else {
            await page.goto(target.open("").replace(/[?#]dag=$/, ""));
            // The dev server may need a while to serve the app's modules.
            await expect(page.locator("header").first()).toBeVisible({ timeout: 30_000 });
            await page.getByRole("button", { name: "File", exact: true }).click();
            await page.getByRole("menuitem", { name: "Import" }).click();
            await page.getByRole("menuitem", { name: "From AutoAir spec (JSON)…" }).click();
            const dialog = page.getByRole("dialog");
            await dialog.locator('input[type="file"]').setInputFiles(specFile);
            await dialog.getByRole("button", { name: "Import", exact: true }).click();
            await dialog.getByRole("button", { name: "Open as unsaved" }).click();
          }
          await expect(page.locator(".react-flow__node").first()).toBeVisible({ timeout: 30_000 });
          await settle(page);
        };
        await reload();
        await crawlCanvas(page);
        await reload();
        await crawlMenus(page, reload);
        expect(errors, errors.join("\n")).toEqual([]);
      });
    }
  });
}

/** Search terms for operators/sensors that exercise special inspectors (HITL forms, sensors…). */
const EXTRA_OPERATORS = [
  "HITLOperator",
  "ApprovalOperator",
  "HITLBranchOperator",
  "HITLEntryOperator",
  "FileSensor",
  "DateTimeSensor",
  "TimeDeltaSensor",
  "ExternalTaskSensor",
  "TriggerDagRunOperator",
  "BranchDayOfWeekOperator",
  "BashOperator",
  "PythonOperator",
  "SQLExecuteQueryOperator",
  "HttpOperator",
  "SmtpNotifier",
  "LatestOnlyOperator",
];

for (const target of TARGETS.filter((t) => t.name !== "plugin")) {
  test(`crawl · ${target.name} · every palette node kind`, async ({ page, request }) => {
    test.setTimeout(900_000);
    const health = await request.get(`${target.api}/health`).catch(() => null);
    test.skip(!health?.ok(), `${target.name} server not running`);
    if (target.name === "dev") {
      const dev = await request.get(DEV).catch(() => null);
      test.skip(!dev?.ok(), "vite dev server not running");
    }
    const token = (await (await request.get("/auth/token")).json()).access_token as string;
    const errors = track(page);
    await page.addInitScript(
      ([t, lang]) => {
        try {
          sessionStorage.setItem("autoair.airflowToken", t);
          localStorage.setItem("autoair.lang", lang);
        } catch {
          /* about:blank has no storage */
        }
      },
      [token, "en"] as const,
    );
    await page.goto(target.open("").replace(/[?#]dag=$/, ""));
    const search = page.getByPlaceholder(/Search operators/);
    await expect(search).toBeVisible({ timeout: 30_000 });

    // Built-in kinds and templates (the "Built-in" tab: few rows, all rendered — the list is
    // virtualized), then catalog operators by name.
    await page.getByRole("tab", { name: "Built-in", exact: true }).first().click();
    await expect(page.locator('[role="option"]').first()).toBeVisible();
    await page.waitForTimeout(300);
    const labels: string[] = [];
    const options = page.locator('[role="option"]');
    for (let i = 0; i < (await options.count()); i++) {
      const name = (await options.nth(i).locator("p").first().textContent())?.trim();
      if (name) labels.push(name);
    }
    expect(labels.length).toBeGreaterThan(8);
    const insert = async (label: string, query: string) => {
      step.label = `palette: ${label}`;
      // Tab crawling leaves the palette on another filter (e.g. "Recent"): reset it.
      await soft(page, () => page.getByRole("tab", { name: "All", exact: true }).first().click());
      await search.fill(query);
      await settle(page);
      const row = page.locator('[role="option"]', { hasText: label }).first();
      if (!(await row.count())) {
        if (process.env.CRAWL_DEBUG) console.log(`not in catalog: ${label}`);
        test.info().annotations.push({ type: "not-in-catalog", description: label });
        return;
      }
      const before = await page.locator(".react-flow__node").count();
      await soft(page, () => row.dblclick());
      await settle(page);
      const nodes = page.locator(".react-flow__node");
      const after = await nodes.count();
      if (process.env.CRAWL_DEBUG) console.log(`inserted ${label}: ${before} -> ${after}`);
      if (after <= before) return;
      await soft(page, () => nodes.last().click({ position: { x: 12, y: 8 } }));
      await settle(page);
      await clickTabs(page);
      await poke(page, page.locator('[data-panel-id="inspector"], [id*="inspector"]').first());
      await exerciseInputs(page, page.locator('[data-panel-id="inspector"], [id*="inspector"]').first());
      await closeOverlays(page);
    };
    for (const label of labels.slice(0, 40)) await insert(label, "");
    for (const op of EXTRA_OPERATORS) await insert(op, op);

    // Whole-document views with every kind present.
    step.label = "palette: document views";
    await soft(page, () =>
      page
        .locator(".react-flow__pane")
        .first()
        .click({ position: { x: 5, y: 5 } }),
    );
    await clickTabs(page);
    await page.keyboard.press("ControlOrMeta+a");
    await settle(page);
    await closeOverlays(page);
    expect(errors, errors.join("\n")).toEqual([]);
  });
}
