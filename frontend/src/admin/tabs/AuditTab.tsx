import { Button, Code, Field, HStack, Input, SimpleGrid, Stack, Table, Text } from "@chakra-ui/react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuDownload, LuFilter, LuRotateCcw } from "react-icons/lu";

import { adminAuditOptions } from "../../api/gen/@tanstack/react-query.gen";
import { adminAuditCsv } from "../../api/gen/sdk.gen";
import { errorMessage } from "../../api/setup";
import { toaster } from "../../ui/toaster";
import { DataTable, Empty, QueryState } from "../common";
import { dayBound, formatTime } from "../format";

const PAGE = 50;

interface Filters {
  user: string;
  action: string;
  dag_id: string;
  since: string;
  until: string;
}

const EMPTY: Filters = { user: "", action: "", dag_id: "", since: "", until: "" };

function toQuery(f: Filters) {
  return {
    user: f.user.trim() || undefined,
    action: f.action.trim() || undefined,
    dag_id: f.dag_id.trim() || undefined,
    since: dayBound(f.since),
    until: dayBound(f.until, true),
  };
}

export function AuditTab() {
  const { t, i18n } = useTranslation("admin");
  const [draft, setDraft] = useState<Filters>(EMPTY);
  const [applied, setApplied] = useState<Filters>(EMPTY);
  const [offset, setOffset] = useState(0);
  const [exporting, setExporting] = useState(false);
  const q = useQuery({
    ...adminAuditOptions({ query: { ...toQuery(applied), limit: PAGE, offset } }),
    placeholderData: keepPreviousData,
  });

  const field = (key: keyof Filters, label: string, type = "text") => (
    <Field.Root>
      <Field.Label fontSize="xs">{label}</Field.Label>
      <Input
        size="sm"
        type={type}
        value={draft[key]}
        onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            setApplied(draft);
            setOffset(0);
          }
        }}
      />
    </Field.Root>
  );

  const exportCsv = async () => {
    setExporting(true);
    try {
      const { data } = await adminAuditCsv({
        query: { ...toQuery(applied), limit: 50_000 },
        parseAs: "blob",
        throwOnError: true,
      });
      const url = URL.createObjectURL(data as Blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `autoair-audit-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(url);
      toaster.create({ type: "success", title: t("audit.exported") });
    } catch (err) {
      toaster.create({ type: "error", title: t("error", { message: errorMessage(err) }) });
    } finally {
      setExporting(false);
    }
  };

  const items = q.data?.items ?? [];
  return (
    <Stack gap={4}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setApplied(draft);
          setOffset(0);
        }}
      >
        <SimpleGrid columns={{ base: 1, sm: 2, lg: 5 }} gap={3} alignItems="end">
          {field("user", t("audit.user"))}
          {field("action", t("audit.action"))}
          {field("dag_id", t("audit.dag"))}
          {field("since", t("audit.since"), "date")}
          {field("until", t("audit.until"), "date")}
        </SimpleGrid>
        <HStack mt={3} gap={2} wrap="wrap">
          <Button size="sm" type="submit" colorPalette="blue">
            <LuFilter /> {t("audit.apply")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setDraft(EMPTY);
              setApplied(EMPTY);
              setOffset(0);
            }}
          >
            <LuRotateCcw /> {t("audit.reset")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => void exportCsv()}
            loading={exporting}
            data-testid="admin-audit-export"
          >
            <LuDownload /> {t("audit.export")}
          </Button>
        </HStack>
      </form>
      <QueryState isPending={q.isPending} error={q.error}>
        {items.length ? (
          <DataTable label={t("audit.table")} testId="admin-audit-table">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeader>{t("audit.time")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("audit.action")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("audit.user")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("audit.dag")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("audit.details")}</Table.ColumnHeader>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {items.map((r, i) => (
                <Table.Row key={`${r.timestamp}-${i}`} verticalAlign="top">
                  <Table.Cell fontSize="xs" whiteSpace="nowrap">
                    {formatTime(r.timestamp, i18n.language)}
                  </Table.Cell>
                  <Table.Cell>
                    <Code fontSize="xs">{r.action}</Code>
                  </Table.Cell>
                  <Table.Cell fontSize="xs">{r.user ?? "—"}</Table.Cell>
                  <Table.Cell fontSize="xs" fontFamily="mono">
                    {r.dag_id ?? "—"}
                  </Table.Cell>
                  <Table.Cell fontSize="xs" fontFamily="mono" maxW="420px" color="fg.muted">
                    {r.details && Object.keys(r.details).length ? (
                      <Text lineClamp={2} wordBreak="break-all" title={JSON.stringify(r.details, null, 1)}>
                        {JSON.stringify(r.details)}
                      </Text>
                    ) : (
                      "—"
                    )}
                  </Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </DataTable>
        ) : (
          <Empty />
        )}
        <HStack justify="space-between">
          <Text fontSize="xs" color="fg.muted">
            {items.length ? t("audit.page", { from: offset + 1, to: offset + items.length }) : null}
          </Text>
          <HStack gap={2}>
            <Button
              size="xs"
              variant="outline"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE))}
            >
              {t("audit.previous")}
            </Button>
            <Button size="xs" variant="outline" disabled={!q.data?.has_more} onClick={() => setOffset(offset + PAGE)}>
              {t("audit.next")}
            </Button>
          </HStack>
        </HStack>
      </QueryState>
    </Stack>
  );
}
