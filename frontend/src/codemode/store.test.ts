import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { type LifecycleDialog, useLifecycle } from "../lifecycle/store";
import { useBuilder } from "../store/builder";
import {
  IDLE_CHECK,
  SPEC_ONLY_DIALOGS,
  canvasActive,
  editorAvailability,
  effectiveMode,
  isRawDag,
  isStale,
  specOnlyBlocked,
  useCodeMode,
} from "./store";

const access = (features: Record<string, boolean>, perms: string[] = ["spec:write", "code:execute"]) => ({
  feature: (f: string) => features[f] ?? true,
  can: (p: string) => perms.includes(p),
});

describe("editor availability", () => {
  it("shows the toggle only when both editors are on", () => {
    expect(editorAvailability(access({})).both).toBe(true);
    expect(editorAvailability(access({ code_editor: false })).both).toBe(false);
  });
  it("loads straight into the only enabled editor", () => {
    const codeOnly = editorAvailability(access({ visual_editor: false }));
    expect(effectiveMode("visual", codeOnly)).toBe("code");
    const visualOnly = editorAvailability(access({ code_editor: false }));
    expect(effectiveMode("code", visualOnly)).toBe("visual");
    expect(effectiveMode("code", editorAvailability(access({})))).toBe("code");
  });
  it("needs spec:write, code:execute and python_code to edit", () => {
    expect(editorAvailability(access({})).canEditCode).toBe(true);
    expect(editorAvailability(access({}, ["spec:write"])).canEditCode).toBe(false);
    expect(editorAvailability(access({ python_code: false })).canEditCode).toBe(false);
  });
});

describe("raw code DAGs", () => {
  const author = ["spec:write", "code:execute", "code:raw"];
  it("need code:raw, the code editor and Python code", () => {
    expect(editorAvailability(access({}, author)).canRaw).toBe(true);
    expect(editorAvailability(access({})).canRaw).toBe(false);
    expect(editorAvailability(access({ code_editor: false }, author)).canRaw).toBe(false);
    expect(editorAvailability(access({ python_code: false }, author)).canRaw).toBe(false);
  });
  it("are recognised by raw_code and hide the canvas", () => {
    expect(isRawDag({ raw_code: "" })).toBe(true);
    expect(isRawDag({ raw_code: null })).toBe(false);
    expect(isRawDag({})).toBe(false);
    useCodeMode.setState({ mode: "visual" });
    useBuilder.getState().reset("v");
    expect(canvasActive()).toBe(true);
    useCodeMode.setState({ mode: "code" });
    expect(canvasActive()).toBe(false);
    useCodeMode.setState({ mode: "visual" });
    useBuilder.setState((s) => ({ dag: { ...s.dag, raw_code: "x = 1\n" } }));
    expect(canvasActive()).toBe(false);
    useBuilder.getState().reset("v");
  });
});

describe("spec-only dialogs", () => {
  afterEach(() => {
    useLifecycle.getState().closeDialog();
    useBuilder.getState().reset("v");
  });
  it("are refused for raw code DAGs, whatever entry point opens them", () => {
    useBuilder.setState((s) => ({ dag: { ...s.dag, raw_code: "x = 1\n" } }));
    for (const kind of SPEC_ONLY_DIALOGS) {
      expect(specOnlyBlocked(kind)).toBe(true);
      useLifecycle.getState().openDialog({ kind } as LifecycleDialog);
      expect(useLifecycle.getState().dialog).toBe(null);
    }
    // Dialogs that work on the source stay available.
    useLifecycle.getState().openDialog({ kind: "aiExplainDag" });
    expect(useLifecycle.getState().dialog).toEqual({ kind: "aiExplainDag" });
  });
  it("open normally for visual DAGs", () => {
    useBuilder.getState().reset("v");
    useLifecycle.getState().openDialog({ kind: "aiOptimize" });
    expect(useLifecycle.getState().dialog).toEqual({ kind: "aiOptimize" });
  });
});

describe("code drafts", () => {
  beforeEach(() => useCodeMode.setState({ mode: "visual", draft: null, base: null, check: IDLE_CHECK }));

  it("starts a draft from the generated code and ends it when typed back", () => {
    const s = useCodeMode.getState();
    s.edit("gen", "gen");
    expect(useCodeMode.getState().draft).toBe(null);
    s.edit("gen + edit", "gen");
    expect(useCodeMode.getState()).toMatchObject({ draft: "gen + edit", base: "gen" });
    // The generated code changes meanwhile: the base stays what the draft started from.
    useCodeMode.getState().edit("gen + more", "gen2");
    expect(useCodeMode.getState().base).toBe("gen");
    expect(isStale(useCodeMode.getState().draft, "gen", "gen2")).toBe(true);
    useCodeMode.getState().edit("gen", "gen2");
    expect(useCodeMode.getState().draft).toBe(null);
  });
});
