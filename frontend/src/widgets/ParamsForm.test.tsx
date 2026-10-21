// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import type { JsonSchema } from "../lib/formSchema";
import { renderWithProviders } from "../test/render";
import { ParamsForm } from "./ParamsForm";

const schema: JsonSchema = {
  type: "object",
  required: ["retries_n", "ratio", "flag", "mode", "name"],
  properties: {
    retries_n: { type: "integer", title: "Retries n" },
    ratio: { type: "number", title: "Ratio" },
    flag: { type: "boolean", title: "Flag" },
    mode: { type: "string", title: "Mode", enum: ["fast", "safe"] },
    name: { type: "string", title: "Name" },
    files: { type: "array", title: "Files", items: { type: "string" } },
    options: { type: "object", title: "Options" },
    tuning: { type: "integer", title: "Tuning", default: 3 },
  },
};

function Form({
  initial = {},
  onValue,
  ...rest
}: {
  initial?: Record<string, unknown>;
  onValue: (v: Record<string, unknown>) => void;
  exclude?: string[];
  errors?: Record<string, string>;
}) {
  const [value, setValue] = useState(initial);
  return (
    <ParamsForm
      schema={schema}
      value={value}
      allowXcom={false}
      onChange={(v) => {
        onValue(v);
        setValue(v);
      }}
      {...rest}
      // A new array on every render, like the inspector passes it.
      {...(rest.exclude ? { exclude: [...rest.exclude] } : {})}
    />
  );
}

describe("ParamsForm", () => {
  it("renders a typed input per JSON Schema type and keeps value types", () => {
    let value: Record<string, unknown> = {};
    renderWithProviders(<Form onValue={(v) => (value = v)} />);
    fireEvent.change(screen.getByRole("spinbutton", { name: /^retries_n/ }), { target: { value: "4" } });
    expect(value.retries_n).toBe(4);
    fireEvent.change(screen.getByRole("spinbutton", { name: /^ratio/ }), { target: { value: "0.5" } });
    expect(value.ratio).toBe(0.5);
    fireEvent.change(screen.getByRole("combobox", { name: /^flag/ }), { target: { value: "false" } });
    expect(value.flag).toBe(false);
    const mode = screen.getByRole("combobox", { name: /^mode/ });
    expect([...mode.querySelectorAll("option")].map((o) => o.textContent)).toContain("safe");
    fireEvent.change(mode, { target: { value: "safe" } });
    expect(value.mode).toBe("safe");
    fireEvent.change(screen.getByRole("textbox", { name: /^name/ }), { target: { value: "etl" } });
    expect(value.name).toBe("etl");
    // Clearing a value removes the key (Airflow's default applies) instead of sending "".
    fireEvent.change(screen.getByRole("textbox", { name: /^name/ }), { target: { value: "" } });
    expect("name" in value).toBe(false);
  });

  it("flags missing required params and hides unset optional ones until asked", () => {
    renderWithProviders(<Form onValue={() => undefined} />);
    expect(screen.getByText("5 required parameters are not set.")).toBeTruthy();
    expect(screen.queryByRole("spinbutton", { name: /^tuning/ })).toBeNull();
    expect(screen.getByText(/3 more parameters/)).toBeTruthy();
  });

  it("keeps an optional field in place while it is being typed into (no remount, caret kept)", async () => {
    renderWithProviders(<Form onValue={() => undefined} exclude={[]} />);
    fireEvent.click(screen.getByText(/3 more parameters/));
    const tuning = await screen.findByRole("spinbutton", { name: /^tuning/ });
    fireEvent.focus(tuning);
    // The first character gives the field a value: it must not jump to the main list.
    fireEvent.change(tuning, { target: { value: "5" } });
    expect(tuning.isConnected).toBe(true);
    expect(screen.getByRole("spinbutton", { name: /^tuning/ })).toBe(tuning);
    // Clearing it again (the value becomes unset) keeps it mounted too.
    fireEvent.change(tuning, { target: { value: "" } });
    expect(tuning.isConnected).toBe(true);
  });

  it("keeps a set field in the main list while it is cleared and retyped", () => {
    renderWithProviders(<Form initial={{ tuning: 7 }} onValue={() => undefined} exclude={[]} />);
    const tuning = screen.getByRole("spinbutton", { name: /^tuning/ });
    fireEvent.focus(tuning);
    fireEvent.change(tuning, { target: { value: "" } });
    expect(tuning.isConnected).toBe(true);
    fireEvent.change(tuning, { target: { value: "8" } });
    expect(screen.getByRole("spinbutton", { name: /^tuning/ })).toBe(tuning);
  });

  it("shows optional params that have a value, with the right widgets", () => {
    renderWithProviders(
      <Form initial={{ files: ["a.csv"], options: { k: 1 }, tuning: 7 }} onValue={() => undefined} />,
    );
    expect((screen.getByRole("spinbutton", { name: /^tuning/ }) as HTMLInputElement).value).toBe("7");
    expect(screen.getByDisplayValue("a.csv")).toBeTruthy();
    expect(screen.getByRole("textbox", { name: /^options/ })).toBeTruthy();
  });

  it("leaves out excluded params and shows validation messages", async () => {
    renderWithProviders(<Form exclude={["ratio"]} errors={{ name: "name is too short" }} onValue={() => undefined} />);
    expect(screen.queryByRole("spinbutton", { name: /^ratio/ })).toBeNull();
    await waitFor(() => expect(screen.getByText("name is too short")).toBeTruthy());
  });
});

describe("ParamsForm catalog overrides", () => {
  const overridden: JsonSchema = {
    type: "object",
    required: ["token", "mode"],
    properties: {
      token: { type: "string", title: "API token", "x-autoair-widget": "secret" },
      mode: { type: "string", title: "Mode", "x-autoair-widget": "enum", enum: ["append", "overwrite"] },
      secret_internal: { type: "string", title: "Internal", "x-autoair-hidden": true },
    },
  };

  it("masks secrets, renders enum overrides and hides hidden params (keeping their value)", () => {
    let value: Record<string, unknown> = { secret_internal: "kept" };
    function Wrapped() {
      const [v, setV] = useState<Record<string, unknown>>(value);
      return (
        <ParamsForm
          schema={overridden}
          value={v}
          allowXcom={false}
          onChange={(next) => {
            value = next;
            setV(next);
          }}
        />
      );
    }
    const { container } = renderWithProviders(<Wrapped />);
    const secret = container.querySelector('input[type="password"]');
    expect(secret).not.toBeNull();
    fireEvent.change(secret as Element, { target: { value: "s3cr3t" } });
    expect(value.token).toBe("s3cr3t");
    expect(value.secret_internal).toBe("kept");
    expect(screen.queryByText("Internal")).toBeNull();
    fireEvent.change(screen.getByRole("combobox", { name: /^mode/ }), { target: { value: "overwrite" } });
    expect(value.mode).toBe("overwrite");
  });

  it("renders wrong-typed values in override widgets without throwing", () => {
    const odd: JsonSchema = {
      type: "object",
      required: ["q", "tok", "v", "payload"],
      properties: {
        q: { type: "string", title: "Q", "x-autoair-widget": "sql" },
        tok: { type: "string", title: "Tok", "x-autoair-widget": "secret" },
        v: { type: "string", title: "V", "x-autoair-widget": "variable" },
        payload: { type: "string", title: "Payload", "x-autoair-widget": "json" },
      },
    };
    expect(() =>
      renderWithProviders(
        <ParamsForm
          schema={odd}
          value={{ q: 42, tok: { a: 1 }, v: null, payload: [1, 2] }}
          allowXcom={false}
          onChange={() => {}}
        />,
      ),
    ).not.toThrow();
  });
});
