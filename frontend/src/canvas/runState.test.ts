import { describe, expect, it } from "vitest";

import {
  LEGEND_STATES,
  aggregateState,
  normalizeRunState,
  runStateHex,
  runStateStyle,
  shortRunLabel,
} from "./runState";

describe("run state mapping", () => {
  it("maps every Airflow task state to its own legend entry", () => {
    for (const s of [
      "success",
      "failed",
      "upstream_failed",
      "up_for_retry",
      "up_for_reschedule",
      "deferred",
      "skipped",
      "removed",
      "restarting",
      "scheduled",
      "queued",
      "running",
      "awaiting_input",
    ]) {
      expect(normalizeRunState(s)).toBe(s);
      expect(LEGEND_STATES).toContain(s);
    }
    expect(normalizeRunState(null)).toBe("none");
    expect(normalizeRunState(undefined)).toBe("none");
    expect(normalizeRunState("UPSTREAM_FAILED")).toBe("upstream_failed");
    expect(normalizeRunState("something_new")).toBe("none");
    expect(LEGEND_STATES).toContain("none");
  });

  it("does not paint upstream_failed or up_for_retry like failed/running", () => {
    expect(runStateStyle("upstream_failed").palette).toBe("orange");
    expect(runStateStyle("failed").palette).toBe("red");
    expect(runStateStyle("up_for_retry").palette).not.toBe(runStateStyle("running").palette);
    expect(runStateHex("upstream_failed")).not.toBe(runStateHex("failed"));
    expect(runStateHex("success", true)).not.toBe(runStateHex("success", false));
    expect(runStateHex(undefined)).toBeUndefined();
  });

  it("aggregates worst-first but keeps single states", () => {
    expect(aggregateState(["upstream_failed"])).toBe("upstream_failed");
    expect(aggregateState(["success", "upstream_failed", "up_for_retry"])).toBe("upstream_failed");
    expect(aggregateState(["success", "skipped"])).toBe("success");
    expect(aggregateState(["skipped", "skipped"])).toBe("skipped");
    expect(aggregateState([null, undefined])).toBeNull();
  });
});

describe("shortRunLabel", () => {
  it("shows the run type and a short local time", () => {
    const l = shortRunLabel("manual__2026-10-15T13:43:34.497397+00:00", "en-GB", "Europe/Istanbul");
    expect(l.type).toBe("manual");
    expect(l.when).toBe("15 Oct, 16:43");
  });
  it("keeps custom run ids", () => {
    expect(shortRunLabel("my_custom_run")).toEqual({ type: null, when: null, raw: "my_custom_run" });
    expect(shortRunLabel("scheduled__garbage").when).toBeNull();
  });
});
