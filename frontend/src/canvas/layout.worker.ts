/**
 * The one module that bundles ELK. Vite emits it as a separate ES module file, used twice:
 *
 *  - as a module Web Worker: `elk-worker.min.js` detects a worker scope (no `document`, has
 *    `self`) and installs ELK's own message dispatcher. Protocol (ELK's):
 *    `{id, cmd: "layout", graph, layoutOptions, options}` → `{id, data}` | `{id, error}`.
 *  - as a dynamically imported module on the main thread (fallback when workers are
 *    unavailable): ELK then exports its in-process `FakeWorker`, published on `globalThis`
 *    because Vite drops the exports of worker entries.
 *
 * Loading both roles from one file keeps the ~1.4 MB ELK code from being bundled twice.
 */
import elkWorker from "elkjs/lib/elk-worker.min.js";

import { ELK_FALLBACK_GLOBAL } from "./elkFallback";

const FakeWorker = elkWorker?.Worker;
if (FakeWorker) (globalThis as Record<string, unknown>)[ELK_FALLBACK_GLOBAL] = FakeWorker;
