// Browser helpers shared by the screen and video scenes.
import { mkdirSync } from "node:fs";
import { homedir, hostname } from "node:os";
import { createRequire } from "node:module";
import { resolve } from "node:path";

import { SCREENS, STATE, UI, WEBSITE } from "./env.mjs";

const require = createRequire(resolve(WEBSITE, "package.json"));
export const { chromium, expect } = require("@playwright/test");

export const VIEWPORT = { width: 1440, height: 900 };

/** Noise hidden in every capture: toasts (unless a scene opts in) and the text caret. */
const NOISE_CSS = `
  html[data-capture-toasts="off"] [data-scope="toast"] { display: none !important; }
  * { caret-color: transparent !important; }
`;

/**
 * A fresh browser context (own storage) in `mode`, signed in with `token` unless
 * `token` is null. The preview build gets its API base the way the server would inject it.
 */
export async function newPage(browser, { mode = "light", token, recordVideo, viewport = VIEWPORT, name } = {}) {
  const context = await browser.newContext({
    viewport,
    colorScheme: mode,
    deviceScaleFactor: 1,
    locale: "en-US",
    timezoneId: "Europe/Istanbul",
    recordVideo,
  });
  await context.addInitScript(
    ([state, home, host]) => {
      // Keep local paths / the host name out of every frame (also mid-video): see sanitize().
      const fix = (s) =>
        s && (s.includes(state) || s.includes(home) || s.includes(host))
          ? s.split(state).join("/srv/autoair").split(home).join("/home/user").split(host).join("autoair-host")
          : s;
      const sweep = (root) => {
        const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        for (let n = walk.nextNode(); n; n = walk.nextNode()) {
          const v = fix(n.nodeValue);
          if (v !== n.nodeValue) n.nodeValue = v;
        }
      };
      const start = () => {
        sweep(document.body);
        new MutationObserver((records) => {
          for (const r of records) {
            if (r.type === "characterData") {
              const v = fix(r.target.nodeValue);
              if (v !== r.target.nodeValue) r.target.nodeValue = v;
            } else for (const n of r.addedNodes) n.nodeType === 3 ? (n.nodeValue = fix(n.nodeValue)) : n.nodeType === 1 && sweep(n);
          }
        }).observe(document.body, { subtree: true, childList: true, characterData: true });
        setInterval(() => {
          for (const el of document.querySelectorAll("input, textarea")) {
            const v = fix(el.value);
            if (v !== el.value) el.value = v;
          }
        }, 250);
      };
      if (document.body) start();
      else document.addEventListener("DOMContentLoaded", start);
    },
    [STATE, homedir(), hostname()],
  );
  await context.addInitScript(
    ([t, m, n, css]) => {
      window.__AUTOAIR_CONFIG__ = { apiBase: "/api" };
      try {
        if (t) sessionStorage.setItem("autoair.airflowToken", t);
        localStorage.setItem("autoair.lang", "en");
        localStorage.setItem("autoair.colorMode", m);
        localStorage.removeItem("autoair.draft.v1");
        if (n) localStorage.setItem("autoair.collab.name", n);
      } catch {
        /* storage unavailable */
      }
      const add = () => {
        const s = document.createElement("style");
        s.textContent = css;
        document.head.appendChild(s);
        document.documentElement.dataset.captureToasts ??= "off";
      };
      if (document.head) add();
      else document.addEventListener("DOMContentLoaded", add);
    },
    [token ?? null, mode, name ?? null, NOISE_CSS],
  );
  const page = await context.newPage();
  page.on("pageerror", (e) => console.warn(`    pageerror: ${e.message.slice(0, 200)}`));
  return { context, page };
}

export async function toasts(page, on) {
  await page.evaluate((v) => (document.documentElement.dataset.captureToasts = v), on ? "on" : "off");
}

/** Opens the builder (optionally a saved DAG) and waits until it is usable. */
export async function openApp(page, { dag, path = "/" } = {}) {
  await page.goto(`${UI}${path}${dag ? `?dag=${dag}` : ""}`);
  await page.getByTestId("toolbar-dag-id").waitFor({ timeout: 60_000 });
  if (dag) await page.locator(".react-flow__node").first().waitFor({ timeout: 60_000 });
  await idle(page);
}

/** No running animation, no visible spinner, network quiet. */
async function idle(page, { spinners = true } = {}) {
  await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});
  if (spinners) {
    await page
      .waitForFunction(
        () =>
          // (the toolbar's "not in Airflow yet" status spins for as long as that is true)
          [...document.querySelectorAll('.chakra-spinner, [class*="spinner"], [role="progressbar"]')].every(
            (el) => !(el instanceof HTMLElement) || el.offsetParent === null || el.closest('[role="banner"]'),
          ),
        null,
        { timeout: 20_000 },
      )
      .catch(() => {});
  }
  await page
    .waitForFunction(() => document.getAnimations().every((a) => a.playState !== "running"), null, {
      timeout: 5_000,
    })
    .catch(() => {});
  await page.evaluate(() => document.fonts.ready);
}

/** Replaces local paths (the scratch git repo lives under the user's home) in the DOM. */
export async function sanitize(page) {
  await page.evaluate(
    ([state, home, host]) => {
      const fix = (s) =>
        s.split(state).join("/srv/autoair").split(home).join("/home/user").split(host).join("autoair-host");
      const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let n = walk.nextNode(); n; n = walk.nextNode()) {
        const v = fix(n.nodeValue ?? "");
        if (v !== n.nodeValue) n.nodeValue = v;
      }
      for (const el of document.querySelectorAll("input, textarea")) {
        const v = fix(el.value);
        if (v !== el.value) el.value = v;
      }
      for (const el of document.querySelectorAll("[title], [aria-label]")) {
        for (const a of ["title", "aria-label"]) {
          const cur = el.getAttribute(a);
          if (cur && fix(cur) !== cur) el.setAttribute(a, fix(cur));
        }
      }
    },
    [STATE, homedir(), hostname()],
  );
}

/** A screenshot into static/img/screens/<name>-<mode>.png. */
export async function shot(page, name, mode, { settle = true, clip } = {}) {
  if (settle) await idle(page);
  await sanitize(page);
  mkdirSync(SCREENS, { recursive: true });
  const path = resolve(SCREENS, `${name}-${mode}.png`);
  await page.screenshot({ path, animations: "disabled", caret: "hide", clip });
  console.log(`    ✓ ${name}-${mode}.png`);
}

// --- builder interactions -------------------------------------------------------------

export const node = (page, text) => page.locator(".react-flow__node").filter({ hasText: text }).first();

/** Clicks the empty canvas (deselects: the inspector shows the DAG settings). */
export async function clickPane(page, position = { x: 20, y: 20 }) {
  await page.locator(".react-flow__pane").first().click({ position });
  await page.waitForTimeout(200);
  await inspectorTop(page);
}

export async function fitView(page, { zoomOut = 0 } = {}) {
  await page.getByRole("button", { name: "Fit view" }).click();
  await page.waitForTimeout(500);
  for (let i = 0; i < zoomOut; i++) {
    await page.getByRole("button", { name: "Zoom out" }).click();
    await page.waitForTimeout(250);
  }
}

export async function autoLayout(page) {
  await page.getByRole("button", { name: "Auto layout" }).click();
  await page.waitForTimeout(900);
}

/** Inserts an operator from the palette (double click on its row), returns its node. */
export async function addOperator(page, query, name = query) {
  if (!(await page.getByPlaceholder(/Search operators/).isVisible())) {
    await page.getByRole("button", { name: /^Show palette/ }).click();
    await page.waitForTimeout(300);
  }
  await page.getByPlaceholder(/Search operators/).fill(query);
  const row = page
    .locator('[role="option"]')
    .filter({ has: page.getByText(name, { exact: true }) })
    .first();
  await row.waitFor();
  await row.dblclick();
  await page.waitForTimeout(400);
  await page.getByPlaceholder(/Search operators/).fill("");
}

/** Replaces the text of a CodeMirror field of the inspector. */
export async function fillCode(page, field, text) {
  const editor = page.locator(`[data-autoair-field*="${field}"] .cm-content`).first();
  await editor.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.insertText(text);
}

/** Drags from the right handle of `from` to the left handle of `to`. */
export async function connect(page, from, to, { steps = 12 } = {}) {
  const s = await node(page, from).locator(".react-flow__handle-right").first().boundingBox();
  const t = await node(page, to).locator(".react-flow__handle-left").first().boundingBox();
  if (!s || !t) throw new Error(`cannot connect ${from} -> ${to}`);
  await page.mouse.move(s.x + s.width / 2, s.y + s.height / 2);
  await page.mouse.down();
  await page.mouse.move(t.x + t.width / 2, t.y + t.height / 2, { steps });
  await page.mouse.up();
  await page.waitForTimeout(300);
}

export async function renameTask(page, taskId) {
  const field = page.getByRole("textbox", { name: /^Task id/ }).first();
  await field.fill(taskId);
  await field.press("Tab");
}

export async function menu(page, top, item) {
  await page.getByRole("button", { name: top, exact: true }).click();
  await page.getByRole("menuitem", { name: item }).click();
}

/** A visible pointer for videos (headless recordings have no cursor). */
export async function showCursor(page) {
  await page.addInitScript(() => {
    const install = () => {
      if (document.getElementById("capture-cursor")) return;
      const c = document.createElement("div");
      c.id = "capture-cursor";
      c.style.cssText =
        "position:fixed;left:-40px;top:-40px;width:22px;height:22px;z-index:2147483647;pointer-events:none;" +
        "transition:transform .05s;background:no-repeat center/contain url(\"data:image/svg+xml;utf8," +
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M4 2l15 11-6.5 1.2L9 21z' " +
        "fill='%23111' stroke='white' stroke-width='1.5' stroke-linejoin='round'/></svg>\")";
      document.body.appendChild(c);
      addEventListener(
        "mousemove",
        (e) => {
          c.style.left = `${e.clientX - 3}px`;
          c.style.top = `${e.clientY - 2}px`;
        },
        true,
      );
      addEventListener("mousedown", () => (c.style.transform = "scale(.85)"), true);
      addEventListener("mouseup", () => (c.style.transform = ""), true);
    };
    if (document.body) install();
    else addEventListener("DOMContentLoaded", install);
  });
}

/** Moves the mouse to the centre of `locator` in visible steps and clicks it (videos). */
export async function glideClick(page, locator, { steps = 14, pause = 150, dbl = false, button } = {}) {
  const b = await locator.boundingBox();
  if (!b) throw new Error("glideClick: not visible");
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps });
  await page.waitForTimeout(pause);
  if (dbl) await locator.dblclick();
  else await locator.click({ button });
}

/** Drags the bottom panel's top edge to `y` (viewport px): more room for problems / code. */
export async function bottomPanelTop(page, y) {
  const sep = page.getByRole("separator", { name: /Resize the bottom panel/ });
  const b = await sep.boundingBox();
  if (!b) return;
  const x = b.x + 60;
  await page.mouse.move(x, b.y + b.height / 2);
  await page.mouse.down();
  await page.mouse.move(x, y, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(700); // the canvas resizes after the panel: fit the view after this
}

/** Scrolls the inspector (right panel) back to its top. */
export async function inspectorTop(page) {
  await page.evaluate(() => {
    for (const el of document.querySelectorAll("*")) {
      const r = el.getBoundingClientRect();
      if (r.left > window.innerWidth * 0.6 && el.scrollHeight > el.clientHeight + 4 && el.scrollTop > 0) el.scrollTop = 0;
    }
  });
}

/** Hides the minimap (Shift+M): on a short canvas it would cover the right-hand tasks. */
export async function hideMinimap(page) {
  if (!(await page.locator(".react-flow__minimap").count())) return;
  await page.locator(".react-flow__pane").first().click({ position: { x: 20, y: 20 } });
  await page.keyboard.press("Shift+M");
  await page.waitForTimeout(300);
}
