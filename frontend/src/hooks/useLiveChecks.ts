import { useEffect, useRef, useState } from "react";

import { generateCode, validateSpec } from "../api/gen/sdk.gen";
import type { Problem } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { schemaProblems } from "../lib/schemaErrors";
import { useBuilder } from "../store/builder";

/**
 * Validation problems plus the code generator's. A node the validator already reports an
 * error for (e.g. a syntax error in a task body) fails code generation for the same reason:
 * that duplicate "codegen" error is dropped, as is an unlocated one when validation failed.
 */
export function mergeProblems<T extends Pick<Problem, "severity" | "code" | "node_id">>(
  report: T[],
  codegen: T[],
): T[] {
  const failed = new Set(report.filter((p) => p.severity === "error").map((p) => p.node_id ?? null));
  const duplicate = (p: T) => p.code === "codegen" && failed.size > 0 && (!p.node_id || failed.has(p.node_id));
  const extra = codegen.filter((p) => !duplicate(p));
  return [...report, ...extra];
}

export type LiveState = { code: string | null; checking: boolean; error: string | null };

/**
 * Debounced server-side validation + code preview on every document change. The backend
 * is the single source of truth for validity (Pydantic, catalog, graph and policy checks).
 */
export function useLiveChecks(delay = 600): LiveState {
  // A check is scheduled at once: "checking" until the first result (no "fix the problems" flash).
  const [state, setState] = useState<LiveState>({ code: null, checking: true, error: null });
  const seq = useRef(0);
  const inflight = useRef<AbortController | null>(null);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const run = async () => {
      const id = ++seq.current;
      // A newer document supersedes the in-flight check: cancel it.
      inflight.current?.abort();
      const ctrl = new AbortController();
      inflight.current = ctrl;
      const { signal } = ctrl;
      const { environment } = useBuilder.getState();
      const spec = useBuilder.getState().toSpec();
      setState((s) => ({ ...s, checking: true }));
      try {
        // The selected environment applies its overlay and the policy rules matching it.
        const query = { import_check: false, ...(environment ? { environment } : {}) };
        const [report, code] = await Promise.all([
          validateSpec({ body: spec, query, signal, throwOnError: true }),
          generateCode({ body: spec, signal, throwOnError: true }),
        ]);
        if (id !== seq.current) return;
        useBuilder.getState().setProblems(mergeProblems(report.data.problems, code.data.problems));
        setState({ code: code.data.code ?? null, checking: false, error: null });
      } catch (err) {
        if (id !== seq.current || signal.aborted) return;
        // A 422 means the document does not even match the spec schema: one located problem
        // per error, so clicking it opens the right node and field.
        const message = errorMessage(err);
        useBuilder
          .getState()
          .setProblems(schemaProblems(err, spec) ?? [{ severity: "error", code: "schema", message }]);
        setState({ code: null, checking: false, error: message });
      }
    };
    const schedule = () => {
      clearTimeout(timer);
      timer = setTimeout(() => void run(), delay);
    };
    schedule();
    // Ignore pure selection/measurement changes: compare a serialisation of the document
    // with the last one (computed once per change, not twice).
    const doc = (x: ReturnType<typeof useBuilder.getState>) =>
      JSON.stringify([
        x.dag,
        x.edges.map((e) => [e.source, e.target, e.label]),
        x.nodes.map((n) => [n.id, n.parentId, n.data]),
      ]);
    let last = doc(useBuilder.getState());
    const unsubscribe = useBuilder.subscribe((s, prev) => {
      if (s.environment !== prev.environment) {
        schedule();
        return;
      }
      if (s.dag === prev.dag && s.nodes === prev.nodes && s.edges === prev.edges) return;
      const next = doc(s);
      if (next !== last) {
        last = next;
        schedule();
      }
    });
    return () => {
      clearTimeout(timer);
      inflight.current?.abort();
      unsubscribe();
    };
  }, [delay]);

  return state;
}
