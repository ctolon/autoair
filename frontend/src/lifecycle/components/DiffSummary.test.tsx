// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SpecDiff } from "../../api/gen/types.gen";
import { renderWithProviders } from "../../test/render";
import { DiffSummary } from "./DiffSummary";

const diff = (before_present: boolean, after_present: boolean): SpecDiff =>
  ({
    identical: false,
    changed_fields: [],
    added_nodes: [],
    removed_nodes: [],
    changed_nodes: [
      {
        id: "a",
        kind: "operator",
        layout_only: false,
        changes: [{ path: "params.execution_timeout", before: null, after: null, before_present, after_present }],
      },
    ],
    added_edges: [],
    removed_edges: [],
  }) as unknown as SpecDiff;

describe("DiffSummary", () => {
  it("tells an absent key from an explicit null", () => {
    renderWithProviders(<DiffSummary diff={diff(false, true)} />);
    expect(screen.getByText("(not set)")).toBeTruthy();
    expect(screen.getByText("null")).toBeTruthy();
  });

  it("renders older servers' changes (no presence flags) as values", () => {
    const old = diff(true, true);
    const change = old.changed_nodes?.[0]?.changes?.[0] as Record<string, unknown>;
    delete change.before_present;
    delete change.after_present;
    renderWithProviders(<DiffSummary diff={old} />);
    expect(screen.queryByText("(not set)")).toBeNull();
  });
});
