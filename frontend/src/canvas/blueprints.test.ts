import { describe, expect, it } from "vitest";

import type { BlueprintDetail, BlueprintSummary } from "../api/gen/types.gen";
import type { BlueprintSpec } from "../lib/types";
import { blueprintPreviewOptions, previewTasks, versionsOf } from "./blueprints";
import { makeBlueprintSpec } from "./factories";
import { prepareFragment } from "./fragment";

const summary = (name: string, version: number): BlueprintSummary => ({
  name,
  version,
  class_name: "Extract",
  module: "company.blueprints",
  class_path: `company.blueprints.Extract${version > 1 ? `V${version}` : ""}`,
  config_class: "ExtractConfig",
  source: "entry_point",
});

const spec = (over: Partial<BlueprintSpec> = {}): BlueprintSpec => ({
  kind: "blueprint",
  step_id: "extract_1",
  blueprint: "extract",
  version: 1,
  class_path: "company.blueprints.Extract",
  config: {},
  ...over,
});

describe("blueprint helpers", () => {
  it("lists the versions of one blueprint, newest first", () => {
    const all = [summary("extract", 1), summary("load", 1), summary("extract", 3), summary("extract", 2)];
    expect(versionsOf(all, "extract")).toEqual([3, 2, 1]);
    expect(versionsOf(undefined, "extract")).toEqual([]);
  });

  it("creates a step with a unique id from the catalog entry", () => {
    const made = makeBlueprintSpec(summary("extract", 2), []);
    expect(made).toEqual({
      kind: "blueprint",
      step_id: "extract_1",
      blueprint: "extract",
      version: 2,
      class_path: "company.blueprints.ExtractV2",
      config_class: "ExtractConfig",
      config: {},
    });
  });

  it("keys previews by the config, whatever the key order", () => {
    const a = blueprintPreviewOptions(spec({ config: { a: 1, b: 2 } })).queryKey;
    const b = blueprintPreviewOptions(spec({ config: { b: 2, a: 1 } })).queryKey;
    const c = blueprintPreviewOptions(spec({ config: { a: 1, b: 3 } })).queryKey;
    expect(a).toEqual(b);
    expect(a).not.toEqual(c);
    // Invalid live input (a non-object config) is previewed as an empty config.
    expect(blueprintPreviewOptions(spec({ config: null as never })).queryKey).toEqual(
      blueprintPreviewOptions(spec()).queryKey,
    );
  });

  it("shows the default preview only for an empty config", () => {
    const detail = { preview: { kind: "group", tasks: ["a", "b"] } } as BlueprintDetail;
    expect(previewTasks(spec(), undefined, detail)?.tasks).toEqual(["a", "b"]);
    expect(previewTasks(spec({ config: { x: 1 } }), undefined, detail)).toBeNull();
    expect(previewTasks(spec({ config: { x: 1 } }), { kind: "task", tasks: ["s"] }, detail)?.tasks).toEqual(["s"]);
  });

  it("pasting a step gives it a fresh step id", () => {
    let n = 0;
    const existing = [{ id: "x", type: "blueprint", position: { x: 0, y: 0 }, data: { spec: spec() } }] as never;
    const out = prepareFragment(
      existing,
      { nodes: [{ ...spec(), id: "b1", position: { x: 0, y: 0 } }] },
      { newId: () => `n${++n}` },
    );
    const pasted = out.nodes[0]?.data.spec as BlueprintSpec;
    expect(pasted.kind).toBe("blueprint");
    expect(pasted.step_id).toBe("extract_2");
  });
});
