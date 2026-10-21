import { Alert, Badge, HStack, SimpleGrid, Stack, Table, Text, Wrap } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { adminOverviewOptions } from "../../api/gen/@tanstack/react-query.gen";
import { useMeta } from "../../lifecycle/hooks";
import { DataTable, QueryState, Section, StatCard } from "../common";
import { ago, formatNumber, formatTime } from "../format";

export function OverviewTab() {
  const { t, i18n } = useTranslation("admin");
  const q = useQuery({ ...adminOverviewOptions(), refetchInterval: 30_000 });
  const o = q.data;
  const aiReason = useMeta()?.ai_unavailable_reason;
  return (
    <QueryState isPending={q.isPending} error={q.error}>
      {o ? (
        <Stack gap={6}>
          <SimpleGrid columns={{ base: 1, sm: 2, lg: 4 }} gap={3}>
            <StatCard label={t("overview.version")} value={o.autoair_version} testId="admin-version" />
            <StatCard label={t("overview.airflow")} value={o.airflow_version ?? "—"} />
            <StatCard label={t("overview.mode")} value={o.mode} hint={`${t("overview.auth")}: ${o.auth}`} />
            <StatCard label={t("overview.started")} value={formatTime(o.started_at, i18n.language)} />
          </SimpleGrid>
          <Section title={t("overview.backends")}>
            <SimpleGrid columns={{ base: 1, sm: 2, lg: 4 }} gap={3}>
              <StatCard
                label={t("overview.database")}
                value={o.backends.database}
                hint={o.backends.database_url ?? undefined}
                testId="admin-backend"
              />
              <StatCard
                label={t("overview.schema")}
                value={o.backends.schema_version ?? "—"}
                hint={o.backends.schema_head ? `head ${o.backends.schema_head}` : undefined}
              />
              <StatCard label={t("overview.collab")} value={o.backends.collab_sync} />
              <StatCard
                label={t("overview.deployTarget")}
                value={o.backends.deploy_target}
                hint={t("overview.source", { source: o.backends.deploy_target_source })}
              />
            </SimpleGrid>
          </Section>
          <Section title={t("overview.catalog")}>
            <SimpleGrid columns={{ base: 2, lg: 4 }} gap={3}>
              <StatCard label={t("overview.operators")} value={formatNumber(o.catalog?.operators, i18n.language)} />
              <StatCard label={t("overview.providers")} value={formatNumber(o.catalog?.providers, i18n.language)} />
              <StatCard label={t("overview.notifiers")} value={formatNumber(o.catalog?.notifiers, i18n.language)} />
              <StatCard
                label={t("overview.templates")}
                value={formatNumber(o.templates, i18n.language)}
                hint={t("overview.userTemplates", { count: o.user_templates ?? 0 })}
              />
            </SimpleGrid>
          </Section>
          <Section title={t("overview.features")}>
            <Wrap gap={2} data-testid="admin-features">
              {Object.entries(o.features)
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([name, on]) => (
                  <Badge key={name} colorPalette={on ? "green" : "gray"} variant="subtle" size="md">
                    {name}: {on ? t("overview.on") : t("overview.off")}
                  </Badge>
                ))}
            </Wrap>
            {aiReason ? (
              <Alert.Root status="warning" size="sm" mt={3} data-testid="admin-ai-unavailable">
                <Alert.Indicator />
                <Alert.Description>{t("overview.aiUnavailable", { reason: aiReason })}</Alert.Description>
              </Alert.Root>
            ) : null}
          </Section>
          <Section title={t("overview.replicas")} help={t("overview.replicasHelp")}>
            <DataTable label={t("overview.replicas")} testId="admin-replicas">
              <Table.Header>
                <Table.Row>
                  <Table.ColumnHeader>{t("overview.host")}</Table.ColumnHeader>
                  <Table.ColumnHeader>{t("overview.pid")}</Table.ColumnHeader>
                  <Table.ColumnHeader>{t("overview.version")}</Table.ColumnHeader>
                  <Table.ColumnHeader>{t("overview.started")}</Table.ColumnHeader>
                  <Table.ColumnHeader>{t("overview.lastSeen")}</Table.ColumnHeader>
                </Table.Row>
              </Table.Header>
              <Table.Body>
                {o.replicas.map((r) => (
                  <Table.Row key={r.id}>
                    <Table.Cell>
                      <HStack gap={2}>
                        <Text fontFamily="mono" fontSize="xs">
                          {r.hostname}
                        </Text>
                        {r.current ? (
                          <Badge size="xs" colorPalette="blue">
                            {t("overview.current")}
                          </Badge>
                        ) : null}
                      </HStack>
                    </Table.Cell>
                    <Table.Cell fontFamily="mono" fontSize="xs">
                      {r.pid}
                    </Table.Cell>
                    <Table.Cell fontSize="xs">{r.version}</Table.Cell>
                    <Table.Cell fontSize="xs">{formatTime(r.started_at, i18n.language)}</Table.Cell>
                    <Table.Cell>
                      <HStack gap={2}>
                        <Badge size="xs" colorPalette={r.alive ? "green" : "orange"}>
                          {r.alive ? t("overview.alive") : t("overview.stale")}
                        </Badge>
                        <Text fontSize="xs" color="fg.muted">
                          {ago(r.last_seen)}
                        </Text>
                      </HStack>
                    </Table.Cell>
                  </Table.Row>
                ))}
              </Table.Body>
            </DataTable>
          </Section>
        </Stack>
      ) : null}
    </QueryState>
  );
}
