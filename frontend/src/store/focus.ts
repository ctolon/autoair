import { create } from "zustand";

import type { Problem } from "../api/gen/types.gen";
import { useLayout } from "../shell/layout";

type FocusRequest = { nodeId: string | null; field: string | null; code: string; seq: number };

// Monotonic across clears: consumers remember the last seq they handled.
let counter = 0;

/**
 * "Take me to this problem": the inspector (task or DAG settings) consumes the request, opens
 * the tab owning the field and focuses its control (see `focusField`).
 */
export const useFocusRequest = create<{
  request: FocusRequest | null;
  focus: (p: Pick<Problem, "node_id" | "field" | "code">) => void;
  clear: () => void;
}>()((set) => ({
  request: null,
  focus: (p) => {
    // The target lives in the inspector: make sure it is visible.
    useLayout.getState().set("inspector", false);
    set({
      request: { nodeId: p.node_id ?? null, field: p.field ?? null, code: p.code, seq: ++counter },
    });
  },
  clear: () => set({ request: null }),
}));
