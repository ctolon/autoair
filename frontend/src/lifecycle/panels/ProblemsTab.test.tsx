// @vitest-environment jsdom
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import type { Problem } from "../../api/gen/types.gen";
import { useBuilder } from "../../store/builder";
import { useFocusRequest } from "../../store/focus";
import { renderWithProviders } from "../../test/render";
import { ProblemsTab } from "./ProblemsTab";

const problems: Problem[] = [
  { severity: "warning", code: "policy_rule", message: "tags must include the team", field: "tags" },
  { severity: "error", code: "structure", message: "task_id is required", node_id: "n1", field: "task_id" },
  { severity: "info", code: "import_check_skipped", message: "import check skipped" },
  { severity: "warning", code: "jinja", message: "unknown template variable 'foo'", node_id: "n1" },
];

describe("ProblemsTab", () => {
  beforeEach(() => {
    useBuilder.getState().setProblems([]);
  });

  it("says when there are no problems", () => {
    renderWithProviders(<ProblemsTab />);
    expect(screen.getByText("No problems detected.")).toBeTruthy();
  });

  it("groups problems by node with DAG-level problems first and errors before warnings", () => {
    useBuilder.getState().setProblems(problems);
    renderWithProviders(<ProblemsTab />);
    const sections = screen.getAllByRole("region");
    expect(sections[0]?.getAttribute("aria-label")).toBe("DAG");
    // Unknown node id (not on the canvas): the id itself is shown.
    expect(sections[1]?.getAttribute("aria-label")).toBe("n1");
    const nodeItems = within(screen.getByRole("region", { name: "n1" })).getAllByTitle("Show where this problem is");
    expect(nodeItems).toHaveLength(2);
    expect(nodeItems[0]?.textContent).toContain("task_id is required");
    expect(nodeItems[1]?.textContent).toContain("unknown template variable");
    expect(within(screen.getByRole("region", { name: "DAG" })).getByText(/tags must include the team/)).toBeTruthy();
  });

  it("counts per severity and filters", async () => {
    useBuilder.getState().setProblems(problems);
    renderWithProviders(<ProblemsTab />);
    expect(screen.getByText("All (4)")).toBeTruthy();
    expect(screen.getByText("Errors (1)")).toBeTruthy();
    expect(screen.getByText("Warnings (2)")).toBeTruthy();
    expect(screen.getByText("Info (1)")).toBeTruthy();

    fireEvent.click(screen.getByRole("radio", { name: "Errors (1)" }));
    await waitFor(() => expect(screen.queryByText(/tags must include the team/)).toBeNull());
    expect(screen.getByText(/task_id is required/)).toBeTruthy();

    fireEvent.click(screen.getByRole("radio", { name: "Info (1)" }));
    expect(await screen.findByText(/import check skipped/)).toBeTruthy();
    expect(screen.queryByText(/task_id is required/)).toBeNull();
  });

  it("shows a hint when the filter hides every problem", async () => {
    useBuilder.getState().setProblems(problems.slice(0, 1));
    renderWithProviders(<ProblemsTab />);
    fireEvent.click(screen.getByRole("radio", { name: "Errors (0)" }));
    expect(await screen.findByText("No problems of this severity.")).toBeTruthy();
  });

  it("jumps to the DAG setting a DAG-level problem is about", () => {
    useBuilder.getState().setProblems(problems.slice(0, 1));
    useFocusRequest.getState().clear();
    renderWithProviders(<ProblemsTab />);
    fireEvent.click(screen.getByTitle("Show where this problem is"));
    const request = useFocusRequest.getState().request;
    expect(request).toMatchObject({ nodeId: null, field: "tags", code: "policy_rule" });
    expect(useBuilder.getState().selectedId ?? null).toBeNull();
  });

  it("does not navigate to nodes that are no longer on the canvas", () => {
    useBuilder.getState().setProblems(problems.slice(1, 2));
    useFocusRequest.getState().clear();
    renderWithProviders(<ProblemsTab />);
    fireEvent.click(screen.getByTitle("Show where this problem is"));
    expect(useFocusRequest.getState().request).toBeNull();
  });
});
