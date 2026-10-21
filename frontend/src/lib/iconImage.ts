/**
 * Browser-side icon resizer: any image the browser can draw (PNG, JPEG, WebP, GIF, SVG…) is
 * rendered into a square PNG (64 px by default, crisp at 16–32 px on HiDPI screens). SVGs are
 * rasterised here, so the server only ever stores small PNGs.
 */
export type IconFit = "contain" | "cover";
export type IconBackground = "transparent" | "white" | "rounded";

export type IconRenderOptions = {
  size?: number;
  fit?: IconFit;
  /** Empty margin around the image, as a fraction of the size (0–0.3). */
  padding?: number;
  /** Extra magnification on top of the fit (1 = none). */
  zoom?: number;
  background?: IconBackground;
  /** Fill colour for "rounded" backgrounds. */
  color?: string;
};

type Rect = { x: number; y: number; width: number; height: number };

/** Where to draw a `w`×`h` image inside a square icon (centred). */
export function drawRect(w: number, h: number, opts: IconRenderOptions = {}): Rect {
  const size = opts.size ?? 64;
  const pad = Math.min(Math.max(opts.padding ?? 0.08, 0), 0.3) * size;
  const box = size - 2 * pad;
  if (w <= 0 || h <= 0) return { x: pad, y: pad, width: box, height: box };
  const scale =
    ((opts.fit ?? "contain") === "cover" ? Math.max(box / w, box / h) : Math.min(box / w, box / h)) * (opts.zoom ?? 1);
  const width = w * scale;
  const height = h * scale;
  return { x: (size - width) / 2, y: (size - height) / 2, width, height };
}

export async function loadImage(file: Blob): Promise<HTMLImageElement> {
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.decoding = "async";
    img.src = url;
    await img.decode();
    return img;
  } finally {
    // Decoded pixels stay usable after the object URL is released.
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

/** SVGs without width/height report 0×0 (or 150×150): draw them at a sensible size. */
function intrinsicSize(img: HTMLImageElement): { w: number; h: number } {
  const w = img.naturalWidth || 256;
  const h = img.naturalHeight || 256;
  return { w, h };
}

export function renderIcon(img: HTMLImageElement, canvas: HTMLCanvasElement, opts: IconRenderOptions = {}): void {
  const size = opts.size ?? 64;
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.clearRect(0, 0, size, size);
  const bg = opts.background ?? "transparent";
  if (bg === "white") {
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, size, size);
  } else if (bg === "rounded") {
    ctx.fillStyle = opts.color ?? "#ffffff";
    ctx.beginPath();
    ctx.roundRect(0, 0, size, size, size * 0.22);
    ctx.fill();
  }
  ctx.save();
  if (bg === "rounded") {
    ctx.beginPath();
    ctx.roundRect(0, 0, size, size, size * 0.22);
    ctx.clip();
  }
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  const { w, h } = intrinsicSize(img);
  const r = drawRect(w, h, opts);
  ctx.drawImage(img, r.x, r.y, r.width, r.height);
  ctx.restore();
}

/** Lower-case slug used as the icon name (`custom:<slug>`). */
export const iconSlug = (name: string) =>
  name
    .toLowerCase()
    .replace(/\.[a-z0-9]+$/, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80) || "icon";
