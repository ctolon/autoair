// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { metaFixture, renderWithProviders } from "../test/render";
import { EditorModeToggle } from "./EditorModeToggle";
import { useCodeMode } from "./store";

describe("EditorModeToggle", () => {
  beforeEach(() => {
    useCodeMode.setState({ mode: "visual", draft: null, base: null });
  });

  it("switches between the visual and the code editor", () => {
    renderWithProviders(<EditorModeToggle />);
    const visual = screen.getByTestId("mode-visual");
    const code = screen.getByTestId("mode-code");
    expect(visual.getAttribute("aria-pressed")).toBe("true");
    expect(code.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(code);
    expect(useCodeMode.getState().mode).toBe("code");
    expect(screen.getByTestId("mode-code").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("mode-visual").getAttribute("aria-pressed")).toBe("false");
  });

  it("labels the buttons and marks an edited code draft", () => {
    useCodeMode.setState({ draft: "print(1)", base: "" });
    renderWithProviders(<EditorModeToggle />);
    expect(screen.getByRole("group", { name: "Editor mode" })).toBeTruthy();
    expect(screen.getByText("Visual")).toBeTruthy();
    expect(screen.getByLabelText("edited")).toBeTruthy();
  });

  it("shows icons only when asked", () => {
    renderWithProviders(<EditorModeToggle iconOnly />);
    expect(screen.queryByText("Visual")).toBeNull();
    expect(screen.getByRole("button", { name: "Code" })).toBeTruthy();
  });

  it("is hidden when only one editor is enabled", () => {
    const { container } = renderWithProviders(<EditorModeToggle />, {
      meta: metaFixture({ features: { code_editor: false } }),
    });
    expect(container.querySelector("[data-testid='mode-visual']")).toBeNull();
    expect(screen.queryByRole("group")).toBeNull();
  });

  it("reflects the stored mode and switches back to the visual editor", () => {
    useCodeMode.setState({ mode: "code" });
    renderWithProviders(<EditorModeToggle />);
    expect(screen.getByTestId("mode-code").getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByTestId("mode-visual"));
    expect(useCodeMode.getState().mode).toBe("visual");
    expect(screen.getByTestId("mode-visual").getAttribute("aria-pressed")).toBe("true");
  });
});
