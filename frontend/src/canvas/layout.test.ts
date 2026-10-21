import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import type { BuilderEdge, BuilderNode } from "../lib/types";

const op = (id: string, parentId?: string): BuilderNode => ({
  id,
  type: "operator",
  position: { x: 0, y: 0 },
  measured: { width: 200, height: 60 },
  ...(parentId ? { parentId } : {}),
  data: { spec: { kind: "operator", task_id: id, operator: "m.Op", params: {} } },
});
const group = (id: string): BuilderNode => ({
  id,
  type: "task_group",
  position: { x: 0, y: 0 },
  style: { width: 600, height: 400 },
  data: { spec: { kind: "task_group", group_id: id } },
});
const edge = (source: string, target: string): BuilderEdge => ({ id: `${source}-${target}`, source, target });

type Box = { x: number; y: number; width?: number; height?: number };
const at = (out: Map<string, Box>, id: string): Box => {
  const box = out.get(id);
  if (!box) throw new Error(`no position for ${id}`);
  return box;
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("autoLayout", () => {
  it("lays out left to right on the main thread when workers are unavailable", async () => {
    vi.stubGlobal("Worker", undefined);
    const { autoLayout } = await import("./layout");
    const out = await autoLayout([op("a"), op("b"), op("c")], [edge("a", "b"), edge("b", "c")]);
    expect(at(out, "a").x).toBeLessThan(at(out, "b").x);
    expect(at(out, "b").x).toBeLessThan(at(out, "c").x);
  }, 30_000);

  it("returns group sizes and child positions relative to the group", async () => {
    vi.stubGlobal("Worker", undefined);
    const { autoLayout } = await import("./layout");
    const out = await autoLayout([group("g"), op("a", "g"), op("b", "g"), op("c")], [edge("a", "b"), edge("g", "c")]);
    const g = at(out, "g");
    const width = g.width ?? 0;
    expect(width).toBeGreaterThan(400);
    expect(g.height ?? 0).toBeGreaterThan(60);
    expect(at(out, "a").x).toBeLessThan(width);
    expect(at(out, "c").x).toBeGreaterThan(g.x + width - 1);
  }, 30_000);

  it("uses the module worker when available and falls back when it fails", async () => {
    const created: { url: string; options: WorkerOptions }[] = [];
    class BrokenWorker {
      onmessage: ((e: MessageEvent) => void) | null = null;
      onerror: ((e: { message: string; preventDefault(): void }) => void) | null = null;
      constructor(url: string | URL, options: WorkerOptions) {
        created.push({ url: String(url), options });
      }
      postMessage() {
        queueMicrotask(() => this.onerror?.({ message: "boom", preventDefault: () => undefined }));
      }
      terminate() {}
    }
    vi.stubGlobal("Worker", BrokenWorker);
    const { autoLayout } = await import("./layout");
    const out = await autoLayout([op("a"), op("b")], [edge("a", "b")]);
    expect(created).toEqual([
      { url: expect.stringContaining("layout.worker"), options: expect.objectContaining({ type: "module" }) },
    ]);
    expect(at(out, "a").x).toBeLessThan(at(out, "b").x);
  }, 30_000);
});

describe("ELK bundling", () => {
  it("only layout.worker.ts imports the ELK runtime (one copy per bundle)", () => {
    const root = join(import.meta.dirname, "..");
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) {
          if (name !== "gen") walk(path);
        } else if (/\.tsx?$/.test(name) && !name.endsWith(".d.ts") && !name.endsWith(".test.ts")) {
          const text = readFileSync(path, "utf8");
          if (/["']elkjs(\/lib\/(elk\.bundled|elk-worker|main)[^"']*)?["']/.test(text))
            offenders.push(relative(root, path));
        }
      }
    };
    walk(root);
    expect(offenders).toEqual(["canvas/layout.worker.ts"]);
  });
});
