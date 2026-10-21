import { Badge, NativeSelect, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useShallow } from "zustand/react/shallow";

import { generateCode } from "../api/gen/sdk.gen";
import { errorMessage } from "../api/setup";
import { useBuilder } from "../store/builder";
import { toSpec } from "../store/convert";
import { isRawDag, useCodeMode } from "./store";

/** Environments of the open document that the code preview can render (none for raw code). */
function usePreviewEnvironments(): string[] {
  return useBuilder(useShallow((s) => (isRawDag(s.dag) ? [] : Object.keys(s.dag.environments ?? {}))));
}

/**
 * The environment the code is previewed for, or null for the base. An environment that no
 * longer exists (renamed, removed, another DAG opened) falls back to the base.
 */
export function usePreviewEnv(): string | null {
  const envs = usePreviewEnvironments();
  const env = useCodeMode((s) => s.previewEnv);
  const valid = env !== null && envs.includes(env);
  useEffect(() => {
    if (env !== null && !valid) useCodeMode.getState().setPreviewEnv(null);
  }, [env, valid]);
  return valid ? env : null;
}

type EnvPreviewState = { code: string | null; loading: boolean; error: string | null };

/** The DAG file rendered with an environment's overlay and mapping (read-only preview). */
export function useEnvPreviewCode(env: string | null): EnvPreviewState {
  const dag = useBuilder((s) => s.dag);
  const nodes = useBuilder((s) => s.nodes);
  const edges = useBuilder((s) => s.edges);
  const spec = useMemo(() => (env ? toSpec(dag, nodes, edges) : null), [env, dag, nodes, edges]);
  const query = useQuery({
    queryKey: ["codemode", "env-preview", env, spec],
    enabled: !!env && !!spec,
    queryFn: async ({ signal }) => {
      if (!spec || !env) return null;
      const res = await generateCode({ body: spec, query: { environment: env }, signal, throwOnError: true });
      return res.data;
    },
    staleTime: Infinity,
    placeholderData: (previous) => previous,
  });
  if (!env) return { code: null, loading: false, error: null };
  const problems = query.data?.problems ?? [];
  return {
    code: query.data?.code ?? null,
    loading: query.isPending,
    error: query.isError ? errorMessage(query.error) : problems.length ? (problems[0]?.message ?? null) : null,
  };
}

/** "Preview: Base (editable) | <env> (read-only)" selector; hidden without environments. */
export function PreviewEnvSelect() {
  const { t } = useTranslation("lifecycle");
  const envs = usePreviewEnvironments();
  const env = usePreviewEnv();
  if (!envs.length) return null;
  return (
    <NativeSelect.Root size="xs" w="auto" minW="150px">
      <NativeSelect.Field
        aria-label={t("codeMode.previewLabel")}
        title={t("codeMode.previewHelp")}
        data-testid="preview-env"
        value={env ?? ""}
        onChange={(e) => useCodeMode.getState().setPreviewEnv(e.target.value || null)}
      >
        <option value="">{t("codeMode.previewBase")}</option>
        {envs.map((e) => (
          <option key={e} value={e}>
            {t("codeMode.previewEnvOption", { env: e })}
          </option>
        ))}
      </NativeSelect.Field>
      <NativeSelect.Indicator />
    </NativeSelect.Root>
  );
}

/** Label of a read-only environment preview ("Preview: prod (read-only)"). */
export function PreviewBadge({ env }: { env: string }) {
  const { t } = useTranslation("lifecycle");
  return (
    <Badge colorPalette="purple" variant="subtle" title={t("codeMode.previewHint")} data-testid="preview-badge">
      {t("codeMode.previewTitle", { env })}
    </Badge>
  );
}

/** Why the preview is read-only. */
export function PreviewHint() {
  const { t } = useTranslation("lifecycle");
  return (
    <Text fontSize="xs" color="fg.muted" data-testid="preview-hint">
      {t("codeMode.previewHint")}
    </Text>
  );
}
