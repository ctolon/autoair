import type { Box } from "./geometry";

/** An alignment guide (absolute canvas coordinates) spanning the aligned boxes. */
export type HelperLine = { orientation: "v" | "h"; pos: number; from: number; to: number };

type HelperResult = {
  /** Correction to apply to the dragged box so it snaps onto the guides. */
  dx: number;
  dy: number;
  lines: HelperLine[];
};

const anchorsX = (b: Box) => [b.x, b.x + b.width / 2, b.x + b.width];
const anchorsY = (b: Box) => [b.y, b.y + b.height / 2, b.y + b.height];

/**
 * Alignment guides for a dragged box against other boxes: left/center/right and
 * top/middle/bottom edges within `threshold` snap together.
 */
export function computeHelperLines(dragged: Box, others: readonly Box[], threshold = 6): HelperResult {
  let bestX: { d: number; pos: number; delta: number } | null = null;
  let bestY: { d: number; pos: number; delta: number } | null = null;
  for (const o of others) {
    for (const a of anchorsX(dragged)) {
      for (const b of anchorsX(o)) {
        const d = Math.abs(a - b);
        if (d <= threshold && (!bestX || d < bestX.d)) bestX = { d, pos: b, delta: b - a };
      }
    }
    for (const a of anchorsY(dragged)) {
      for (const b of anchorsY(o)) {
        const d = Math.abs(a - b);
        if (d <= threshold && (!bestY || d < bestY.d)) bestY = { d, pos: b, delta: b - a };
      }
    }
  }
  const dx = bestX?.delta ?? 0;
  const dy = bestY?.delta ?? 0;
  const snapped = { ...dragged, x: dragged.x + dx, y: dragged.y + dy };
  const lines: HelperLine[] = [];
  const near = (v: number, list: number[]) => list.some((x) => Math.abs(x - v) < 0.5);
  if (bestX) {
    const pos = bestX.pos;
    const aligned = others.filter((o) => near(pos, anchorsX(o)));
    const ys = [snapped, ...aligned].flatMap((b) => [b.y, b.y + b.height]);
    lines.push({ orientation: "v", pos, from: Math.min(...ys), to: Math.max(...ys) });
  }
  if (bestY) {
    const pos = bestY.pos;
    const aligned = others.filter((o) => near(pos, anchorsY(o)));
    const xs = [snapped, ...aligned].flatMap((b) => [b.x, b.x + b.width]);
    lines.push({ orientation: "h", pos, from: Math.min(...xs), to: Math.max(...xs) });
  }
  return { dx, dy, lines };
}
