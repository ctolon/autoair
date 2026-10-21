import { readFileSync } from "node:fs";
import path from "node:path";

import type { BrowserContext, Route } from "@playwright/test";

/**
 * Serves `/autoair/static/<file>` from this run's plugin build (AUTOAIR_VISUAL_PLUGIN_DIST, set by
 * playwright.visual-plugin.config.ts) instead of the bundle installed next to Airflow, so the
 * screenshots show the checked-out frontend and src/autoair/static is never rebuilt for them.
 */
const MIME: Record<string, string> = {
  ".js": "application/javascript",
  ".css": "text/css",
  ".svg": "image/svg+xml",
  ".json": "application/json",
  ".wasm": "application/wasm",
  ".woff2": "font/woff2",
};

async function serve(route: Route, dist: string): Promise<void> {
  const rel = decodeURIComponent(new URL(route.request().url()).pathname).replace(/^.*?\/autoair\/static\//, "");
  const file = path.resolve(dist, rel);
  if (!file.startsWith(dist + path.sep)) return route.fulfill({ status: 404 });
  let body: Buffer;
  try {
    body = readFileSync(file);
  } catch {
    return route.fulfill({ status: 404 });
  }
  await route.fulfill({
    body,
    contentType: MIME[path.extname(file)] ?? "application/octet-stream",
    headers: { "Cache-Control": "no-store" },
  });
}

export async function routePluginBundle(context: BrowserContext): Promise<void> {
  const dist = process.env.AUTOAIR_VISUAL_PLUGIN_DIST;
  if (!dist) throw new Error("AUTOAIR_VISUAL_PLUGIN_DIST is not set (run npm run test:visual:plugin)");
  await context.route("**/autoair/static/**", (route) => serve(route, dist));
}
