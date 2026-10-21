import { Box, Button, HStack, Spinner, Table, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { LuEraser, LuEyeOff, LuHistory, LuPlay, LuRefreshCw } from "react-icons/lu";
import { useAccess } from "../../lib/access";

import { AnalyzeWithAiButton } from "../../ai/AnalyzeButton";
import { listDagRunsOptions } from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import { useBuilder } from "../../store/builder";
import { useDagInAirflow } from "../hooks";
import { StateBadge } from "../components/StateBadge";
import { formatDuration } from "../lib/dates";
import { useLifecycle } from "../store";
import { useSelectedRunOverlay } from "../useRunOverlaySync";

/** DAG runs from Airflow; selecting one paints its task states on the canvas. */
export default function RunsTab() {
  const { t, i18n } = useTranslation("lifecycle");
  const access = useAccess();
  const dagId = useBuilder((s) => s.savedDagId);
  const inAirflow = useDagInAirflow(dagId);
  const selected = useLifecycle((s) => s.selectedRunId);
  const setSelected = useLifecycle((s) => s.setSelectedRunId);
  const openDialog = useLifecycle((s) => s.openDialog);
  const overlay = useSelectedRunOverlay();
  const runs = useQuery({
    ...listDagRunsOptions({ path: { dag_id: dagId ?? "_" }, query: { limit: 50 } }),
    enabled: inAirflow,
    retry: false,
    refetchInterval: 10_000,
  });
  // The overlay stops polling once its run looks finished; when the runs list (polled on its
  // own) reports another state for the selected run (cleared, re-run…), refresh the overlay
  // too so the canvas chips and the Task tab never lag behind this table.
  const selectedState = runs.data?.find((r) => r.dag_run_id === selected)?.state ?? null;
  const overlayState = overlay.data?.run?.state ?? null;
  const refetchOverlay = overlay.refetch;
  useEffect(() => {
    if (selectedState && overlayState && selectedState !== overlayState) void refetchOverlay();
  }, [selectedState, overlayState, refetchOverlay]);
  const fmt = useMemo(
    () => new Intl.DateTimeFormat(i18n.language, { dateStyle: "short", timeStyle: "medium" }),
    [i18n.language],
  );
  const date = (v: string | null | undefined) => (v ? fmt.format(new Date(v)) : "—");
  const duration = (a: string | null | undefined, b: string | null | undefined) =>
    a && b ? formatDuration((new Date(b).getTime() - new Date(a).getTime()) / 1000) : "—";

  if (!dagId) {
    return (
      <Text fontSize="sm" color="fg.muted" p={3}>
        {t("common.deployFirst")}
      </Text>
    );
  }
  return (
    <Box>
      <HStack px={3} py={1.5} gap={2} borderBottomWidth="1px" wrap="wrap">
        <Button
          size="xs"
          colorPalette="green"
          hidden={!access.feature("runs")}
          disabled={!access.can("run:trigger")}
          title={access.can("run:trigger") ? undefined : t("access.denied", { permission: "run:trigger" })}
          onClick={() => openDialog({ kind: "trigger" })}
        >
          <LuPlay /> {t("menu.trigger")}
        </Button>
        <Button
          size="xs"
          variant="outline"
          hidden={!access.feature("runs")}
          disabled={!access.can("run:manage")}
          title={access.can("run:manage") ? undefined : t("access.denied", { permission: "run:manage" })}
          onClick={() => openDialog({ kind: "backfill" })}
        >
          <LuHistory /> {t("menu.backfill")}
        </Button>
        <Button
          size="xs"
          variant="outline"
          disabled={!selected || !access.can("run:manage")}
          hidden={!access.feature("runs")}
          title={access.can("run:manage") ? undefined : t("access.denied", { permission: "run:manage" })}
          onClick={() => selected && openDialog({ kind: "clear", runId: selected })}
        >
          <LuEraser /> {t("runs.clear")}
        </Button>
        <Button size="xs" variant="ghost" disabled={!selected} onClick={() => setSelected(null)}>
          <LuEyeOff /> {t("runs.clearOverlay")}
        </Button>
        <AnalyzeWithAiButton target={selected ? { dagId, runId: selected } : null} />
        <Box flex="1" />
        {overlay.isFetching || runs.isFetching ? <Spinner size="xs" /> : null}
        <Button
          size="xs"
          variant="ghost"
          aria-label={t("common.refresh")}
          onClick={() => {
            void runs.refetch();
            if (selected) void overlay.refetch();
          }}
        >
          <LuRefreshCw />
        </Button>
      </HStack>
      {runs.isError ? (
        <Text fontSize="sm" color="red.500" p={3}>
          {t("airflow.unreachable", { message: errorMessage(runs.error) })}
        </Text>
      ) : null}
      {overlay.isError ? (
        <Text fontSize="xs" color="red.500" px={3} pt={2}>
          {errorMessage(overlay.error)}
        </Text>
      ) : null}
      {runs.data && !runs.data.length ? (
        <Text fontSize="sm" color="fg.muted" p={3}>
          {t("runs.empty")}
        </Text>
      ) : null}
      {runs.data?.length ? (
        <Table.Root size="sm" interactive>
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>{t("runs.run")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("runs.state")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("runs.type")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("runs.logicalDate")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("runs.started")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("runs.duration")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("runs.bundle")}</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {runs.data.map((r) => {
              const isSel = r.dag_run_id === selected;
              return (
                <Table.Row
                  key={r.dag_run_id}
                  cursor="pointer"
                  bg={isSel ? "blue.subtle" : undefined}
                  aria-selected={isSel}
                  tabIndex={0}
                  onClick={() => setSelected(isSel ? null : r.dag_run_id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setSelected(isSel ? null : r.dag_run_id);
                    }
                  }}
                >
                  <Table.Cell fontFamily="mono" fontSize="xs" title={r.note ?? undefined}>
                    {r.dag_run_id}
                  </Table.Cell>
                  <Table.Cell>
                    <StateBadge state={r.state} />
                  </Table.Cell>
                  <Table.Cell fontSize="xs">{r.run_type ?? ""}</Table.Cell>
                  <Table.Cell fontSize="xs">{date(r.logical_date)}</Table.Cell>
                  <Table.Cell fontSize="xs">{date(r.start_date)}</Table.Cell>
                  <Table.Cell fontSize="xs">{duration(r.start_date, r.end_date)}</Table.Cell>
                  <Table.Cell fontSize="xs" fontFamily="mono" maxW="120px" truncate>
                    {r.bundle_version ?? ""}
                  </Table.Cell>
                </Table.Row>
              );
            })}
          </Table.Body>
        </Table.Root>
      ) : null}
    </Box>
  );
}
