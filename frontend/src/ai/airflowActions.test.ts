import { describe, expect, it } from "vitest";

import type { AirflowAction } from "../api/gen/types.gen";
import en from "../i18n/locales/en/ai.json";
import tr from "../i18n/locales/tr/ai.json";
import { actionLog, actionTarget, canExecute, clampMode, modesUpTo, resultText } from "./AirflowActions";

const retry: AirflowAction = { id: "a1", action: "retry", task_id: "load", run_id: "r1", status: "proposed" };

describe("Airflow action modes", () => {
  it("offers only the server's mode and stricter ones", () => {
    expect(modesUpTo("apply")).toEqual(["read_only", "hitl", "apply"]);
    expect(modesUpTo("hitl")).toEqual(["read_only", "hitl"]);
    expect(modesUpTo("read_only")).toEqual(["read_only"]);
    expect(modesUpTo(undefined)).toEqual(["read_only"]);
  });

  it("never requests more than the server allows", () => {
    expect(clampMode("apply", "hitl")).toBe("hitl");
    expect(clampMode("read_only", "apply")).toBe("read_only");
    expect(clampMode(null, "hitl")).toBe("hitl");
    expect(clampMode("hitl", null)).toBe("read_only");
  });
});

describe("Airflow action rows", () => {
  it("offers Execute only in hitl mode, with run:manage, for runnable actions", () => {
    expect(canExecute(retry, "hitl", true)).toBe(true);
    expect(canExecute({ ...retry, status: "failed" }, "hitl", true)).toBe(true);
    expect(canExecute(retry, "hitl", false)).toBe(false);
    expect(canExecute(retry, "apply", true)).toBe(false);
    expect(canExecute(retry, "read_only", true)).toBe(false);
    for (const status of ["blocked", "rejected", "running", "succeeded", "skipped", "instruction"] as const)
      expect(canExecute({ ...retry, status }, "hitl", true)).toBe(false);
  });

  it("names the target and orders the log by time", () => {
    expect(actionTarget(retry)).toBe("load");
    expect(actionTarget({ ...retry, map_index: 3 })).toBe("load[3]");
    expect(actionTarget({ action: "clear_run", run_id: "r1", status: "proposed" })).toBe("r1");
    expect(actionTarget({ action: "fix_connection", target: "pg", run_id: "r1", status: "instruction" })).toBe("pg");
    const log = actionLog([
      { ...retry, results: [{ status: "succeeded", at: "2026-01-02T00:00:00" }] },
      { id: "b", action: "clear", results: [{ status: "skipped", at: "2026-01-01T00:00:00" }] },
    ]);
    expect(log.map((e) => e.result.status)).toEqual(["skipped", "succeeded"]);
    expect(actionLog(undefined)).toEqual([]);
  });

  it("translates every status, mode and block reason in en and tr", () => {
    for (const locale of [en, tr]) {
      const ts = locale.troubleshoot;
      for (const s of ["instruction", "proposed", "blocked", "rejected", "running", "succeeded", "skipped", "failed"])
        expect(ts.actionStatus[s as keyof typeof ts.actionStatus]).toBeTruthy();
      for (const m of ["read_only", "hitl", "apply"] as const) {
        expect(ts.modes[m]).toBeTruthy();
        expect(ts.modeBadge[m]).toBeTruthy();
        expect(ts.modeHelp[m]).toBeTruthy();
      }
      for (const b of [
        "not_allowed",
        "other_dag",
        "other_run",
        "missing_task",
        "upstream_not_allowed",
        "downstream_not_allowed",
        "limit",
        "duplicate",
        "permission",
        "feature_off",
      ])
        expect(ts.blocked[b as keyof typeof ts.blocked]).toBeTruthy();
    }
  });
});

describe("action result messages", () => {
  const fixedT = (lang: "en" | "tr") => {
    const ts = (lang === "en" ? en : tr).troubleshoot as unknown as Record<string, Record<string, string>>;
    return (key: string, opts: Record<string, unknown> = {}) => {
      const [, group, name] = key.split(".");
      const text = group && name ? ts[group]?.[name] : undefined;
      if (text === undefined) return String(opts.defaultValue ?? key);
      return text.replace(/\{\{(\w+)\}\}/g, (_, v: string) => String(opts[v] ?? ""));
    };
  };

  it("are shown in the UI language through their code", () => {
    const cleared = {
      status: "succeeded" as const,
      code: "cleared",
      params: { count: 1 },
      message: "cleared 1 task instance(s)",
    };
    expect(resultText(fixedT("en"), cleared)).toBe("Cleared 1 task instance(s).");
    expect(resultText(fixedT("tr"), cleared)).toBe("1 görev örneği temizlendi.");
    const blocked = {
      status: "failed" as const,
      code: "blocked",
      params: { reason: "limit" },
      message: "blocked: limit",
    };
    expect(resultText(fixedT("tr"), blocked)).toBe(tr.troubleshoot.blocked.limit);
  });

  it("fall back to the server's text for unknown or missing codes", () => {
    expect(resultText(fixedT("tr"), { status: "failed", code: "brand_new", message: "new thing" })).toBe("new thing");
    expect(resultText(fixedT("tr"), { status: "failed", message: "old analysis" })).toBe("old analysis");
  });
});
