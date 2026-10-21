import { Alert, Badge, Box, Collapsible, HStack, List, Spinner, Stack, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { LuChevronRight } from "react-icons/lu";

import { previewSource } from "../../api/gen/sdk.gen";
import type { DagSpec, SourcePlan } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { useDebounced } from "../../hooks/useDebounced";
import { policyApplies } from "../../lib/sourcePolicy";
import { useMeta } from "../hooks";
import { UnifiedDiff } from "./UnifiedDiff";

const KNOWN_ACTIONS = new Set(["generate", "preserve", "unchanged", "blocked", "refused", "not_applicable"]);
const KNOWN_STATES = new Set(["absent", "clean", "hand_written", "ejected", "foreign"]);
const KNOWN_CODES = new Set([
  "unmappable",
  "unsupported",
  "risky",
  "conflict",
  "roundtrip",
  "syntax",
  "import",
  "policy",
  "permission",
  "ejected",
  "foreign",
]);

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/** Target states a deploy does not regenerate on its own (each has its own wording). */
export const KEPT_STATES = new Set(["hand_written", "ejected", "foreign"]);

/** The plan's target state, "absent" for anything missing or unknown (e.g. a newer server). */
export function planState(plan: Pick<SourcePlan, "target_state"> | null | undefined): string {
  const value = plan?.target_state;
  return typeof value === "string" && KNOWN_STATES.has(value) ? value : "absent";
}

/** The plan's options (what the user can do next), ignoring malformed entries. */
export function planOptions(plan: Pick<SourcePlan, "options"> | null | undefined): Set<string> {
  return new Set(strings(plan?.options));
}

/**
 * A blocked generate on the hand-written file AutoAir keeps, without the overwrite offer:
 * regenerating it needs preserve.allow_force_overwrite and deploy:overwrite_ejected (the
 * server offers "overwrite" only where both apply).
 */
export function needsOverwritePermission(plan: SourcePlan | null | undefined): boolean {
  return plan?.action === "blocked" && planState(plan) === "hand_written" && !planOptions(plan).has("overwrite");
}

/** What a deploy writes as the DAG file: policy, action, refusals, warnings and the diff. */
export function SourcePlanSummary({
  plan,
  showDiff = true,
  showState = true,
}: {
  plan: SourcePlan | null | undefined;
  showDiff?: boolean;
  /** Say when the file was changed outside AutoAir (off where the context already says it). */
  showState?: boolean;
}) {
  const { t } = useTranslation("lifecycle");
  // Overwrites are off unless the server allows them (preserve.allow_force_overwrite).
  const forceOn = useMeta()?.allow_force_overwrite === true;
  if (!plan || typeof plan !== "object") return null;
  const action = KNOWN_ACTIONS.has(plan.action ?? "") ? (plan.action as string) : "generate";
  const state = planState(plan);
  const stopped = action === "refused" || action === "blocked";
  // A blocked plan says why per state: a kept hand-written file was not "changed outside".
  const actionText =
    action === "blocked" && KEPT_STATES.has(state) ? t(`source.blockedAction.${state}`) : t(`source.action.${action}`);
  const warnings = strings(plan.warnings);
  const operations = strings(plan.operations);
  const refusals = Array.isArray(plan.refusals) ? plan.refusals.filter((r) => r && typeof r === "object") : [];
  if (plan.applicable === false) {
    return (
      <Text fontSize="xs" color="fg.muted" data-testid="source-plan">
        {t("source.notApplicable")}
      </Text>
    );
  }
  return (
    <Stack gap={2} data-testid="source-plan" data-action={action}>
      <HStack gap={2} wrap="wrap">
        <Badge size="sm" variant="subtle" colorPalette={stopped ? "orange" : "blue"}>
          {t(`source.policy.${plan.policy ?? "generate"}`, { defaultValue: plan.policy ?? "" })}
        </Badge>
        <Text fontSize="sm">{actionText}</Text>
      </HStack>
      {showState && KEPT_STATES.has(state) ? (
        // Information while the deploy goes ahead (e.g. the kept file is patched); a warning
        // only when it stops there.
        <Text
          fontSize="xs"
          color={stopped ? "fg.warning" : "fg.muted"}
          data-testid="source-state"
          data-tone={stopped ? "warning" : "info"}
        >
          {t(`source.state.${state}`, { file: typeof plan.target_file === "string" ? plan.target_file : "" })}
        </Text>
      ) : null}
      {needsOverwritePermission(plan) ? (
        <Text fontSize="xs" color="fg.warning" data-testid="source-overwrite-permission">
          {forceOn ? t("source.overwritePermission") : t("source.overwriteOff")}
        </Text>
      ) : null}
      {plan.content_hidden === true ? (
        <Text fontSize="xs" color="fg.muted" data-testid="source-content-hidden">
          {t("source.contentHidden")}
        </Text>
      ) : null}
      {refusals.length ? (
        <Alert.Root status="warning" size="sm">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title fontSize="sm">{t("source.refusalsTitle")}</Alert.Title>
            <List.Root fontSize="xs" ps={4} mt={1} data-testid="source-refusals">
              {refusals.map((r, i) => (
                <List.Item key={i}>
                  <Badge size="xs" variant="outline" me={1}>
                    {KNOWN_CODES.has(r.code) ? t(`source.code.${r.code}`) : typeof r.code === "string" ? r.code : "?"}
                  </Badge>
                  {typeof r.message === "string" ? r.message : null}
                </List.Item>
              ))}
            </List.Root>
          </Alert.Content>
        </Alert.Root>
      ) : null}
      {warnings.length ? (
        <List.Root fontSize="xs" ps={4} color="fg.warning" data-testid="source-warnings">
          {warnings.map((w, i) => (
            <List.Item key={i}>{w}</List.Item>
          ))}
        </List.Root>
      ) : null}
      {operations.length ? (
        <Collapsible.Root>
          <Collapsible.Trigger asChild>
            <HStack as="button" gap={1} fontSize="xs" color="fg.muted" cursor="pointer">
              <LuChevronRight />
              {t("source.operations", { count: operations.length })}
            </HStack>
          </Collapsible.Trigger>
          <Collapsible.Content>
            <List.Root fontSize="xs" ps={6} color="fg.muted">
              {operations.map((o, i) => (
                <List.Item key={i}>{o}</List.Item>
              ))}
            </List.Root>
          </Collapsible.Content>
        </Collapsible.Root>
      ) : null}
      {showDiff && !stopped ? (
        <UnifiedDiff diff={plan.diff} truncated={plan.diff_truncated} label={t("source.diffLabel")} />
      ) : null}
    </Stack>
  );
}

/**
 * The file a deploy of ``spec`` would write, previewed on the server (``POST
 * /specs/{dag_id}/source-preview``). Preserve previews import the DAG file in a sandbox, so
 * this can take a few seconds; failures are shown inline and never block the deploy.
 */
export function SourcePreview({
  spec,
  environment,
  resolution,
  showState = true,
  onPlan,
}: {
  spec: DagSpec;
  environment: string | null;
  resolution?: "preserve" | "overwrite" | null;
  showState?: boolean;
  /** The plan of the shown preview (the deploy binds to its target file). */
  onPlan?: (plan: SourcePlan | null) => void;
}) {
  const { t } = useTranslation("lifecycle");
  const applies = policyApplies(spec);
  // Each preview may import the file on the server: wait until the edits pause.
  const debounced = useDebounced(spec, 400);
  const query = useQuery({
    queryKey: ["lifecycle", "sourcePreview", debounced, environment, resolution ?? null],
    enabled: applies,
    staleTime: 30_000,
    retry: false,
    queryFn: async ({ signal }) => {
      const { data } = await previewSource({
        path: { dag_id: debounced.dag_id },
        body: { spec: debounced, environment, ...(resolution ? { source_resolution: resolution } : {}) },
        signal,
        throwOnError: true,
      });
      return data;
    },
  });
  const plan = query.data ?? null;
  useEffect(() => {
    onPlan?.(plan);
  }, [plan, onPlan]);
  if (!applies) {
    return (
      <Text fontSize="xs" color="fg.muted" data-testid="source-plan">
        {t("source.notApplicable")}
      </Text>
    );
  }
  if (query.isPending) {
    return (
      <HStack gap={2} fontSize="xs" color="fg.muted" data-testid="source-preview-loading">
        <Spinner size="xs" />
        <Text>{t("source.previewing")}</Text>
      </HStack>
    );
  }
  if (query.isError) {
    return (
      <Text fontSize="xs" color="fg.muted" data-testid="source-preview-error">
        {t("source.previewFailed", { error: errorMessage(query.error) })}
      </Text>
    );
  }
  return (
    <Box>
      <SourcePlanSummary plan={query.data} showState={showState} />
    </Box>
  );
}
