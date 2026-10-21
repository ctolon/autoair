// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { type NodeProps, ReactFlowProvider } from "@xyflow/react";
import { beforeEach, describe, expect, it } from "vitest";

import { getBlueprintQueryKey } from "../../api/gen/@tanstack/react-query.gen";
import type { BlueprintDetail, DagSpec } from "../../api/gen/types.gen";
import type { BlueprintFlowNode, BlueprintSpec } from "../../lib/types";
import { BlueprintInspector } from "../../panels/inspector/BlueprintInspector";
import { flushHistory, useBuilder } from "../../store/builder";
import { metaFixture, renderWithProviders, testQueryClient } from "../../test/render";
import { blueprintPreviewOptions } from "../blueprints";
import { BlueprintNodeView } from "./BlueprintNode";

const SPEC: BlueprintSpec = {
  kind: "blueprint",
  step_id: "extract_customers",
  blueprint: "extract",
  version: 1,
  class_path: "company.blueprints.Extract",
  config_class: "ExtractConfig",
  config: {},
};

const DETAIL: BlueprintDetail = {
  name: "extract",
  version: 1,
  class_name: "Extract",
  module: "company.blueprints",
  class_path: "company.blueprints.Extract",
  config_class: "ExtractConfig",
  source: "entry_point",
  docstring: "Extract rows from a table.",
  config_schema: {
    type: "object",
    properties: {
      source_table: { type: "string", title: "Source Table" },
      batch_size: { type: "integer", title: "Batch Size", default: 1000, minimum: 1 },
    },
    required: ["source_table"],
  },
  preview: { kind: "group", tasks: ["validate", "extract"] },
};

function client() {
  const c = testQueryClient(metaFixture({ features: { blueprints: true } } as never));
  c.setQueryData(getBlueprintQueryKey({ path: { name: "extract", version: 1 } }), DETAIL);
  return c;
}

function renderNode(spec: BlueprintSpec, extra: { collapsed?: boolean } = {}, c = client()) {
  const props = { id: "b", data: { spec, ...extra }, selected: false } as unknown as NodeProps<BlueprintFlowNode>;
  return renderWithProviders(
    <ReactFlowProvider>
      <BlueprintNodeView {...props} />
    </ReactFlowProvider>,
    { client: c },
  );
}

describe("BlueprintNodeView", () => {
  beforeEach(() => useBuilder.setState({ problems: [], runOverlay: null }));

  it("shows the catalog's default preview while the config is empty", async () => {
    renderNode(SPEC);
    expect(screen.getByText("extract_customers")).toBeTruthy();
    expect(screen.getByText("extract v1")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("extract_customers.validate")).toBeTruthy());
  });

  it("shows the preview fetched for the current config", () => {
    const spec = { ...SPEC, config: { source_table: "raw.t" } };
    const c = client();
    c.setQueryData(blueprintPreviewOptions(spec).queryKey, { kind: "task", tasks: ["extract_customers"] });
    renderNode(spec, {}, c);
    expect(screen.getAllByText("extract_customers").length).toBe(2);
    expect(screen.queryByText("extract_customers.validate")).toBeNull();
  });

  it("collapsed, it summarises the task count", () => {
    renderNode(SPEC, { collapsed: true });
    expect(screen.queryByText("extract_customers.validate")).toBeNull();
    expect(screen.getByRole("button", { name: /Show the tasks/ })).toBeTruthy();
  });

  it("never throws on invalid live input", () => {
    const broken = {
      ...SPEC,
      version: "x",
      blueprint: null,
      config: null,
      step_id: 3,
      label: {},
    } as unknown as BlueprintSpec;
    renderNode(broken);
    expect(screen.getByText("Select the step to preview its tasks.")).toBeTruthy();
  });
});

describe("BlueprintInspector", () => {
  const load = (config: Record<string, unknown>) => {
    const dag = {
      spec_version: 3,
      dag_id: "demo",
      nodes: [{ ...SPEC, id: "b", config, position: { x: 0, y: 0 } }],
      edges: [],
    } as unknown as DagSpec;
    useBuilder.getState().load(dag, false);
    return useBuilder.getState().nodes[0] as BlueprintFlowNode;
  };

  it("edits the config through the schema form, undoably", { timeout: 30_000 }, async () => {
    const node = load({ source_table: "raw.a" });
    const c = client();
    c.setQueryData(blueprintPreviewOptions(node.data.spec).queryKey, { kind: "group", tasks: ["validate"] });
    renderWithProviders(<BlueprintInspector node={node} />, { client: c });
    expect(screen.getByText("company.blueprints.Extract")).toBeTruthy();
    expect(screen.getByText(/needs airflow-blueprint and the module company.blueprints/)).toBeTruthy();
    expect(screen.getByTestId("blueprint-preview").textContent).toContain("extract_customers.validate");
    // RJSF (lazy) names its inputs root_<field>; the Chakra label is not linked to them.
    const input = await waitFor(
      () => {
        const el = document.getElementById("root_source_table");
        if (!(el instanceof HTMLInputElement)) throw new Error("form not rendered yet");
        return el;
      },
      { timeout: 15_000 },
    );
    expect(input.value).toBe("raw.a");
    fireEvent.change(input, { target: { value: "raw.b" } });
    await waitFor(() => {
      const spec = useBuilder.getState().nodes[0]?.data.spec as BlueprintSpec;
      expect(spec.config).toMatchObject({ source_table: "raw.b" });
    });
    flushHistory();
    useBuilder.temporal.getState().undo();
    const restored = useBuilder.getState().nodes[0]?.data.spec as BlueprintSpec;
    expect(restored.config).toMatchObject({ source_table: "raw.a" });
  });

  it("explains a step whose blueprint is not in the catalog", async () => {
    const node = load({});
    const missing = { ...node, data: { spec: { ...node.data.spec, blueprint: "gone" } } } as BlueprintFlowNode;
    renderWithProviders(<BlueprintInspector node={missing} />, { client: client() });
    await waitFor(() => expect(screen.getByText(/Blueprint gone v1 is not in the catalog/)).toBeTruthy());
  });
});
