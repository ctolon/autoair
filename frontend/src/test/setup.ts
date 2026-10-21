/**
 * Browser APIs jsdom lacks but Chakra / Ark UI use (media queries, element observers,
 * scrolling), stubbed for component tests. Node-environment tests skip this file's effects.
 */
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

if (typeof window !== "undefined") {
  window.matchMedia ??= ((query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }) as MediaQueryList) as typeof window.matchMedia;

  class NoopObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  }
  globalThis.ResizeObserver ??= NoopObserver as unknown as typeof ResizeObserver;
  globalThis.IntersectionObserver ??= NoopObserver as unknown as typeof IntersectionObserver;
  Element.prototype.scrollIntoView ??= () => undefined;
  Element.prototype.scrollTo ??= () => undefined;

  afterEach(() => cleanup());
}
