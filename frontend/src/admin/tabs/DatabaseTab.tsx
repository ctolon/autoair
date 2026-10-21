import { Alert, Badge, Button, Code, SimpleGrid, Stack, Table, Text } from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { LuDatabaseZap } from "react-icons/lu";

import { adminDatabaseOptions, adminDatabaseUpgradeMutation } from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import { toaster } from "../../ui/toaster";
import { DataTable, QueryState, Section, StatCard } from "../common";
import { formatNumber, formatTime } from "../format";

export function DatabaseTab({ canWrite }: { canWrite: boolean }) {
  const { t, i18n } = useTranslation("admin");
  const client = useQueryClient();
  const q = useQuery(adminDatabaseOptions());
  const upgrade = useMutation({
    ...adminDatabaseUpgradeMutation(),
    onSuccess: (data) => {
      client.setQueryData(adminDatabaseOptions().queryKey, data);
      toaster.create({ type: "success", title: t("database.upgraded") });
    },
    onError: (err) => toaster.create({ type: "error", title: t("error", { message: errorMessage(err) }) }),
  });
  const d = q.data;
  return (
    <QueryState isPending={q.isPending} error={q.error}>
      {d ? (
        <Stack gap={6}>
          <SimpleGrid columns={{ base: 1, sm: 2, lg: 4 }} gap={3}>
            <StatCard label={t("database.backend")} value={d.backend} testId="admin-db-backend" />
            {d.backend !== "file" ? (
              <>
                <StatCard
                  label={t("database.reachable")}
                  value={
                    <Badge colorPalette={d.reachable ? "green" : "red"}>
                      {d.reachable ? t("database.yes") : t("database.no")}
                    </Badge>
                  }
                />
                <StatCard
                  label={t("database.version")}
                  value={d.current ?? "—"}
                  hint={t("database.head", { head: d.head })}
                  testId="admin-db-version"
                />
                <StatCard
                  label={t("database.pool")}
                  value={
                    <Text as="span" fontSize="xs" fontWeight="normal">
                      {d.pool ?? "—"}
                    </Text>
                  }
                />
              </>
            ) : null}
          </SimpleGrid>
          {d.backend === "file" ? (
            <Alert.Root status="info" size="sm">
              <Alert.Indicator />
              <Alert.Description>{t("database.fileHelp", { dir: d.state_dir })}</Alert.Description>
            </Alert.Root>
          ) : (
            <>
              <Text fontSize="sm">
                {t("database.url")}: <Code fontSize="xs">{d.url}</Code>
              </Text>
              <Section
                title={t("database.pending")}
                actions={
                  canWrite && d.pending?.length ? (
                    <Button
                      size="sm"
                      colorPalette="blue"
                      loading={upgrade.isPending}
                      onClick={() => upgrade.mutate({})}
                    >
                      <LuDatabaseZap /> {t("database.upgrade")}
                    </Button>
                  ) : null
                }
              >
                {d.pending?.length ? (
                  <Stack gap={1}>
                    {d.pending.map((m) => (
                      <Text key={m.version} fontSize="sm">
                        <Badge me={2}>{m.version}</Badge>
                        {m.description}
                      </Text>
                    ))}
                  </Stack>
                ) : (
                  <Badge colorPalette="green" alignSelf="start">
                    {t("database.upToDate")}
                  </Badge>
                )}
              </Section>
              <Section title={t("database.applied")}>
                <DataTable label={t("database.applied")} testId="admin-db-migrations">
                  <Table.Header>
                    <Table.Row>
                      <Table.ColumnHeader>#</Table.ColumnHeader>
                      <Table.ColumnHeader>{t("database.description")}</Table.ColumnHeader>
                      <Table.ColumnHeader>{t("database.appliedAt")}</Table.ColumnHeader>
                    </Table.Row>
                  </Table.Header>
                  <Table.Body>
                    {(d.applied ?? []).map((m) => (
                      <Table.Row key={m.version}>
                        <Table.Cell>{m.version}</Table.Cell>
                        <Table.Cell fontSize="xs">{m.description}</Table.Cell>
                        <Table.Cell fontSize="xs">{formatTime(m.applied_at, i18n.language)}</Table.Cell>
                      </Table.Row>
                    ))}
                  </Table.Body>
                </DataTable>
              </Section>
              <Section title={t("database.tables")}>
                <DataTable label={t("database.tables")} testId="admin-db-tables">
                  <Table.Header>
                    <Table.Row>
                      <Table.ColumnHeader>{t("database.table")}</Table.ColumnHeader>
                      <Table.ColumnHeader textAlign="end">{t("database.rows")}</Table.ColumnHeader>
                    </Table.Row>
                  </Table.Header>
                  <Table.Body>
                    {Object.entries(d.tables ?? {}).map(([name, rows]) => (
                      <Table.Row key={name}>
                        <Table.Cell fontFamily="mono" fontSize="xs">
                          {name}
                        </Table.Cell>
                        <Table.Cell textAlign="end">{formatNumber(rows, i18n.language)}</Table.Cell>
                      </Table.Row>
                    ))}
                  </Table.Body>
                </DataTable>
              </Section>
            </>
          )}
        </Stack>
      ) : null}
    </QueryState>
  );
}
