import { queryOptions, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { getBlueprintOptions, listBlueprintsOptions } from "../api/gen/@tanstack/react-query.gen";
import type { BlueprintDetail, BlueprintPreview, BlueprintSummary } from "../api/gen/types.gen";
import { previewBlueprint } from "../api/gen/sdk.gen";
import { useAccess } from "../lib/access";
import { canonicalJson } from "../lib/canonical";
import type { BlueprintSpec } from "../lib/types";

/** Astronomer Blueprint catalog (palette): only while `features.blueprints` is on. */
export function useBlueprintSummaries() {
  const on = useAccess().feature("blueprints");
  return useQuery({ ...listBlueprintsOptions(), enabled: on, retry: false, staleTime: 60_000 });
}

/** One blueprint version (config schema, default preview); shared by canvas and inspector. */
export function useBlueprintDetail(name: string, version: number) {
  const on = useAccess().feature("blueprints");
  const valid = /^[a-z][a-z0-9_]*$/.test(name) && Number.isInteger(version) && version >= 1;
  return useQuery({
    ...getBlueprintOptions({ path: { name, version } }),
    enabled: on && valid,
    retry: false,
    staleTime: 60_000,
  });
}

/** Versions of `name` in the catalog, newest first. */
export function versionsOf(summaries: readonly BlueprintSummary[] | undefined, name: string): number[] {
  return (summaries ?? [])
    .filter((s) => s.name === name)
    .map((s) => s.version)
    .sort((a, b) => b - a);
}

type PreviewInput = Pick<BlueprintSpec, "blueprint" | "step_id" | "config"> & { version?: number | undefined };

const plainConfig = (config: unknown): Record<string, unknown> =>
  config && typeof config === "object" && !Array.isArray(config) ? (config as Record<string, unknown>) : {};

/**
 * Query of the task preview of one step. The key holds the config, so the canvas node shows
 * the preview the inspector fetched for exactly that config (and nothing for another one).
 */
export function blueprintPreviewOptions(input: PreviewInput) {
  const config = plainConfig(input.config);
  return queryOptions<BlueprintPreview>({
    queryKey: ["blueprintPreview", input.blueprint, input.version, input.step_id, canonicalJson(config)],
    queryFn: async ({ signal }) =>
      (
        await previewBlueprint({
          body: { name: input.blueprint, version: input.version ?? 1, step_id: input.step_id, config },
          signal,
          throwOnError: true,
        })
      ).data,
    staleTime: Infinity,
    retry: false,
  });
}

const isKey = (v: string) => /^[A-Za-z0-9_.-]+$/.test(v);

/** Preview for the canvas node: what the inspector already fetched, never a new request. */
export function useCachedPreview(spec: BlueprintSpec) {
  return useQuery({ ...blueprintPreviewOptions(spec), enabled: false });
}

/** Preview for the inspector: fetched (debounced) whenever name, version, step id or config change. */
export function useLivePreview(spec: BlueprintSpec, detail: BlueprintDetail | undefined, delayMs = 800) {
  const on = useAccess().feature("blueprints");
  const [debounced, setDebounced] = useState<PreviewInput>(spec);
  const key = canonicalJson([spec.blueprint, spec.version, spec.step_id, plainConfig(spec.config)]);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(spec), delayMs);
    return () => clearTimeout(timer);
    // `key` captures every input; `spec` itself changes identity on unrelated edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, delayMs]);
  const enabled = on && !!detail?.config_schema && isKey(debounced.step_id ?? "");
  return useQuery({ ...blueprintPreviewOptions(debounced), enabled });
}

/** Tasks to draw inside the node: a fetched preview, else the catalog's default one (empty config). */
export function previewTasks(
  spec: BlueprintSpec,
  cached: BlueprintPreview | undefined,
  detail: BlueprintDetail | undefined,
): BlueprintPreview | null {
  if (cached) return cached;
  const empty = Object.keys(plainConfig(spec.config)).length === 0;
  return empty && detail?.preview ? detail.preview : null;
}
