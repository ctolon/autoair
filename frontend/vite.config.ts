import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import cssInjectedByJs from "vite-plugin-css-injected-by-js";

const STATIC = resolve(import.meta.dirname, "../src/autoair/static");
const API_PORT = process.env.AUTOAIR_API_PORT ?? "8765";
// Licences of the bundled dependencies, shipped next to each bundle (see NOTICE).
const LICENSE_FILE = { fileName: "THIRD_PARTY_LICENSES.md" };
// ES workers: `canvas/layout.worker.ts` runs as a module worker and is also imported on the
// main thread as the ELK fallback (one copy of ELK per bundle).
const WORKER = { format: "es" as const };
const shim = (name: string) => resolve(import.meta.dirname, "src/shims", `${name}.cjs`);

/**
 * Two builds of the same app:
 *  - `--mode plugin`: ES module loaded by Airflow >= 3.1 (`react_apps`, via dynamic import()).
 *    React, ReactDOM, Chakra and Emotion resolve to the Airflow UI's globals through tiny
 *    CJS shims, so there is exactly one instance of each. Being an ES module, the bundle
 *    can code-split: heavy parts (ELK layout, CodeMirror, RJSF) load lazily as chunks
 *    next to `main.js`.
 *  - `--mode standalone`: a regular SPA served at `/ui/` (standalone server, Airflow 3.0).
 */
export default defineConfig(({ mode }) => {
  if (mode === "plugin") {
    return {
      plugins: [react(), cssInjectedByJs({ relativeCSSInjection: true })],
      // Relative asset/worker URLs resolve against the chunk (import.meta.url), wherever
      // Airflow serves the bundle from.
      base: "./",
      worker: WORKER,
      resolve: {
        alias: [
          { find: /^react$/, replacement: shim("react") },
          { find: /^react\/jsx-runtime$/, replacement: shim("react-jsx-runtime") },
          { find: /^react\/jsx-dev-runtime$/, replacement: shim("react-jsx-runtime") },
          { find: /^react-dom$/, replacement: shim("react-dom") },
          { find: /^react-dom\/client$/, replacement: shim("react-dom-client") },
          { find: /^@chakra-ui\/react$/, replacement: shim("chakra-ui") },
          { find: /^@emotion\/react$/, replacement: shim("emotion-react") },
        ],
      },
      define: {
        global: "globalThis",
        "process.env": "{}",
        "process.env.NODE_ENV": JSON.stringify("production"),
      },
      build: {
        outDir: resolve(STATIC, "plugin"),
        emptyOutDir: true,
        chunkSizeWarningLimit: 2000,
        license: LICENSE_FILE,
        lib: {
          entry: resolve(import.meta.dirname, "src/main.plugin.tsx"),
          fileName: () => "main.js",
          formats: ["es"],
        },
      },
    };
  }
  return {
    base: "./",
    plugins: [react()],
    worker: WORKER,
    build: {
      outDir: resolve(STATIC, "standalone"),
      emptyOutDir: true,
      chunkSizeWarningLimit: 2000,
      license: LICENSE_FILE,
    },
    // AUTOAIR_API_PORT points a second dev server at another backend (parallel E2E runs).
    server: {
      proxy: {
        "/api": { target: `http://127.0.0.1:${API_PORT}`, ws: true },
        "/collab": { target: `ws://127.0.0.1:${API_PORT}`, ws: true },
      },
    },
  };
});
