import { describe, expect, it } from "vitest";

import { drawRect, iconSlug } from "./iconImage";

describe("icon resizer geometry", () => {
  it("contains wide images centred with padding", () => {
    const r = drawRect(200, 100, { size: 64, padding: 0.125 }); // 8 px padding → 48 px box
    expect(r.width).toBe(48);
    expect(r.height).toBe(24);
    expect(r.x).toBe(8);
    expect(r.y).toBe(20);
  });

  it("covers the box (cropping) and zooms", () => {
    const cover = drawRect(200, 100, { size: 64, padding: 0, fit: "cover" });
    expect(cover.height).toBe(64);
    expect(cover.width).toBe(128);
    expect(cover.x).toBe(-32);
    const zoomed = drawRect(100, 100, { size: 64, padding: 0, zoom: 1.5 });
    expect(zoomed.width).toBe(96);
    expect(zoomed.x).toBe(-16);
  });

  it("clamps padding and handles empty images", () => {
    expect(drawRect(10, 10, { size: 64, padding: 2 }).width).toBeCloseTo(64 * 0.4);
    expect(drawRect(0, 0, { size: 64, padding: 0 })).toEqual({ x: 0, y: 0, width: 64, height: 64 });
  });

  it("slugifies file names", () => {
    expect(iconSlug("GitLab Logo.SVG")).toBe("gitlab-logo");
    expect(iconSlug("***")).toBe("icon");
  });
});
