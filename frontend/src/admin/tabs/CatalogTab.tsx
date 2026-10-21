import { Badge, Box, Button, Collapsible, HStack, NativeSelect, SimpleGrid, Table, Text } from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuChevronDown, LuRefreshCw } from "react-icons/lu";

import {
  getCatalogDiagnosticsOptions,
  getCatalogDiagnosticsQueryKey,
  refreshCatalogMutation,
} from "../../api/gen/@tanstack/react-query.gen";
import type { CatalogDiagnostics } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { toaster } from "../../ui/toaster";
import { DataTable, Empty, QueryState, Section, StatCard } from "../common";
import { ago, formatNumber } from "../format";

/** Epoch seconds → local date-time ("—" for missing/invalid values). */
function formatEpoch(value: number | null | undefined, lng = "en"): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return "—";
  return new Intl.DateTimeFormat(lng, { dateStyle: "medium", timeStyle: "medium" }).format(new Date(value * 1000));
}

/** Distinct filter reasons, sorted (the reason filter's options). */
function filterReasons(diag: Pick<CatalogDiagnostics, "filtered"> | undefined): string[] {
  return [...new Set((diag?.filtered ?? []).map((f) => f.reason))].sort();
}

function SourcesTable({ diag }: { diag: CatalogDiagnostics }) {
  const { t, i18n } = useTranslation("admin");
  const sources = diag.sources ?? [];
  if (!sources.length) return <Empty />;
  return (
    <DataTable label={t("catalog.sources")} testId="admin-catalog-sources">
      <Table.Header>
        <Table.Row>
          <Table.ColumnHeader>{t("catalog.source")}</Table.ColumnHeader>
          <Table.ColumnHeader>{t("catalog.scans")}</Table.ColumnHeader>
          <Table.ColumnHeader textAlign="end">{t("catalog.modules")}</Table.ColumnHeader>
          <Table.ColumnHeader textAlign="end">{t("catalog.operators")}</Table.ColumnHeader>
          <Table.ColumnHeader>{t("catalog.providers")}</Table.ColumnHeader>
        </Table.Row>
      </Table.Header>
      <Table.Body>
        {sources.map((s, i) => (
          <Table.Row key={`${s.source}-${i}`} verticalAlign="top">
            <Table.Cell fontWeight="medium">
              {s.source}
              {s.error ? (
                <Text fontSize="xs" color="fg.error" wordBreak="break-word">
                  {s.error}
                </Text>
              ) : null}
            </Table.Cell>
            <Table.Cell fontSize="xs" fontFamily="mono" wordBreak="break-all" maxW="xs">
              {(s.detail ?? []).join(", ")}
            </Table.Cell>
            <Table.Cell textAlign="end">{formatNumber(s.modules, i18n.language)}</Table.Cell>
            <Table.Cell textAlign="end">
              {(s.operators ?? []).length ? (
                <Collapsible.Root>
                  <Collapsible.Trigger asChild>
                    <Button size="2xs" variant="ghost">
                      {formatNumber((s.operators ?? []).length, i18n.language)} <LuChevronDown />
                    </Button>
                  </Collapsible.Trigger>
                  <Collapsible.Content>
                    <Box textAlign="start" fontFamily="mono" fontSize="xs" maxH="48" overflowY="auto">
                      {(s.operators ?? []).map((fqn) => (
                        <Text key={fqn} wordBreak="break-all">
                          {fqn}
                        </Text>
                      ))}
                    </Box>
                  </Collapsible.Content>
                </Collapsible.Root>
              ) : (
                "0"
              )}
            </Table.Cell>
            <Table.Cell fontSize="xs" wordBreak="break-word" maxW="xs">
              {(s.providers ?? []).join(", ")}
            </Table.Cell>
          </Table.Row>
        ))}
      </Table.Body>
    </DataTable>
  );
}

function FilteredTable({ diag }: { diag: CatalogDiagnostics }) {
  const { t } = useTranslation("admin");
  const [reason, setReason] = useState("");
  const reasons = useMemo(() => filterReasons(diag), [diag]);
  const rows = (diag.filtered ?? []).filter((f) => !reason || f.reason === reason);
  return (
    <Section
      title={t("catalog.filtered", { count: (diag.filtered ?? []).length })}
      help={t("catalog.filteredHelp")}
      actions={
        reasons.length > 1 ? (
          <NativeSelect.Root size="xs" w="auto">
            <NativeSelect.Field
              aria-label={t("catalog.reason")}
              value={reason}
              onChange={(e) => setReason(e.currentTarget.value)}
              data-testid="admin-catalog-reason"
            >
              <option value="">{t("catalog.allReasons")}</option>
              {reasons.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </NativeSelect.Field>
            <NativeSelect.Indicator />
          </NativeSelect.Root>
        ) : null
      }
    >
      {rows.length ? (
        <DataTable label={t("catalog.filtered", { count: rows.length })} testId="admin-catalog-filtered">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>{t("catalog.name")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("catalog.kind")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("catalog.reason")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("catalog.pattern")}</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {rows.slice(0, 500).map((f, i) => (
              <Table.Row key={`${f.name}-${f.reason}-${i}`}>
                <Table.Cell fontFamily="mono" fontSize="xs" wordBreak="break-all">
                  {f.name}
                </Table.Cell>
                <Table.Cell fontSize="xs">{t(`catalog.kinds.${f.kind}`, { defaultValue: f.kind })}</Table.Cell>
                <Table.Cell>
                  <Badge size="xs" variant="outline">
                    {f.reason}
                  </Badge>
                </Table.Cell>
                <Table.Cell fontFamily="mono" fontSize="xs" wordBreak="break-all">
                  {f.detail ?? ""}
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </DataTable>
      ) : (
        <Empty />
      )}
    </Section>
  );
}

function ErrorsTable({ diag }: { diag: CatalogDiagnostics }) {
  const { t } = useTranslation("admin");
  const rows = [
    ...(diag.parse_errors ?? []).map((e) => ({ ...e, phase: t("catalog.parse") })),
    ...(diag.errors ?? []).map((e) => ({ ...e, phase: t("catalog.importPhase") })),
  ];
  return (
    <Section title={t("catalog.errors", { count: rows.length })} help={t("catalog.errorsHelp")}>
      {rows.length ? (
        <DataTable label={t("catalog.errors", { count: rows.length })} testId="admin-catalog-errors">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>{t("catalog.module")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("catalog.provider")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("catalog.phase")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("catalog.error")}</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {rows.slice(0, 500).map((e, i) => (
              <Table.Row key={`${e.module}-${i}`} verticalAlign="top">
                <Table.Cell fontFamily="mono" fontSize="xs" wordBreak="break-all">
                  {e.module}
                </Table.Cell>
                <Table.Cell fontSize="xs">{e.provider}</Table.Cell>
                <Table.Cell fontSize="xs">{e.phase}</Table.Cell>
                <Table.Cell fontSize="xs" color="fg.error" wordBreak="break-word">
                  {e.error}
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </DataTable>
      ) : (
        <Empty />
      )}
    </Section>
  );
}

function MetadataTable({ diag }: { diag: CatalogDiagnostics }) {
  const { t, i18n } = useTranslation("admin");
  const rows = diag.metadata ?? [];
  return (
    <Section title={t("catalog.metadata")} help={t("catalog.metadataHelp")}>
      {rows.length ? (
        <DataTable label={t("catalog.metadata")} testId="admin-catalog-metadata">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>{t("catalog.kind")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("catalog.origin")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("catalog.package")}</Table.ColumnHeader>
              <Table.ColumnHeader textAlign="end">{t("catalog.operators")}</Table.ColumnHeader>
              <Table.ColumnHeader textAlign="end">{t("catalog.icons")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("catalog.error")}</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {rows.map((m, i) => (
              <Table.Row key={`${m.origin}-${i}`} verticalAlign="top">
                <Table.Cell fontSize="xs">{t(`catalog.metadataKinds.${m.kind}`, { defaultValue: m.kind })}</Table.Cell>
                <Table.Cell fontFamily="mono" fontSize="xs" wordBreak="break-all">
                  {m.origin}
                </Table.Cell>
                <Table.Cell fontSize="xs">{[m.package, m.distribution].filter(Boolean).join(" · ")}</Table.Cell>
                <Table.Cell textAlign="end">{formatNumber(m.operators, i18n.language)}</Table.Cell>
                <Table.Cell textAlign="end">{formatNumber(m.icons, i18n.language)}</Table.Cell>
                <Table.Cell fontSize="xs" color="fg.error" wordBreak="break-word">
                  {m.error ?? ""}
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </DataTable>
      ) : (
        <Empty />
      )}
    </Section>
  );
}

/** Catalog diagnostics (catalog:refresh): sources, filters, failures, package metadata. */
export function CatalogTab({ canRefresh }: { canRefresh: boolean }) {
  const { t, i18n } = useTranslation("admin");
  const client = useQueryClient();
  const q = useQuery({ ...getCatalogDiagnosticsOptions(), enabled: canRefresh, retry: false });
  const refresh = useMutation({
    ...refreshCatalogMutation(),
    onSuccess: async (stats) => {
      toaster.create({
        type: "success",
        title: t("catalog.refreshed", { count: stats.operators }),
      });
      await client.invalidateQueries({ queryKey: getCatalogDiagnosticsQueryKey() });
      // The palette and inspector read the catalog too.
      void client.invalidateQueries();
    },
    onError: (err) => toaster.create({ type: "error", title: t("error", { message: errorMessage(err) }) }),
  });
  if (!canRefresh) return <Text color="fg.muted">{t("catalog.noPermission")}</Text>;
  const d = q.data;
  return (
    <Section
      help={t("catalog.help")}
      actions={
        <Button
          size="xs"
          variant="outline"
          loading={refresh.isPending}
          onClick={() => refresh.mutate({})}
          data-testid="admin-catalog-refresh"
        >
          <LuRefreshCw /> {t("catalog.refresh")}
        </Button>
      }
    >
      <QueryState isPending={q.isPending} error={q.error}>
        {d ? (
          <>
            <SimpleGrid columns={{ base: 2, md: 3, xl: 6 }} gap={3} data-testid="admin-catalog-summary">
              <StatCard label={t("catalog.operators")} value={formatNumber(d.operators, i18n.language)} />
              <StatCard label={t("catalog.notifiers")} value={formatNumber(d.notifiers, i18n.language)} />
              <StatCard label={t("catalog.providers")} value={formatNumber(d.providers, i18n.language)} />
              <StatCard
                label={t("catalog.builtAt")}
                value={formatEpoch(d.built_at, i18n.language)}
                hint={
                  typeof d.built_at === "number" && d.built_at > 0
                    ? t("catalog.ago", { value: ago(d.built_at) })
                    : undefined
                }
                testId="admin-catalog-built"
              />
              <StatCard
                label={t("catalog.duration")}
                value={`${typeof d.build_seconds === "number" ? d.build_seconds.toFixed(2) : "—"} s`}
                hint={d.loaded_from_cache ? t("catalog.fromCache") : t("catalog.fresh")}
              />
              <StatCard
                label={t("catalog.strategy")}
                value={d.strategy}
                hint={t("catalog.loadedAt", { value: formatEpoch(d.loaded_at, i18n.language) })}
              />
            </SimpleGrid>
            <Section title={t("catalog.sources")} help={t("catalog.sourcesHelp")}>
              <SourcesTable diag={d} />
            </Section>
            <MetadataTable diag={d} />
            <FilteredTable diag={d} />
            <ErrorsTable diag={d} />
            {d.cache_key ? (
              <HStack fontSize="xs" color="fg.muted" gap={1}>
                <Text>{t("catalog.cacheKey")}</Text>
                <Text fontFamily="mono">{d.cache_key.slice(0, 24)}</Text>
              </HStack>
            ) : null}
          </>
        ) : (
          <Empty />
        )}
      </QueryState>
    </Section>
  );
}
