import { Badge, Spinner, Table, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { getSpecAuditOptions } from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import { useBuilder } from "../../store/builder";

const str = (v: unknown) => (v === null || v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v));

/** Audit trail of deploys / deletes / promotions of the open DAG. */
export default function AuditTab() {
  const { t, i18n } = useTranslation("lifecycle");
  const dagId = useBuilder((s) => s.savedDagId);
  const audit = useQuery({
    ...getSpecAuditOptions({ path: { dag_id: dagId ?? "_" }, query: { limit: 100 } }),
    enabled: !!dagId,
    retry: false,
  });
  if (!dagId) {
    return (
      <Text fontSize="sm" color="fg.muted" p={3}>
        {t("common.deployFirst")}
      </Text>
    );
  }
  if (audit.isPending) return <Spinner size="sm" m={3} />;
  if (audit.isError) {
    return (
      <Text fontSize="sm" color="red.500" p={3}>
        {errorMessage(audit.error)}
      </Text>
    );
  }
  if (!audit.data.length) {
    return (
      <Text fontSize="sm" color="fg.muted" p={3}>
        {t("audit.empty")}
      </Text>
    );
  }
  const rows = [...audit.data].reverse();
  return (
    <Table.Root size="sm">
      <Table.Header>
        <Table.Row>
          <Table.ColumnHeader>{t("audit.time")}</Table.ColumnHeader>
          <Table.ColumnHeader>{t("audit.action")}</Table.ColumnHeader>
          <Table.ColumnHeader>{t("audit.user")}</Table.ColumnHeader>
          <Table.ColumnHeader>{t("audit.details")}</Table.ColumnHeader>
        </Table.Row>
      </Table.Header>
      <Table.Body>
        {rows.map((r, i) => {
          const details = (r.details && typeof r.details === "object" ? r.details : {}) as Record<string, unknown>;
          return (
            <Table.Row key={i}>
              <Table.Cell fontSize="xs" whiteSpace="nowrap">
                {r.timestamp ? new Date(str(r.timestamp)).toLocaleString(i18n.language) : ""}
              </Table.Cell>
              <Table.Cell>
                <Badge size="xs">{str(r.action)}</Badge>
              </Table.Cell>
              <Table.Cell fontSize="xs">{str(r.user) || "—"}</Table.Cell>
              <Table.Cell fontSize="xs" fontFamily="mono" wordBreak="break-all">
                {Object.entries(details)
                  .filter(([, v]) => v !== null && v !== undefined && v !== "")
                  .map(([k, v]) => `${k}=${str(v)}`)
                  .join("  ")}
              </Table.Cell>
            </Table.Row>
          );
        })}
      </Table.Body>
    </Table.Root>
  );
}
