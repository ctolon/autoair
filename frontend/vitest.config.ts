import { defineConfig } from "vitest/config";

// Pure-logic tests (*.test.ts) run in Node; component tests (*.test.tsx) opt into jsdom with
// a `// @vitest-environment jsdom` docblock and use Testing Library (see src/test/render.tsx).
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    setupFiles: ["src/test/setup.ts"],
  },
});
