import { describe, expect, it } from "vitest";

import { ago, dayBound, formatCost, formatNumber, formatTime, usageTotals } from "./format";
import { canOpenAdmin } from "./store";

describe("admin formatting", () => {
  it("formats numbers and costs", () => {
    expect(formatNumber(1234567)).toBe("1,234,567");
    expect(formatNumber(null)).toBe("—");
    expect(formatCost(0.75)).toBe("$0.75");
    expect(formatCost(12.5)).toBe("$12.50");
    expect(formatCost(undefined)).toBe("—");
  });

  it("never throws on bad timestamps", () => {
    expect(formatTime("not a date")).toBe("not a date");
    expect(formatTime(null)).toBe("—");
    expect(formatTime("2026-10-15T10:00:00+00:00")).toMatch(/2026/);
  });

  it("describes ages", () => {
    expect(ago(100, 105)).toBe("5 s");
    expect(ago(0, 180)).toBe("3 min");
    expect(ago(0, 7200)).toBe("2 h");
    expect(ago(0, 3 * 86400)).toBe("3 d");
    expect(ago(10, 5)).toBe("0 s");
  });

  it("totals usage rows; unknown costs stay unknown", () => {
    expect(
      usageTotals([
        { requests: 2, input_tokens: 10, output_tokens: 5, cost_usd: 0.5 },
        { requests: 1, input_tokens: 1, output_tokens: 1, cost_usd: null },
      ]),
    ).toEqual({ requests: 3, input: 11, output: 6, cost: 0.5 });
    expect(usageTotals([{ requests: 1, input_tokens: 1, output_tokens: 1 }]).cost).toBeNull();
  });

  it("turns date inputs into bounds", () => {
    expect(dayBound("2026-10-15")).toBe("2026-10-15");
    expect(dayBound("2026-10-31", true)).toBe("2026-11-01");
    expect(dayBound("")).toBeUndefined();
    expect(dayBound("15.10.2026")).toBeUndefined();
  });

  it("requires admin:panel", () => {
    expect(canOpenAdmin(["admin:panel", "spec:read"])).toBe(true);
    expect(canOpenAdmin(["admin:config"])).toBe(false);
    expect(canOpenAdmin(undefined)).toBe(false);
  });
});
