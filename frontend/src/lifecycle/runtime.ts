import type { QueryClient } from "@tanstack/react-query";

import type { MetaInfo } from "../api/gen/types.gen";
import { getMetaQueryKey } from "../api/gen/@tanstack/react-query.gen";

/**
 * Handles the lifecycle host hands to imperative actions (commands run outside React).
 * Set once by <LifecycleHost/>.
 */
type Runtime = { queryClient: QueryClient | null; fitView: (() => void) | null };
export const runtime: Runtime = { queryClient: null, fitView: null };

export function getMeta(): MetaInfo | undefined {
  return runtime.queryClient?.getQueryData<MetaInfo>(getMetaQueryKey());
}

export function fitViewSoon(): void {
  requestAnimationFrame(() => runtime.fitView?.());
}
