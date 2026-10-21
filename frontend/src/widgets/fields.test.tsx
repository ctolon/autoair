// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../test/render";
import { JsonField } from "./fields";

// A plain controlled textarea stands in for CodeMirror: what matters is the value the field
// hands back to its editor after each keystroke (a different text resets the editor's caret).
vi.mock("./code", () => ({
  CodeEditor: ({
    value,
    onChange,
    ariaLabel,
  }: {
    value: string;
    onChange: (v: string) => void;
    ariaLabel?: string;
  }) => <textarea aria-label={ariaLabel} value={value} onChange={(e) => onChange(e.target.value)} />,
}));

function Controlled({ initial, onValue }: { initial?: unknown; onValue?: (v: unknown) => void }) {
  const [value, setValue] = useState<unknown>(initial);
  return (
    <>
      <JsonField
        label="env"
        value={value}
        onChange={(v) => {
          onValue?.(v);
          setValue(v);
        }}
      />
      <button type="button" onClick={() => setValue({ outside: true })}>
        outside
      </button>
    </>
  );
}

const editor = () => screen.getByRole<HTMLTextAreaElement>("textbox", { name: "env" });

describe("JsonField", () => {
  it("keeps the user's text while typing valid JSON (no pretty-print round trip)", () => {
    let committed: unknown;
    renderWithProviders(<Controlled onValue={(v) => (committed = v)} />);
    for (const text of ["[", "[1", "[1]", '{"a": "b"}', '{"a": "b", "c": 1}']) {
      fireEvent.change(editor(), { target: { value: text } });
      expect(editor().value).toBe(text);
    }
    expect(committed).toEqual({ a: "b", c: 1 });
  });

  it("keeps an emptied field empty and still shows outside changes", () => {
    renderWithProviders(<Controlled initial={[1]} />);
    expect(editor().value).toBe("[\n  1\n]");
    fireEvent.change(editor(), { target: { value: "" } });
    expect(editor().value).toBe("");
    fireEvent.change(editor(), { target: { value: "2" } });
    expect(editor().value).toBe("2");
    fireEvent.click(screen.getByRole("button", { name: "outside" }));
    expect(editor().value).toBe('{\n  "outside": true\n}');
  });
});
