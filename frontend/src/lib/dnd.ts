import type { NodeSpec } from "./types";

/** Payload dragged from the palette onto the canvas. */
export const DND_MIME = "application/x-autoair-node";
/** A template fragment dragged from the palette (fetched with `getTemplate` on drop). */
export const FRAGMENT_MIME = "application/x-autoair-fragment";

/** `key` (optional) is the palette item key, recorded as "recently used" on drop. */
type PaletteItem = { spec: NodeSpec; label: string; key?: string };
type FragmentDragItem = { templateId: string; label: string };

export function encodePaletteItem(item: PaletteItem): string {
  return JSON.stringify(item);
}

export function decodePaletteItem(raw: string): PaletteItem | null {
  try {
    const value = JSON.parse(raw) as PaletteItem;
    return value && typeof value === "object" && "spec" in value ? value : null;
  } catch {
    return null;
  }
}

export function encodeFragmentItem(item: FragmentDragItem): string {
  return JSON.stringify(item);
}

export function decodeFragmentItem(raw: string): FragmentDragItem | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<FragmentDragItem>;
    return value && typeof value.templateId === "string"
      ? { templateId: value.templateId, label: String(value.label ?? "") }
      : null;
  } catch {
    return null;
  }
}
