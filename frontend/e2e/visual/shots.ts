/**
 * Screenshot helpers shared by the standalone (`screens.spec.ts`) and plugin
 * (`plugin.spec.ts`) visual suites.
 */
import { type Locator, type Page, expect } from "@playwright/test";

/**
 * Machine-specific text: the local deploy folder lives in this run's folder (a different path
 * per run and machine). It is rewritten to a fixed placeholder before each screenshot, so the
 * layout (line wrapping) is the same everywhere, and masked.
 */
const RUN_DIR = (process.env.AUTOAIR_VISUAL_DIR ?? "").replaceAll("\\", "/");
const RUN_PLACEHOLDER = "/visual-run";

export const masks = (page: Page): Locator[] => [page.getByText(/^local · /), page.getByText(RUN_PLACEHOLDER)];

export async function hideRunDir(page: Page): Promise<void> {
  if (!RUN_DIR) return;
  await page.evaluate(
    ([dir, placeholder]) => {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode())
        if (n.nodeValue?.includes(dir)) n.nodeValue = n.nodeValue.split(dir).join(placeholder);
    },
    [RUN_DIR, RUN_PLACEHOLDER] as const,
  );
}

export async function settle(page: Page): Promise<void> {
  await page.evaluate(() => document.fonts.ready);
  // Live validation / code preview are debounced (600 ms) and the layout runs in a worker.
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(800);
  await page.mouse.move(0, 0);
}

export async function snap(page: Page, name: string, target?: Locator, extraMasks: Locator[] = []): Promise<void> {
  await hideRunDir(page);
  const opts = { mask: [...masks(page), ...extraMasks], fullPage: false };
  if (target) await expect(target).toHaveScreenshot(`${name}.png`, opts);
  else await expect(page).toHaveScreenshot(`${name}.png`, opts);
}
