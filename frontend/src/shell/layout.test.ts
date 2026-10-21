import { beforeEach, describe, expect, it } from "vitest";

import { DEFAULT_SIZES, MIN_SIZES, isPanelOpen, overlayFor, useLayout } from "./layout";

const reset = () =>
  useLayout.setState({
    collapsed: { palette: false, inspector: false, bottom: false },
    sizes: { ...DEFAULT_SIZES },
    overlay: { palette: false, inspector: false },
    drawer: null,
  });

describe("layout store", () => {
  beforeEach(reset);

  it("collapses docked panels", () => {
    const s = useLayout.getState();
    s.toggle("palette");
    expect(useLayout.getState().collapsed.palette).toBe(true);
    expect(isPanelOpen(useLayout.getState(), "palette")).toBe(false);
    useLayout.getState().set("palette", false);
    expect(isPanelOpen(useLayout.getState(), "palette")).toBe(true);
  });

  it("turns side panels into drawers on narrow builders", () => {
    expect(overlayFor(1600)).toEqual({ palette: false, inspector: false });
    expect(overlayFor(1000)).toEqual({ palette: false, inspector: true });
    expect(overlayFor(600)).toEqual({ palette: true, inspector: true });
    useLayout.getState().setOverlay(overlayFor(1000));
    // An overlay inspector starts closed and opens/closes as a drawer, docked prefs untouched.
    expect(isPanelOpen(useLayout.getState(), "inspector")).toBe(false);
    useLayout.getState().toggle("inspector");
    expect(useLayout.getState().drawer).toBe("inspector");
    expect(useLayout.getState().collapsed.inspector).toBe(false);
    useLayout.getState().set("inspector", true);
    expect(useLayout.getState().drawer).toBe(null);
    // Back to a wide builder: the drawer is forgotten.
    useLayout.getState().toggle("inspector");
    useLayout.getState().setOverlay(overlayFor(1600));
    expect(useLayout.getState().drawer).toBe(null);
    expect(isPanelOpen(useLayout.getState(), "inspector")).toBe(true);
  });

  it("clamps sizes to the minimum", () => {
    useLayout.getState().setSize("palette", 10);
    expect(useLayout.getState().sizes.palette).toBe(MIN_SIZES.palette);
    useLayout.getState().setSize("bottom", 333.4);
    expect(useLayout.getState().sizes.bottom).toBe(333);
  });
});
