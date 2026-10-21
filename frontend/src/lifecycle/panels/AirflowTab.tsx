import { Badge, Box, Button, Code, HStack, NativeSelect, Spinner, Stack, Table, Text } from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { LuPlay } from "react-icons/lu";

import {
  getDagStatusOptions,
  getDagStatusQueryKey,
  listDagBundlesOptions,
  listImportErrorsOptions,
  setDagSchedulingStateMutation,
} from "../../api/gen/@tanstack/react-query.gen";
import type { SchedulingStateRequest } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { useAccess } from "../../lib/access";
import { useBuilder } from "../../store/builder";
import { toaster } from "../../ui/toaster";
import { useAirflowCapabilities } from "../capabilities";
import { pretty } from "../components/JsonBlock";
import { StateBadge } from "../components/StateBadge";
import { TriggerEndpointCard } from "../components/TriggerEndpointCard";
import { useLifecycle } from "../store";

type SchedulingState = SchedulingStateRequest["state"];
const SCHEDULING_STATES: readonly SchedulingState[] = ["active", "draining", "paused"];

/** Airflow >= 3.4: active / draining (no new scheduled runs, pauses once idle) / paused. */
function SchedulingStateControl({ dagId, state }: { dagId: string; state: string | null | undefined }) {
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const access = useAccess();
  const caps = useAirflowCapabilities();
  const change = useMutation({
    ...setDagSchedulingStateMutation(),
    onSuccess: (status) => {
      qc.setQueryData(getDagStatusQueryKey({ path: { dag_id: dagId } }), status);
      toaster.create({ type: "success", title: t("airflow.schedulingChanged") });
    },
    onError: (e) =>
      toaster.create({ type: "error", title: t("airflow.schedulingFailed"), description: errorMessage(e) }),
  });
  if (!caps.scheduling_state || !access.feature("runs")) return null;
  const allowed = access.can("run:manage");
  const current = SCHEDULING_STATES.includes(state as SchedulingState) ? (state as SchedulingState) : "";
  return (
    <HStack gap={1} data-testid="scheduling-state">
      <Text fontSize="xs" color="fg.muted">
        {t("airflow.schedulingState")}
      </Text>
      <NativeSelect.Root size="xs" width="auto" disabled={!allowed || change.isPending}>
        <NativeSelect.Field
          aria-label={t("airflow.schedulingState")}
          title={allowed ? t("airflow.schedulingHelp") : t("access.denied", { permission: "run:manage" })}
          value={current}
          onChange={(e) => {
            const next = e.target.value as SchedulingState;
            if (SCHEDULING_STATES.includes(next) && next !== current) {
              change.mutate({ path: { dag_id: dagId }, body: { state: next } });
            }
          }}
        >
          {current ? null : <option value="">—</option>}
          {SCHEDULING_STATES.map((st) => (
            <option key={st} value={st}>
              {t(`airflow.scheduling.${st}`)}
            </option>
          ))}
        </NativeSelect.Field>
        <NativeSelect.Indicator />
      </NativeSelect.Root>
    </HStack>
  );
}

const str = (v: unknown) => (v === null || v === undefined ? "" : String(v));

/** Airflow's view of the deployed DAG: parse status, import errors, latest runs, bundles. */
export default function AirflowTab() {
  const { t, i18n } = useTranslation("lifecycle");
  const dagId = useBuilder((s) => s.savedDagId);
  const status = useQuery({
    ...getDagStatusOptions({ path: { dag_id: dagId ?? "_" } }),
    enabled: !!dagId,
    refetchInterval: 5000,
    retry: false,
  });
  const fileName = status.data?.fileloc?.split("/").pop() ?? `${dagId ?? ""}.py`;
  const importErrors = useQuery({
    ...listImportErrorsOptions({ query: { filename: fileName } }),
    enabled: !!dagId,
    refetchInterval: 10000,
    retry: false,
  });
  const bundles = useQuery({ ...listDagBundlesOptions(), retry: false, staleTime: 60_000 });
  const date = (v: unknown) => (v ? new Date(String(v)).toLocaleString(i18n.language) : "");

  if (!dagId) {
    return (
      <Text fontSize="sm" color="fg.muted" p={3}>
        {t("common.deployFirst")}
      </Text>
    );
  }
  if (status.isError) {
    return (
      <Text fontSize="sm" color="red.500" p={3}>
        {t("airflow.unreachable", { message: errorMessage(status.error) })}
      </Text>
    );
  }
  const s = status.data;
  return (
    <Stack p={3} gap={3}>
      <HStack gap={3} wrap="wrap">
        <Text fontWeight="semibold" fontFamily="mono">
          {dagId}
        </Text>
        {!s ? (
          <Spinner size="xs" />
        ) : s.exists ? (
          <Badge colorPalette="green">{t("airflow.parsed")}</Badge>
        ) : (
          <Badge colorPalette="orange">{t("airflow.waiting")}</Badge>
        )}
        {s?.is_paused ? <Badge>{t("airflow.paused")}</Badge> : null}
        {s?.scheduling_state === "draining" ? (
          <Badge colorPalette="orange">{t("airflow.scheduling.draining")}</Badge>
        ) : null}
        {s?.last_parsed_time ? (
          <Text fontSize="xs" color="fg.muted">
            {t("airflow.lastParsed", { time: date(s.last_parsed_time) })}
          </Text>
        ) : null}
        <Button
          size="xs"
          colorPalette="green"
          disabled={!s?.exists}
          onClick={() => useLifecycle.getState().openDialog({ kind: "trigger" })}
        >
          <LuPlay /> {t("menu.trigger")}
        </Button>
        {s?.exists ? <SchedulingStateControl dagId={dagId} state={s.scheduling_state} /> : null}
      </HStack>
      {s?.fileloc ? (
        <Text fontSize="xs" color="fg.muted" fontFamily="mono">
          {s.fileloc}
        </Text>
      ) : null}
      {(importErrors.data ?? []).map((e) => (
        <Box key={e.import_error_id ?? e.filename} borderWidth="1px" borderColor="red.500" rounded="md" p={2}>
          <Text fontSize="xs" fontWeight="bold" color="red.500">
            {t("airflow.importError", { file: e.filename ?? "" })}
          </Text>
          <Code as="pre" display="block" fontSize="xs" whiteSpace="pre-wrap" bg="transparent">
            {e.stack_trace}
          </Code>
        </Box>
      ))}
      {s?.latest_runs?.length ? (
        <Table.Root size="sm">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>{t("runs.run")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("runs.state")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("runs.runAfter")}</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {s.latest_runs.map((r) => (
              <Table.Row
                key={str(r.dag_run_id)}
                cursor="pointer"
                onClick={() => {
                  const lc = useLifecycle.getState();
                  lc.setSelectedRunId(str(r.dag_run_id));
                  lc.setBottomTab("runs");
                }}
              >
                <Table.Cell fontFamily="mono" fontSize="xs">
                  {str(r.dag_run_id)}
                </Table.Cell>
                <Table.Cell>
                  <StateBadge state={str(r.state) || null} />
                </Table.Cell>
                <Table.Cell fontSize="xs">{date(r.run_after)}</Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
      ) : null}
      {s?.exists ? <TriggerEndpointCard dagId={dagId} /> : null}
      {bundles.data?.length ? (
        <Box>
          <Text fontSize="xs" fontWeight="bold" textTransform="uppercase" color="fg.muted" mb={1}>
            {t("airflow.bundles")}
          </Text>
          <Stack gap={1}>
            {bundles.data.map((b, i) => (
              <HStack key={str(b.name) || i} fontSize="xs" gap={2}>
                <Text fontFamily="mono" fontWeight="semibold">
                  {str(b.name)}
                </Text>
                <Text color="fg.muted" truncate>
                  {b.version
                    ? str(b.version)
                    : pretty(Object.fromEntries(Object.entries(b).filter(([k]) => k !== "name")))}
                </Text>
              </HStack>
            ))}
          </Stack>
        </Box>
      ) : null}
    </Stack>
  );
}
