// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";

import type { DagSpec } from "../../api/gen/types.gen";
import { useColorMode } from "../../shell/colorMode";
import { renderWithProviders } from "../../test/render";
import { MiniGraph } from "./MiniGraph";

const spec = {
  dag_id: "t",
  nodes: [{ id: "a", kind: "operator", task_id: "extract", operator: "x.BashOperator", position: { x: 0, y: 0 } }],
  edges: [],
} as unknown as DagSpec;

describe("MiniGraph", () => {
  const initial = useColorMode.getState().mode;
  afterEach(() => useColorMode.setState({ mode: initial }));

  it("follows the app's dark mode (React Flow's default `light` class re-themes Chakra tokens)", () => {
    useColorMode.setState({ mode: "dark" });
    const { container } = renderWithProviders(<MiniGraph spec={spec} label="preview" />);
    const flow = container.querySelector(".react-flow");
    expect(flow?.classList.contains("dark")).toBe(true);
    expect(flow?.classList.contains("light")).toBe(false);
  });

  it("uses the light theme in light mode", () => {
    useColorMode.setState({ mode: "light" });
    const { container } = renderWithProviders(<MiniGraph spec={spec} label="preview" />);
    expect(container.querySelector(".react-flow")?.classList.contains("light")).toBe(true);
  });
});
