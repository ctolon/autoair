// @vitest-environment jsdom
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import type { OutputModelValue, ToolsetValue } from "../../lib/llm";
import { renderWithProviders } from "../../test/render";
import { LlmEditor, OutputTypeEditor, ToolsetsEditor } from "./LlmEditor";

/** Chakra switches toggle from their label (the hidden input does not react to clicks). */
function toggle(text: HTMLElement) {
  const label = text.closest("label");
  if (!label) throw new Error("switch label not found");
  fireEvent.click(label);
}

function Harness<T>({
  initial,
  render,
  onValue,
}: {
  initial: T;
  render: (v: T, set: (v: T) => void) => React.ReactElement;
  onValue: (v: T) => void;
}) {
  const [value, setValue] = useState<T>(initial);
  return render(value, (v) => {
    onValue(v);
    setValue(v);
  });
}

describe("OutputTypeEditor", () => {
  it("builds a structured output model and previews the class", () => {
    let last: OutputModelValue | undefined;
    renderWithProviders(
      <Harness<OutputModelValue | undefined>
        initial={undefined}
        onValue={(v) => (last = v)}
        render={(v, set) => <OutputTypeEditor value={v} onChange={set} />}
      />,
    );
    fireEvent.change(screen.getByRole("combobox", { name: /Output/ }), { target: { value: "model" } });
    expect(last?.$type).toBe("output_model");
    fireEvent.click(screen.getByRole("button", { name: /Add field/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "Field 2" }), { target: { value: "score" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Type 2" }), { target: { value: "float" } });
    expect(last?.fields.map((f) => [f.name, f.type])).toEqual([
      ["answer", "str"],
      ["score", "float"],
    ]);
    expect(screen.getByTestId("output-model-preview").textContent).toContain("score: float");

    fireEvent.change(screen.getByRole("textbox", { name: /Class name/ }), { target: { value: "bad name" } });
    expect(screen.getAllByText(/Use a Python class name/).length).toBeGreaterThan(0);

    fireEvent.change(screen.getByRole("combobox", { name: /Output/ }), { target: { value: "text" } });
    expect(last).toBeUndefined();
  });
});

describe("ToolsetsEditor", () => {
  it("adds SQL and MCP toolsets with kind-specific options", async () => {
    let last: ToolsetValue[] | undefined;
    renderWithProviders(
      <Harness<ToolsetValue[] | undefined>
        initial={undefined}
        onValue={(v) => (last = v)}
        render={(v, set) => <ToolsetsEditor value={v} onChange={set} />}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /SQL toolset/ }));
    fireEvent.click(screen.getByRole("button", { name: /MCP toolset/ }));
    expect(last?.map((t) => t.kind)).toEqual(["sql", "mcp"]);
    const sql = screen.getByTestId("toolset-0");
    expect(within(sql).getByText("Allow writes")).toBeTruthy();
    expect(within(screen.getByTestId("toolset-1")).queryByText("Allow writes")).toBeNull();
    toggle(within(sql).getByText("Allow writes"));
    await waitFor(() => expect(last?.[0]?.allow_writes).toBe(true));
    fireEvent.click(within(screen.getByTestId("toolset-1")).getByRole("button", { name: /Remove/ }));
    expect(last?.map((t) => t.kind)).toEqual(["sql"]);
  });
});

describe("LlmEditor", () => {
  it("shows only the fields the task accepts and clears approval options with the toggle", async () => {
    let last: Record<string, unknown> = {};
    renderWithProviders(
      <Harness<Record<string, unknown>>
        initial={{ require_approval: true, approval_timeout: { $type: "timedelta", seconds: 60 } }}
        onValue={(v) => (last = v)}
        render={(v, set) => (
          <LlmEditor
            fields={new Set(["require_approval", "approval_timeout", "allow_modifications"])}
            value={v}
            onChange={set}
          />
        )}
      />,
    );
    expect(screen.queryByText("Toolsets")).toBeNull();
    expect(screen.queryByText("Iterative human review")).toBeNull();
    expect((screen.getByRole("spinbutton", { name: /Approval timeout/ }) as HTMLInputElement).value).toBe("60");
    fireEvent.change(screen.getByRole("spinbutton", { name: /Approval timeout/ }), { target: { value: "120" } });
    expect(last.approval_timeout).toEqual({ $type: "timedelta", seconds: 120 });
    toggle(screen.getByText("Require human approval"));
    await waitFor(() => expect(last).toEqual({}));
    expect(screen.queryByRole("spinbutton", { name: /Approval timeout/ })).toBeNull();
  });
});
