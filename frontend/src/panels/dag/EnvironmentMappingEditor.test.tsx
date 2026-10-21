// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { metaFixture, renderWithProviders } from "../../test/render";
import { EnvironmentMappingEditor } from "./EnvironmentMappingEditor";

// No Airflow: the mapping check and pickers stay disabled (features.airflow off).
const meta = metaFixture({ features: { airflow: false } } as never);

function setup(connections: Record<string, string> = { warehouse: "warehouse_prod" }) {
  const onChange = vi.fn();
  renderWithProviders(
    <EnvironmentMappingEditor env="prod" connections={connections} variables={{}} onChange={onChange} />,
    { meta },
  );
  return onChange;
}

describe("EnvironmentMappingEditor", () => {
  it("shows the mapping rows", () => {
    setup();
    const inputs = screen.getAllByRole("combobox") as HTMLInputElement[];
    expect(inputs.map((i) => i.value)).toEqual(["warehouse", "warehouse_prod"]);
  });

  it("commits valid edits only; invalid input stays a local draft with an error", () => {
    const onChange = setup();
    const actual = screen.getAllByRole("combobox")[1] as HTMLInputElement;
    fireEvent.change(actual, { target: { value: "warehouse prod" } });
    expect(screen.getByText(/Not saved until valid/)).toBeTruthy();
    expect(onChange).toHaveBeenLastCalledWith({ connections: {} });
    fireEvent.change(actual, { target: { value: "warehouse_prod_rw" } });
    expect(onChange).toHaveBeenLastCalledWith({ connections: { warehouse: "warehouse_prod_rw" } });
  });

  it("adds an empty draft row without touching the spec", () => {
    const onChange = setup({});
    fireEvent.click(screen.getByRole("button", { name: /Map a connection/ }));
    expect(screen.getAllByRole("combobox")).toHaveLength(2);
    expect(onChange).not.toHaveBeenCalled();
  });
});
