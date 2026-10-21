import { afterEach, describe, expect, it, vi } from "vitest";

import type { Finding } from "../../api/gen/types.gen";
import { useBuilder } from "../../store/builder";
import { useFocusRequest } from "../../store/focus";
import { formatUsage, getAiModel, patchable, requestModel, revealFinding, setAiModel, severityPalette } from "./ai";

const finding = (over: Partial<Finding> = {}): Finding => ({
  id: "f1",
  severity: "high",
  category: "reliability",
  title: "t",
  message: "m",
  ...over,
});

describe("ai helpers", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("maps severities to palettes", () => {
    expect(severityPalette("critical")).toBe("red");
    expect(severityPalette("medium")).toBe("orange");
    expect(severityPalette("info")).toBe("gray");
  });

  it("keeps only findings with patches", () => {
    const withPatch = finding({ id: "a", patch: [{ op: "add", path: "/nodes/@x/retries", value: 2 }] });
    expect(patchable([withPatch, finding({ id: "b" })]).map((f) => f.id)).toEqual(["a"]);
    expect(patchable(undefined)).toEqual([]);
  });

  it("formats usage", () => {
    expect(formatUsage(null)).toBeNull();
    expect(formatUsage({ requests: 1, input_tokens: 1000, output_tokens: 200, cache_read_tokens: 50 })).toBe(
      "1,200 tokens · 50 cached",
    );
    expect(formatUsage({ requests: 2, input_tokens: 1, output_tokens: 1, cost_usd: 0.01234 })).toBe(
      "2 tokens · $0.0123",
    );
  });

  it("sends the picked model only when the server offers it", () => {
    const store = new Map<string, string>();
    vi.stubGlobal("window", {
      localStorage: {
        getItem: (k: string) => store.get(k) ?? null,
        setItem: (k: string, v: string) => store.set(k, v),
        removeItem: (k: string) => store.delete(k),
      },
    });
    expect(requestModel({ configured: true, models: ["a", "b"] })).toBeNull();
    setAiModel("b");
    expect(getAiModel()).toBe("b");
    expect(requestModel({ configured: true, models: ["a", "b"] })).toBe("b");
    expect(requestModel({ configured: true, models: ["a"] })).toBeNull();
    setAiModel(null);
    expect(getAiModel()).toBeNull();
  });

  it("survives missing storage", () => {
    expect(getAiModel()).toBeNull();
    expect(() => setAiModel("x")).not.toThrow();
  });

  it("selects the finding's node, or clears the selection for DAG-level findings", () => {
    const select = vi.fn();
    const setSelection = vi.fn();
    useBuilder.setState({
      nodes: [{ id: "load", position: { x: 0, y: 0 }, data: {} }] as never,
      select,
      setSelection,
    });
    vi.stubGlobal("requestAnimationFrame", () => 0);
    revealFinding({ node_id: "load", field: "retries" });
    expect(setSelection).toHaveBeenCalledWith(["load"]);
    // The inspector receives a focus request (opens the owning tab, focuses the field).
    expect(useFocusRequest.getState().request).toMatchObject({ nodeId: "load", field: "retries" });
    revealFinding({ node_id: "ghost", field: null });
    expect(select).toHaveBeenCalledWith(null);
    expect(useFocusRequest.getState().request).toMatchObject({ nodeId: null, field: null });
    vi.unstubAllGlobals();
  });
});
