// @vitest-environment jsdom
import { ChakraProvider, defaultSystem } from "@chakra-ui/react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../i18n";
import { RootErrorBoundary } from "./ErrorBoundary";

let broken = true;
function Flaky() {
  if (broken) throw new Error("boom");
  return <p>recovered</p>;
}

function renderRoot() {
  return render(
    <ChakraProvider value={defaultSystem}>
      <RootErrorBoundary>
        <Flaky />
      </RootErrorBoundary>
    </ChakraProvider>,
  );
}

describe("RootErrorBoundary", () => {
  beforeEach(() => {
    broken = true;
    // React reports caught render errors on the console; keep the test output readable.
    vi.spyOn(console, "error").mockImplementation(() => undefined);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    void i18n.changeLanguage("en");
  });

  it("shows a recoverable fallback instead of a blank page", () => {
    void i18n.changeLanguage("en");
    renderRoot();
    const alert = screen.getByTestId("error-boundary");
    expect(alert.textContent).toContain("Something went wrong in AutoAir.");
    expect(alert.textContent).toContain("boom");
    broken = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("recovered")).toBeTruthy();
    expect(screen.queryByTestId("error-boundary")).toBeNull();
  });

  it("offers a page reload", () => {
    const reload = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ ...window.location, reload });
    void i18n.changeLanguage("en");
    renderRoot();
    fireEvent.click(screen.getByRole("button", { name: "Reload page" }));
    expect(reload).toHaveBeenCalledOnce();
  });

  it("is translated", async () => {
    await i18n.changeLanguage("tr");
    renderRoot();
    expect(screen.getByTestId("error-boundary").textContent).toContain("AutoAir içinde bir hata oluştu.");
    expect(screen.getByRole("button", { name: "Tekrar dene" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Sayfayı yeniden yükle" })).toBeTruthy();
  });
});
