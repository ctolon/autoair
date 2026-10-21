import { Badge, Input, Table, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { getConfigOptions } from "../../api/gen/@tanstack/react-query.gen";
import { DataTable, QueryState, Section } from "../common";

function show(value: unknown): string {
  if (value === undefined || value === null) return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** The effective configuration (GET /api/config), searchable. */
export function ConfigTab({ exposed }: { exposed: boolean }) {
  const { t } = useTranslation("admin");
  const q = useQuery({ ...getConfigOptions(), enabled: exposed, retry: false });
  const [query, setQuery] = useState("");
  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (q.data?.options ?? []).filter(
      (o) => !needle || [o.key, o.env, o.description].some((s) => s.toLowerCase().includes(needle)),
    );
  }, [q.data, query]);
  if (!exposed) {
    return (
      <Text fontSize="sm" color="fg.muted">
        {t("config.disabled")}
      </Text>
    );
  }
  return (
    <Section
      help={t("config.help")}
      actions={
        <Input
          size="sm"
          maxW="320px"
          placeholder={t("config.search")}
          aria-label={t("config.search")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      }
    >
      <QueryState isPending={q.isPending} error={q.error}>
        <DataTable label={t("tabs.config")} testId="admin-config-table">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>{t("config.key")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("config.value")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("config.source")}</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {rows.map((o) => (
              <Table.Row key={o.key} verticalAlign="top">
                <Table.Cell maxW="360px">
                  <Text fontFamily="mono" fontSize="xs" fontWeight="medium">
                    {o.key}
                  </Text>
                  <Text fontSize="xs" color="fg.muted">
                    {o.description}
                  </Text>
                </Table.Cell>
                <Table.Cell fontFamily="mono" fontSize="xs" maxW="320px" wordBreak="break-all">
                  {show(o.value)}
                </Table.Cell>
                <Table.Cell>
                  <Badge size="xs" colorPalette={o.source === "default" ? "gray" : "blue"}>
                    {o.source}
                  </Badge>
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </DataTable>
      </QueryState>
    </Section>
  );
}
