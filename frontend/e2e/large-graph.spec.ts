import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { type CDPSession, type Page, expect, test } from "@playwright/test";

import { DeployedDags } from "./cleanup";

/**
 * Large-graph performance: opens synthetic DAGs of `scripts/gen_large_dag.py` (500 and 1000
 * tasks by default) and measures load, auto layout, frame rate and long tasks while panning,
 * zooming and dragging a node, JS heap and DOM size, and the live validation round trip. It
 * also checks that undo still reverts a drag on the large graph.
 *
 * Runs against a production build on the private stack (skill run-e2e):
 *
 *   eval "$(.claude/skills/run-e2e/scripts/private-stack.sh start)"
 *   cd frontend && npx playwright test e2e/large-graph.spec.ts --reporter=list
 *
 * Env: PERF_SIZES (default "500,1000"), PERF_LABEL (name of the result file, default "run"),
 * PERF_PROFILE=1 (save a CPU profile of the node drag next to the results).
 * Results: `.artifacts/perf/large-graph-<label>.json` (repo root) and a table on stdout. The
 * budgets asserted here are generous ceilings that catch regressions, not targets.
 */
const DEV = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";
const SIZES = (process.env.PERF_SIZES ?? "500,1000").split(",").map((s) => Number(s.trim()));
const LABEL = process.env.PERF_LABEL ?? "run";
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const SUFFIX = Math.random().toString(36).slice(2, 7);
/** PERF_PROFILE=1: also save a CPU profile of the node drag (Chrome DevTools format). */
const PROFILE = !!process.env.PERF_PROFILE;

type Window = { frames: number[]; long: number[] };
type Measure = { fps: number; worstFrameMs: number; longTasks: number; longTaskMs: number; scriptMs: number };
type Result = Record<string, number | Measure | string>;

function generate(tasks: number, dagId: string, layout = true): unknown {
  const args = ["run", "python", path.join(ROOT, "scripts", "gen_large_dag.py"), "--tasks", String(tasks)];
  args.push("--dag-id", dagId);
  if (!layout) args.push("--no-layout");
  return JSON.parse(execFileSync("uv", args, { cwd: ROOT, encoding: "utf-8", maxBuffer: 64 << 20 }));
}

async function instrument(page: Page, dagId: string, errors: string[]): Promise<void> {
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    // The private stack has no Airflow: its REST calls answer 502, which is not our concern.
    if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) errors.push(m.text());
  });
  await page.addInitScript(
    ([id, token]) => {
      const w = window as unknown as { __AUTOAIR_CONFIG__: object; __perf: Window };
      w.__AUTOAIR_CONFIG__ = { apiBase: "/api", initialDagId: id };
      try {
        localStorage.setItem("autoair.lang", "en");
        if (token) sessionStorage.setItem("autoair.airflowToken", token);
      } catch {
        /* about:blank */
      }
      const perf: Window = { frames: [], long: [] };
      w.__perf = perf;
      try {
        new PerformanceObserver((list) => {
          for (const e of list.getEntries()) perf.long.push(e.duration);
        }).observe({ type: "longtask", buffered: true });
      } catch {
        /* no long task API */
      }
      const tick = (t: number) => {
        perf.frames.push(t);
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    },
    [dagId, TOKEN] as const,
  );
}

async function scriptMs(cdp: CDPSession): Promise<number> {
  const { metrics } = (await cdp.send("Performance.getMetrics")) as { metrics: { name: string; value: number }[] };
  return (metrics.find((m) => m.name === "ScriptDuration")?.value ?? 0) * 1000;
}

async function measure(page: Page, cdp: CDPSession, action: () => Promise<void>): Promise<Measure> {
  await page.evaluate(() => {
    const p = (window as unknown as { __perf: Window }).__perf;
    p.frames.length = 0;
    p.long.length = 0;
  });
  const script0 = await scriptMs(cdp);
  await action();
  // Let the last frames land.
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  const script1 = await scriptMs(cdp);
  const { frames, long } = await page.evaluate(() => (window as unknown as { __perf: Window }).__perf);
  const span = frames.length > 1 ? (frames[frames.length - 1] ?? 0) - (frames[0] ?? 0) : 0;
  let worst = 0;
  for (let i = 1; i < frames.length; i++) worst = Math.max(worst, (frames[i] ?? 0) - (frames[i - 1] ?? 0));
  return {
    fps: span ? Math.round(((frames.length - 1) * 1000) / span) : 0,
    worstFrameMs: Math.round(worst),
    longTasks: long.length,
    longTaskMs: Math.round(long.reduce((a, b) => a + b, 0)),
    scriptMs: Math.round(script1 - script0),
  };
}

async function zoomLevel(page: Page): Promise<number> {
  return page.evaluate(() => {
    const t = document.querySelector<HTMLElement>(".react-flow__viewport")?.style.transform ?? "";
    return Math.round(Number(/scale\(([\d.]+)\)/.exec(t)?.[1] ?? 0) * 100) / 100;
  });
}

async function heapMb(cdp: CDPSession): Promise<number> {
  await cdp.send("HeapProfiler.collectGarbage");
  const { metrics } = (await cdp.send("Performance.getMetrics")) as { metrics: { name: string; value: number }[] };
  return Math.round((metrics.find((m) => m.name === "JSHeapUsedSize")?.value ?? 0) / 1e5) / 10;
}

/** A point of the canvas that hits the empty pane (panning starts there, not on a node). */
async function panePoint(page: Page): Promise<{ x: number; y: number }> {
  const point = await page.evaluate(() => {
    const pane = document.querySelector(".react-flow__pane")?.getBoundingClientRect();
    if (!pane) return null;
    for (let fy = 0.5; fy < 0.98; fy += 0.04)
      for (let fx = 0.5; fx < 0.98; fx += 0.04)
        for (const [x, y] of [
          [pane.left + pane.width * fx, pane.top + pane.height * fy],
          [pane.left + pane.width * (1 - fx), pane.top + pane.height * (1 - fy)],
        ] as const) {
          if (document.elementFromPoint(x, y)?.classList.contains("react-flow__pane")) return { x, y };
        }
    return null;
  });
  if (!point) throw new Error("no empty pane point");
  return point;
}

async function settle(page: Page): Promise<void> {
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        // Resolves after 500 ms without a long task (or after 15 s).
        const p = (window as unknown as { __perf: Window }).__perf;
        const start = performance.now();
        let seen = p.long.length;
        let quiet = performance.now();
        const check = () => {
          if (p.long.length !== seen) {
            seen = p.long.length;
            quiet = performance.now();
          }
          if (performance.now() - quiet > 500 || performance.now() - start > 15_000) resolve();
          else setTimeout(check, 50);
        };
        check();
      }),
  );
}

const results: Record<string, Result> = {};
/** Airflow token (baseURL = AIRFLOW_URL) when the stack checks auth (`private-stack.sh start --auth`). */
let TOKEN = "";
const auth = () => (TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {});
const dags = new DeployedDags("large-graph");

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ request }) => {
  const ui = await request.get(DEV).catch(() => null);
  test.skip(!ui?.ok(), "no UI server at AUTOAIR_DEV_URL");
  const token = await request.get("/auth/token").catch(() => null);
  if (token?.ok()) TOKEN = ((await token.json()) as { access_token?: string }).access_token ?? "";
});

test.afterAll(async ({ request }) => {
  await dags.cleanup(request);
  if (!Object.keys(results).length) return;
  const dir = path.join(ROOT, ".artifacts", "perf");
  mkdirSync(dir, { recursive: true });
  writeFileSync(path.join(dir, `large-graph-${LABEL}.json`), `${JSON.stringify(results, null, 1)}\n`);
  const rows = new Set(Object.values(results).flatMap((r) => Object.keys(r)));
  const fmt = (v: Result[string] | undefined) =>
    v === undefined
      ? ""
      : typeof v === "object"
        ? `${v.fps} fps, worst ${v.worstFrameMs} ms, ${v.longTasks} long (${v.longTaskMs} ms), script ${v.scriptMs} ms`
        : String(v);
  const lines = [
    `| metric | ${Object.keys(results).join(" | ")} |`,
    `|---|${"---|".repeat(Object.keys(results).length)}`,
  ];
  for (const row of rows)
    lines.push(
      `| ${row} | ${Object.values(results)
        .map((r) => fmt(r[row]))
        .join(" | ")} |`,
    );
  console.log(`\nlarge-graph (${LABEL})\n${lines.join("\n")}\n`);
});

for (const size of SIZES) {
  test(`large graph: ${size} tasks`, async ({ page, request }) => {
    test.setTimeout(600_000);
    const dagId = `perf_large_${size}_${SUFFIX}`;
    const spec = generate(size, dagId);
    dags.track(`${DEV}/api`, dagId, auth());
    const put = await request.put(`${DEV}/api/specs/${dagId}`, { data: { spec, force: true }, headers: auth() });
    expect(put.ok(), await put.text()).toBe(true);
    const errors: string[] = [];
    const result: Result = {};
    results[`${size} tasks`] = result;
    await instrument(page, dagId, errors);
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Performance.enable");

    const isValidate = (url: string) => new URL(url).pathname.endsWith("/validate");
    let validateSent = 0;
    page.on("request", (r) => {
      if (isValidate(r.url()) && !validateSent) validateSent = Date.now();
    });
    const validated = page.waitForResponse((r) => isValidate(r.url()), { timeout: 120_000 });
    const t0 = Date.now();
    await page.goto(`${DEV}/`);
    await expect(page.locator(".react-flow__node").first()).toBeVisible({ timeout: 120_000 });
    result["load: first node (ms)"] = Date.now() - t0;
    await (await validated).finished();
    result["load: validated (ms)"] = Date.now() - t0;
    result["validate request (ms)"] = Date.now() - validateSent;
    await settle(page);
    result["load: settled (ms)"] = Date.now() - t0;
    result["DOM nodes rendered"] = await page.locator(".react-flow__node").count();
    result["DOM edges rendered"] = await page.locator(".react-flow__edge").count();
    result["DOM elements"] = await page.evaluate(() => document.getElementsByTagName("*").length);
    result["heap after load (MB)"] = await heapMb(cdp);

    const pane = page.locator(".react-flow__pane");
    const box = await pane.boundingBox();
    if (!box) throw new Error("no pane");
    const center = { x: box.x + box.width / 2, y: box.y + box.height / 2 };

    // Zoom in towards the middle until task cards are readable (same zoom for every build,
    // whatever zoom the document opened at), then pan around.
    result["zoom"] = await measure(page, cdp, async () => {
      await page.mouse.move(center.x, center.y);
      for (let i = 0; i < 60 && (await zoomLevel(page)) < 0.75; i++) {
        await page.mouse.wheel(0, -120);
        await page.waitForTimeout(16);
      }
    });
    await settle(page);
    result["zoom at drag"] = await zoomLevel(page);
    const start = await panePoint(page);
    result["pan"] = await measure(page, cdp, async () => {
      await page.mouse.move(start.x, start.y);
      await page.mouse.down();
      for (let i = 1; i <= 60; i++) await page.mouse.move(start.x - i * 8, start.y - i * 4);
      await page.mouse.up();
    });

    // Drag one task card (inside the viewport) and undo it.
    const card = await page.evaluate(() => {
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      for (const el of document.querySelectorAll<HTMLElement>(".react-flow__node-operator")) {
        const r = el.getBoundingClientRect();
        if (r.left > 300 && r.top > 120 && r.right < vw - 400 && r.bottom < vh - 200)
          return { id: el.dataset.id ?? "", x: r.left + 30, y: r.top + r.height / 2 };
      }
      return null;
    });
    expect(card, "a task card in the viewport").not.toBeNull();
    if (!card) return;
    const node = page.locator(`.react-flow__node[data-id="${card.id}"]`);
    const before = await node.getAttribute("style");
    if (PROFILE) {
      await cdp.send("Profiler.enable");
      await cdp.send("Profiler.start");
    }
    result["drag node"] = await measure(page, cdp, async () => {
      await page.mouse.move(card.x, card.y);
      await page.mouse.down();
      for (let i = 1; i <= 40; i++) await page.mouse.move(card.x + i * 4, card.y + i * 2);
      await page.mouse.up();
    });
    if (PROFILE) {
      const { profile } = (await cdp.send("Profiler.stop")) as { profile: unknown };
      const dir = path.join(ROOT, ".artifacts", "perf");
      mkdirSync(dir, { recursive: true });
      writeFileSync(path.join(dir, `drag-${size}-${LABEL}.cpuprofile`), JSON.stringify(profile));
    }
    await expect.poll(() => node.getAttribute("style")).not.toBe(before);
    await page.waitForTimeout(400); // the drag burst becomes one history entry
    // Focus the canvas on an empty spot (a mouse click: a covered locator would wait forever).
    const empty = await panePoint(page);
    await page.mouse.click(empty.x, empty.y);
    await page.keyboard.press("ControlOrMeta+z");
    await expect.poll(() => node.getAttribute("style")).toBe(before);

    // Zoom out as far as allowed: the worst case for rendering (on large graphs: all of it).
    result["zoom out (to min zoom)"] = await measure(page, cdp, async () => {
      await page.mouse.move(center.x, center.y);
      for (let i = 0; i < 40; i++) {
        await page.mouse.wheel(0, 160);
        await page.waitForTimeout(16);
      }
    });
    await settle(page);
    result["DOM nodes rendered (zoomed out)"] = await page.locator(".react-flow__node").count();
    result["zoom (zoomed out)"] = await zoomLevel(page);
    const far = await panePoint(page);
    result["pan (zoomed out)"] = await measure(page, cdp, async () => {
      await page.mouse.move(far.x, far.y);
      await page.mouse.down();
      for (let i = 1; i <= 40; i++) await page.mouse.move(far.x + i * 6, far.y + i * 3);
      await page.mouse.up();
    });

    // Auto layout (ELK in the worker) of the whole document.
    const first = page.locator(".react-flow__node").first();
    const firstStyle = await first.getAttribute("style");
    const layoutStart = Date.now();
    await page.getByRole("button", { name: "Auto layout", exact: true }).click();
    await expect.poll(() => first.getAttribute("style"), { timeout: 120_000 }).not.toBe(firstStyle);
    result["auto layout (ms)"] = Date.now() - layoutStart;
    await settle(page);
    result["heap at end (MB)"] = await heapMb(cdp);
    expect(errors).toEqual([]);

    // A document without positions (written by the API, MCP or an importer) is laid out on open.
    const rawId = `${dagId}_raw`;
    dags.track(`${DEV}/api`, rawId, auth());
    const raw = generate(size, rawId, false);
    const putRaw = await request.put(`${DEV}/api/specs/${rawId}`, {
      data: { spec: raw, force: true },
      headers: auth(),
    });
    expect(putRaw.ok(), await putRaw.text()).toBe(true);
    const page2 = await page.context().newPage();
    await instrument(page2, rawId, errors);
    const t1 = Date.now();
    await page2.goto(`${DEV}/`);
    await expect
      .poll(
        () =>
          page2.evaluate(
            () =>
              new Set([...document.querySelectorAll<HTMLElement>(".react-flow__node")].map((n) => n.style.transform))
                .size,
          ),
        { timeout: 120_000, intervals: [100] },
      )
      .toBeGreaterThan(5);
    result["open without layout (ms)"] = Date.now() - t1;
    await page2.close();
    expect(errors).toEqual([]);

    // Generous ceilings: catch regressions (a blocked main thread, a crash), not targets.
    expect(result["load: settled (ms)"] as number).toBeLessThan(size * 120);
    expect((result["drag node"] as Measure).fps).toBeGreaterThan(5);
  });
}
