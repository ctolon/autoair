// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { isHandWritten, policyApplies, policyOf, withPolicy } from "../lib/sourcePolicy";
import { flushHistory, useBuilder } from "../store/builder";
import { renderWithProviders } from "../test/render";
import { SourcePolicyField } from "./SourcePolicyField";

describe("source policy helpers", () => {
  it("reads unknown or missing policies as auto", () => {
    expect(policyOf({})).toBe("auto");
    expect(policyOf({ source: null })).toBe("auto");
    expect(policyOf({ source: { policy: "future-mode" } })).toBe("auto");
    expect(policyOf({ source: { policy: "strict-preserve" } })).toBe("strict-preserve");
    expect(isHandWritten({ source: { origin: "python" } })).toBe(true);
    expect(policyApplies({ output_mode: "loader" })).toBe(false);
    expect(policyApplies({ raw_code: "x = 1" })).toBe(false);
    expect(policyApplies({})).toBe(true);
    expect(withPolicy({ source: { origin: "python", code: "x" } }, "generate")).toEqual({
      origin: "python",
      code: "x",
      policy: "generate",
    });
  });
});

describe("SourcePolicyField", () => {
  beforeEach(() => useBuilder.getState().reset("policy_dag"));

  const select = () => screen.getByRole("combobox") as HTMLSelectElement;

  it("sets the policy as an undoable document change", () => {
    renderWithProviders(<SourcePolicyField />);
    expect(select().value).toBe("auto");
    expect(screen.getByText("Auto (Generate)")).toBeTruthy();
    fireEvent.change(select(), { target: { value: "strict-preserve" } });
    expect(useBuilder.getState().dag.source).toEqual({ policy: "strict-preserve" });
    flushHistory();
    useBuilder.temporal.getState().undo();
    expect(useBuilder.getState().dag.source ?? null).toBeNull();
  });

  it("explains auto for hand-written DAGs", () => {
    useBuilder.getState().setDag({ source: { origin: "python", code: "x = 1\n" } });
    renderWithProviders(<SourcePolicyField />);
    expect(screen.getByText("Auto (Preserve)")).toBeTruthy();
    fireEvent.change(select(), { target: { value: "generate" } });
    expect(useBuilder.getState().dag.source).toEqual({ origin: "python", code: "x = 1\n", policy: "generate" });
  });

  it("is disabled for loader output and hidden for raw code DAGs", () => {
    useBuilder.getState().setDag({ output_mode: "loader" });
    const view = renderWithProviders(<SourcePolicyField />);
    expect(select().disabled).toBe(true);
    expect(screen.getByText(/Applies to Python output only/)).toBeTruthy();
    view.unmount();
    useBuilder.getState().setDag({ output_mode: "python", raw_code: "x = 1\n" });
    renderWithProviders(<SourcePolicyField />);
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("tolerates an unknown policy from a newer server", () => {
    useBuilder.getState().setDag({ source: { policy: "something-else" as never } });
    renderWithProviders(<SourcePolicyField />);
    expect(select().value).toBe("auto");
  });
});
