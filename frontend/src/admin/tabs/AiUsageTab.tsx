import { HStack, NativeSelect, SimpleGrid, Stack, Table, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { adminAiUsageOptions } from "../../api/gen/@tanstack/react-query.gen";
import { DataTable, Empty, QueryState, StatCard } from "../common";
import { formatCost, formatNumber, usageTotals } from "../format";

const PERIODS = [1, 7, 30, 90, 365];

export function AiUsageTab() {
  const { t, i18n } = useTranslation("admin");
  const [days, setDays] = useState(30);
  const q = useQuery(adminAiUsageOptions({ query: { days } }));
  const rows = q.data ?? [];
  const totals = usageTotals(rows);
  const lng = i18n.language;
  return (
    <Stack gap={4}>
      <HStack>
        <NativeSelect.Root size="sm" maxW="220px">
          <NativeSelect.Field aria-label={t("ai.days")} value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {PERIODS.map((d) => (
              <option key={d} value={d}>
                {t("ai.lastDays", { count: d })}
              </option>
            ))}
          </NativeSelect.Field>
          <NativeSelect.Indicator />
        </NativeSelect.Root>
      </HStack>
      <QueryState isPending={q.isPending} error={q.error}>
        <SimpleGrid columns={{ base: 2, lg: 4 }} gap={3}>
          <StatCard label={t("ai.requests")} value={formatNumber(totals.requests, lng)} testId="admin-ai-requests" />
          <StatCard label={t("ai.input")} value={formatNumber(totals.input, lng)} />
          <StatCard label={t("ai.output")} value={formatNumber(totals.output, lng)} />
          <StatCard label={t("ai.cost")} value={formatCost(totals.cost, lng)} hint={t("ai.unpriced")} />
        </SimpleGrid>
        {rows.length ? (
          <DataTable label={t("ai.table")} testId="admin-ai-table">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeader>{t("ai.user")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("ai.model")}</Table.ColumnHeader>
                <Table.ColumnHeader textAlign="end">{t("ai.requests")}</Table.ColumnHeader>
                <Table.ColumnHeader textAlign="end">{t("ai.input")}</Table.ColumnHeader>
                <Table.ColumnHeader textAlign="end">{t("ai.output")}</Table.ColumnHeader>
                <Table.ColumnHeader textAlign="end">{t("ai.cost")}</Table.ColumnHeader>
                <Table.ColumnHeader textAlign="end">{t("ai.errors")}</Table.ColumnHeader>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {rows.map((r) => (
                <Table.Row key={`${r.user}|${r.provider}|${r.model}`}>
                  <Table.Cell>{r.user ?? t("ai.anonymous")}</Table.Cell>
                  <Table.Cell>
                    <Text fontFamily="mono" fontSize="xs">
                      {[r.provider, r.model].filter(Boolean).join(" / ") || "—"}
                    </Text>
                  </Table.Cell>
                  <Table.Cell textAlign="end">{formatNumber(r.requests, lng)}</Table.Cell>
                  <Table.Cell textAlign="end">{formatNumber(r.input_tokens, lng)}</Table.Cell>
                  <Table.Cell textAlign="end">{formatNumber(r.output_tokens, lng)}</Table.Cell>
                  <Table.Cell textAlign="end">{formatCost(r.cost_usd, lng)}</Table.Cell>
                  <Table.Cell textAlign="end" color={r.errors ? "fg.error" : undefined}>
                    {r.errors ?? 0}
                  </Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </DataTable>
        ) : (
          <Empty />
        )}
      </QueryState>
    </Stack>
  );
}
