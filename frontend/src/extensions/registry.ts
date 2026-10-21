import type { ComponentType } from "react";

import type { CustomSpec } from "../lib/types";

/**
 * Frontend extension point. A backend node-kind extension works out of the box (its
 * config form is generated from the JSON Schema it publishes); register a renderer here
 * only to customise how its nodes look on the canvas or in the inspector.
 */
export type NodeKindExtension = {
  handler: string;
  icon?: ComponentType<{ size?: number }>;
  color?: string;
  /** Short text rendered under the node title. */
  describe?: (spec: CustomSpec) => string | undefined;
  /** Replaces the generated config form. */
  Inspector?: ComponentType<{ spec: CustomSpec; onChange: (config: CustomSpec["config"]) => void }>;
};

const extensions = new Map<string, NodeKindExtension>();

export function registerNodeKind(ext: NodeKindExtension): void {
  extensions.set(ext.handler, ext);
}

export function nodeKindExtension(handler: string): NodeKindExtension | undefined {
  return extensions.get(handler);
}
