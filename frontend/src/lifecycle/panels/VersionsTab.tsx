import { Badge, Box, Button, HStack, Spinner, Stack, Tabs, Text, chakra } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuEyeOff, LuGitCompareArrows, LuRotateCcw } from "react-icons/lu";

import { listDagVersionsOptions, listVersionsOptions } from "../../api/gen/@tanstack/react-query.gen";
import { diffVersions, diffWithVersion, generateCode, getVersion } from "../../api/gen/sdk.gen";
import type { DagSpec, SpecDiff, VersionInfo } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { useBuilder } from "../../store/builder";
import { useDagInAirflow } from "../hooks";
import { ReviewsPanel } from "./ReviewsPanel";
import { toaster } from "../../ui/toaster";
import { confirmDiscard, loadAsUnsaved } from "../actions";
import DiffView from "../components/DiffView";
import { DiffSummary } from "../components/DiffSummary";

type Comparison = { version: VersionInfo; spec: DagSpec; current: DagSpec; diff: SpecDiff };

/** Node ids for the canvas diff overlay (layout-only changes are not highlighted). */
export function overlayOf(diff: SpecDiff, label: string) {
  const ids = (list: Record<string, unknown>[] | undefined) => (list ?? []).map((n) => String(n.id));
  return {
    label,
    added: ids(diff.added_nodes),
    removed: ids(diff.removed_nodes),
    changed: (diff.changed_nodes ?? []).filter((n) => !n.layout_only).map((n) => n.id),
  };
}

function CodeDiff({ before, after }: { before: DagSpec; after: DagSpec }) {
  const { t } = useTranslation("lifecycle");
  const code = useQuery({
    queryKey: ["lifecycle", "code-diff", before, after],
    staleTime: Infinity,
    queryFn: async ({ signal }) => {
      const [a, b] = await Promise.all([
        generateCode({ body: before, signal, throwOnError: true }),
        generateCode({ body: after, signal, throwOnError: true }),
      ]);
      return { a: a.data.code ?? "", b: b.data.code ?? "" };
    },
  });
  if (code.isPending) return <Spinner size="sm" />;
  if (code.isError) return <Text color="red.500">{errorMessage(code.error)}</Text>;
  return <DiffView before={code.data.a} after={code.data.b} language="python" label={t("versions.codeDiff")} />;
}

/** Spec history (storage) and Airflow DAG versions; compare / restore. */
export default function VersionsTab() {
  const { t, i18n } = useTranslation("lifecycle");
  const dagId = useBuilder((s) => s.savedDagId);
  const inAirflow = useDagInAirflow(dagId);
  const dirty = useBuilder((s) => s.dirty);
  const diffOverlay = useBuilder((s) => s.diffOverlay);
  const history = useQuery({
    ...listVersionsOptions({ path: { dag_id: dagId ?? "_" }, query: { limit: 50 } }),
    enabled: !!dagId,
    retry: false,
  });
  const airflow = useQuery({
    ...listDagVersionsOptions({ path: { dag_id: dagId ?? "_" } }),
    enabled: inAirflow,
    retry: false,
  });
  const [selected, setSelected] = useState<VersionInfo | null>(null);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [busy, setBusy] = useState<"compare" | "restore" | null>(null);
  const fmt = useMemo(
    () => new Intl.DateTimeFormat(i18n.language, { dateStyle: "medium", timeStyle: "short" }),
    [i18n.language],
  );

  if (!dagId) {
    return (
      <Text fontSize="sm" color="fg.muted" p={3}>
        {t("common.deployFirst")}
      </Text>
    );
  }

  const compare = async (v: VersionInfo) => {
    setBusy("compare");
    try {
      const { data: spec } = await getVersion({ path: { dag_id: dagId, version: v.version }, throwOnError: true });
      const current = useBuilder.getState().toSpec();
      // A clean document equals the stored head: let the server diff the stored versions.
      const diff = dirty
        ? (await diffVersions({ body: { a: spec, b: current }, throwOnError: true })).data
        : (await diffWithVersion({ path: { dag_id: dagId }, query: { from: v.version }, throwOnError: true })).data;
      setComparison({ version: v, spec, current, diff });
      useBuilder
        .getState()
        .setDiffOverlay(overlayOf(diff, t("versions.overlayLabel", { version: v.version.slice(0, 10) })));
    } catch (e) {
      toaster.create({ type: "error", title: t("versions.compareFailed"), description: errorMessage(e) });
    } finally {
      setBusy(null);
    }
  };

  const restore = async (v: VersionInfo) => {
    if (!confirmDiscard()) return;
    setBusy("restore");
    try {
      const { data: spec } = await getVersion({ path: { dag_id: dagId, version: v.version }, throwOnError: true });
      // Restoring keeps the stored identity: deploying it creates a new version on top of head.
      loadAsUnsaved(spec);
      useBuilder.getState().setDiffOverlay(null);
      setComparison(null);
      toaster.create({ type: "info", title: t("versions.restored", { version: v.version.slice(0, 10) }) });
    } catch (e) {
      toaster.create({ type: "error", title: t("versions.restoreFailed"), description: errorMessage(e) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <Stack direction={{ base: "column", md: "row" }} gap={0} h="100%" align="stretch">
      <Box w={{ base: "100%", md: "300px" }} flexShrink={0} borderRightWidth={{ md: "1px" }} overflowY="auto">
        <Tabs.Root defaultValue="history" size="sm" variant="line">
          <Tabs.List px={2}>
            <Tabs.Trigger value="history">{t("versions.history")}</Tabs.Trigger>
            <Tabs.Trigger value="airflow">{t("versions.airflow")}</Tabs.Trigger>
            <Tabs.Trigger value="reviews">{t("reviews.tab")}</Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="reviews" p={0}>
            <ReviewsPanel />
          </Tabs.Content>
          <Tabs.Content value="history" p={0}>
            {history.isPending ? <Spinner size="sm" m={3} /> : null}
            {history.isError ? (
              <Text fontSize="xs" color="red.500" p={3}>
                {errorMessage(history.error)}
              </Text>
            ) : null}
            {history.data && !history.data.length ? (
              <Text fontSize="sm" color="fg.muted" p={3}>
                {t("versions.empty")}
              </Text>
            ) : null}
            <Stack as="ul" gap={0} listStyleType="none" m={0} p={0}>
              {(history.data ?? []).map((v, i) => (
                <li key={v.version}>
                  <chakra.button
                    type="button"
                    w="100%"
                    textAlign="start"
                    px={3}
                    py={1.5}
                    borderBottomWidth="1px"
                    bg={selected?.version === v.version ? "blue.subtle" : undefined}
                    _hover={{ bg: "bg.muted" }}
                    aria-pressed={selected?.version === v.version}
                    onClick={() => setSelected(v)}
                  >
                    <HStack gap={2}>
                      <Text fontFamily="mono" fontSize="xs">
                        {v.version.slice(0, 10)}
                      </Text>
                      {i === 0 ? (
                        <Badge size="xs" colorPalette="green">
                          {t("versions.latest")}
                        </Badge>
                      ) : null}
                      <Box flex="1" />
                      <Text fontSize="xs" color="fg.muted">
                        {v.timestamp ? fmt.format(new Date(v.timestamp)) : ""}
                      </Text>
                    </HStack>
                    <Text fontSize="xs" lineClamp={2}>
                      {v.message || (
                        <Text as="span" color="fg.muted">
                          {t("versions.noMessage")}
                        </Text>
                      )}
                    </Text>
                    {v.author ? (
                      <Text fontSize="xs" color="fg.muted">
                        {v.author}
                      </Text>
                    ) : null}
                  </chakra.button>
                </li>
              ))}
            </Stack>
          </Tabs.Content>
          <Tabs.Content value="airflow" p={0}>
            {airflow.isError ? (
              <Text fontSize="xs" color="red.500" p={3}>
                {errorMessage(airflow.error)}
              </Text>
            ) : null}
            {airflow.data && !airflow.data.length ? (
              <Text fontSize="sm" color="fg.muted" p={3}>
                {t("versions.airflowEmpty")}
              </Text>
            ) : null}
            <Stack as="ul" gap={0} listStyleType="none" m={0} p={0}>
              {(airflow.data ?? []).map((v) => (
                <Box as="li" key={v.version_number} px={3} py={1.5} borderBottomWidth="1px">
                  <HStack>
                    <Badge size="xs">v{v.version_number}</Badge>
                    <Text fontSize="xs" color="fg.muted">
                      {v.created_at ? fmt.format(new Date(v.created_at)) : ""}
                    </Text>
                  </HStack>
                  <Text fontSize="xs" fontFamily="mono" truncate title={v.bundle_version ?? undefined}>
                    {v.bundle_name ?? ""} {v.bundle_version ? `@ ${v.bundle_version}` : ""}
                  </Text>
                </Box>
              ))}
            </Stack>
          </Tabs.Content>
        </Tabs.Root>
      </Box>
      <Box flex="1" minW={0} overflowY="auto" p={3}>
        <HStack gap={2} wrap="wrap" mb={3}>
          <Button
            size="xs"
            variant="outline"
            disabled={!selected}
            loading={busy === "compare"}
            onClick={() => selected && void compare(selected)}
          >
            <LuGitCompareArrows /> {t("versions.compare")}
          </Button>
          <Button
            size="xs"
            variant="outline"
            disabled={!selected}
            loading={busy === "restore"}
            onClick={() => selected && void restore(selected)}
          >
            <LuRotateCcw /> {t("versions.restore")}
          </Button>
          <Button
            size="xs"
            variant="ghost"
            disabled={!diffOverlay}
            onClick={() => {
              useBuilder.getState().setDiffOverlay(null);
              setComparison(null);
            }}
          >
            <LuEyeOff /> {t("versions.clearOverlay")}
          </Button>
        </HStack>
        {!comparison ? (
          <Text fontSize="sm" color="fg.muted">
            {t("versions.hint")}
          </Text>
        ) : (
          <Stack gap={3}>
            <Text fontSize="sm" fontWeight="semibold">
              {t("versions.comparing", { version: comparison.version.version.slice(0, 10) })}
            </Text>
            <DiffSummary diff={comparison.diff} />
            {!comparison.diff.identical ? <CodeDiff before={comparison.spec} after={comparison.current} /> : null}
          </Stack>
        )}
      </Box>
    </Stack>
  );
}
