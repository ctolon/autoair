import { Badge, Box, Button, HStack, IconButton, Stack, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuArrowRight, LuPlus, LuTrash2 } from "react-icons/lu";

import { listConnectionsOptions, listVariablesOptions } from "../../api/gen/@tanstack/react-query.gen";
import { checkEnvironmentMapping } from "../../api/gen/sdk.gen";
import type { MappingEntry } from "../../api/gen/types.gen";
import { useAccess } from "../../lib/access";
import { jsonEqual } from "../../lib/canonical";
import { MAPPING_ID, type Mapping, logicalReferences, validEntries } from "../../lib/envMapping";
import { useBuilder } from "../../store/builder";
import { useTeamQuery } from "../../store/team";
import { Autocomplete, type AutocompleteOption } from "../../widgets/Autocomplete";
import { Hint, Panel } from "../../widgets/fields";
import { useAirflowGate } from "../../widgets/airflowMeta";

type Kind = "connections" | "variables";
type Row = { logical: string; actual: string };

const toRows = (m: Mapping): Row[] => Object.entries(m).map(([logical, actual]) => ({ logical, actual }));
const toMapping = (rows: Row[]): Mapping =>
  validEntries(Object.fromEntries(rows.map((r) => [r.logical.trim(), r.actual.trim()])));
/** Same entries (key order is not a change: it differs between the server and the editor). */
const same = (a: Mapping, b: Mapping) => jsonEqual(a, b);

function MappingRows({
  kind,
  env,
  value,
  onChange,
  logicalOptions,
  actualOptions,
  status,
}: {
  kind: Kind;
  env: string;
  value: Mapping;
  onChange: (m: Mapping) => void;
  logicalOptions: AutocompleteOption[];
  actualOptions: AutocompleteOption[];
  status: Record<string, MappingEntry> | undefined;
}) {
  const { t } = useTranslation("inspector");
  // Rows being typed (an empty side) stay local; only complete rows reach the spec.
  const [rows, setRows] = useState<Row[]>(() => toRows(value));
  // An outside change (undo, collaboration, code mode) replaces the rows; derived during
  // render instead of an effect.
  const [seen, setSeen] = useState(value);
  if (seen !== value) {
    setSeen(value);
    if (!same(toMapping(rows), value)) setRows(toRows(value));
  }
  const update = (next: Row[]) => {
    setRows(next);
    const mapping = toMapping(next);
    if (!same(mapping, value)) onChange(mapping);
  };
  const logicalCount = (id: string) => rows.filter((r) => r.logical.trim() === id).length;
  return (
    <Stack gap={1}>
      {rows.map((row, i) => {
        const entry = row.actual ? status?.[row.logical] : undefined;
        const duplicate = row.logical.trim() !== "" && logicalCount(row.logical.trim()) > 1;
        const invalid = (v: string) =>
          v.trim() !== "" && !MAPPING_ID.test(v.trim()) ? t("dag.env.mapping.invalid") : undefined;
        return (
          <HStack key={i} align="start" gap={1} data-testid={`env-mapping-${kind}-row`}>
            <Box flex="1" minW={0}>
              <Autocomplete
                label={t(`dag.env.mapping.${kind}.logical`)}
                hideLabel={i > 0}
                size="xs"
                mono
                value={row.logical}
                options={logicalOptions}
                error={duplicate ? t("dag.env.mapping.duplicate") : invalid(row.logical)}
                field={`environments.${env}.${kind}`}
                onChange={(v) => update(rows.map((r, j) => (j === i ? { ...r, logical: v ?? "" } : r)))}
              />
            </Box>
            <Box pt={i === 0 ? 6 : 1} color="fg.muted">
              <LuArrowRight aria-hidden />
            </Box>
            <Box flex="1" minW={0}>
              <Autocomplete
                label={t(`dag.env.mapping.${kind}.actual`, { env })}
                hideLabel={i > 0}
                size="xs"
                mono
                value={row.actual}
                options={actualOptions}
                error={invalid(row.actual)}
                onChange={(v) => update(rows.map((r, j) => (j === i ? { ...r, actual: v ?? "" } : r)))}
              />
              {entry?.found === false ? (
                <Hint tone="warning">{status && t(`dag.env.mapping.${kind}.missing`, { id: entry.actual })}</Hint>
              ) : null}
            </Box>
            <IconButton
              mt={i === 0 ? 5 : 0}
              aria-label={t("common.removeItem", { item: row.logical || row.actual || String(i + 1) })}
              size="2xs"
              variant="ghost"
              colorPalette="red"
              onClick={() => update(rows.filter((_, j) => j !== i))}
            >
              <LuTrash2 />
            </IconButton>
          </HStack>
        );
      })}
      <Box>
        <Button size="2xs" variant="outline" onClick={() => setRows([...rows, { logical: "", actual: "" }])}>
          <LuPlus /> {t(`dag.env.mapping.${kind}.add`)}
        </Button>
      </Box>
    </Stack>
  );
}

/**
 * `environments.<env>.connections` / `variables`: logical id (as used in the DAG) → the id in
 * this environment's Airflow. Substituted when previewing / deploying as this environment;
 * checked against the connected Airflow's metastore when it can be listed (non-blocking).
 */
export function EnvironmentMappingEditor({
  env,
  connections,
  variables,
  onChange,
}: {
  env: string;
  connections: Mapping;
  variables: Mapping;
  onChange: (patch: { connections?: Mapping; variables?: Mapping }) => void;
}) {
  const { t } = useTranslation("inspector");
  const access = useAccess();
  const airflow = access.feature("airflow");
  const { meta } = useAirflowGate();
  const team = useTeamQuery();
  const conns = useQuery({ ...listConnectionsOptions(team), retry: false, enabled: airflow, staleTime: 60_000 });
  const vars = useQuery({ ...listVariablesOptions(team), retry: false, enabled: airflow, staleTime: 60_000 });
  const dag = useBuilder((s) => s.dag);
  const nodes = useBuilder((s) => s.nodes);
  const refs = useMemo(() => logicalReferences({ dag, nodes: nodes.map((n) => n.data) }), [dag, nodes]);
  const empty = !Object.keys(connections).length && !Object.keys(variables).length;
  const check = useQuery({
    queryKey: ["autoair", "env-mapping-check", env, connections, variables],
    enabled: airflow && !empty,
    retry: false,
    staleTime: 30_000,
    queryFn: async ({ signal }) => {
      const spec = useBuilder.getState().toSpec();
      const { data } = await checkEnvironmentMapping({
        body: { spec, environment: env },
        signal,
        throwOnError: true,
      });
      return data;
    },
  });
  const byLogical = (entries: MappingEntry[] | undefined) =>
    entries ? Object.fromEntries(entries.map((e) => [e.logical, e])) : undefined;
  const suppress = meta?.secrets_backend ?? check.data?.secrets_backend ?? false;
  const connStatus = suppress ? undefined : byLogical(check.data?.connections);
  const varStatus = suppress ? undefined : byLogical(check.data?.variables);
  const missing =
    (check.data?.connections ?? []).filter((e) => e.found === false).length +
    (check.data?.variables ?? []).filter((e) => e.found === false).length;
  return (
    <Panel
      title={t("dag.env.mapping.title")}
      actions={
        !empty && airflow ? (
          check.isError ? (
            <Badge size="xs" variant="subtle" title={t("dag.env.mapping.uncheckedHelp")}>
              {t("dag.env.mapping.unchecked")}
            </Badge>
          ) : check.data && !suppress ? (
            <Badge size="xs" colorPalette={missing ? "orange" : "green"} data-testid="env-mapping-status">
              {missing ? t("dag.env.mapping.missingCount", { count: missing }) : t("dag.env.mapping.allFound")}
            </Badge>
          ) : null
        ) : null
      }
    >
      <Stack gap={2}>
        <Hint>{t("dag.env.mapping.help")}</Hint>
        <Text fontSize="xs" fontWeight="semibold">
          {t("dag.env.mapping.connections.title")}
        </Text>
        <MappingRows
          kind="connections"
          env={env}
          value={connections}
          onChange={(m) => onChange({ connections: m })}
          logicalOptions={refs.connections.map((value) => ({ value }))}
          actualOptions={(conns.data ?? []).map((c) => ({
            value: c.connection_id,
            description: [c.conn_type, c.host].filter(Boolean).join(" · "),
          }))}
          status={connStatus}
        />
        <Text fontSize="xs" fontWeight="semibold">
          {t("dag.env.mapping.variables.title")}
        </Text>
        <MappingRows
          kind="variables"
          env={env}
          value={variables}
          onChange={(m) => onChange({ variables: m })}
          logicalOptions={refs.variables.map((value) => ({ value }))}
          actualOptions={(vars.data ?? []).map((v) => ({ value: v.key, description: v.description ?? undefined }))}
          status={varStatus}
        />
        {missing && !suppress ? <Hint>{t("dag.env.mapping.secretsHint")}</Hint> : null}
      </Stack>
    </Panel>
  );
}
