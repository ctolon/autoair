import { Badge, Code, HStack, Stack, Table, Text, Wrap } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { adminProposalsOptions } from "../../api/gen/@tanstack/react-query.gen";
import { DataTable, QueryState, Section } from "../common";
import { formatTime } from "../format";

const VERDICT_COLOR: Record<string, string> = { approve: "green", request_changes: "orange", comment: "gray" };

export function ProposalsTab() {
  const { t, i18n } = useTranslation("admin");
  const q = useQuery({ ...adminProposalsOptions(), refetchInterval: 30_000 });
  const data = q.data;
  return (
    <QueryState isPending={q.isPending} error={q.error}>
      {data ? (
        <Stack gap={6}>
          <HStack gap={2}>
            <Text fontSize="sm">{t("proposals.requireApproval")}:</Text>
            <Badge colorPalette={data.require_approval ? "green" : "gray"}>
              {data.require_approval ? t("proposals.required") : t("proposals.notRequired")}
            </Badge>
          </HStack>
          <Section title={t("proposals.pending")}>
            {data.proposals.length ? (
              <DataTable label={t("proposals.pending")} testId="admin-proposals-table">
                <Table.Header>
                  <Table.Row>
                    <Table.ColumnHeader>{t("proposals.dag")}</Table.ColumnHeader>
                    <Table.ColumnHeader>{t("proposals.version")}</Table.ColumnHeader>
                    <Table.ColumnHeader>{t("proposals.author")}</Table.ColumnHeader>
                    <Table.ColumnHeader>{t("proposals.submitted")}</Table.ColumnHeader>
                    <Table.ColumnHeader>{t("proposals.message")}</Table.ColumnHeader>
                    <Table.ColumnHeader>{t("proposals.approvals")}</Table.ColumnHeader>
                  </Table.Row>
                </Table.Header>
                <Table.Body>
                  {data.proposals.map((p) => (
                    <Table.Row key={`${p.dag_id}/${p.etag}`}>
                      <Table.Cell fontFamily="mono" fontSize="xs">
                        {p.dag_id}
                      </Table.Cell>
                      <Table.Cell>
                        <Code fontSize="xs">{p.etag.slice(0, 12)}</Code>
                      </Table.Cell>
                      <Table.Cell fontSize="xs">{p.author ?? "—"}</Table.Cell>
                      <Table.Cell fontSize="xs">{formatTime(p.timestamp, i18n.language)}</Table.Cell>
                      <Table.Cell fontSize="xs" maxW="320px">
                        {p.message ?? "—"}
                      </Table.Cell>
                      <Table.Cell>
                        <Wrap gap={1}>
                          {(p.approvals ?? []).map((u) => (
                            <Badge key={u} size="xs" colorPalette="green">
                              {u}
                            </Badge>
                          ))}
                        </Wrap>
                      </Table.Cell>
                    </Table.Row>
                  ))}
                </Table.Body>
              </DataTable>
            ) : (
              <Text fontSize="sm" color="fg.muted">
                {t("proposals.none")}
              </Text>
            )}
          </Section>
          <Section title={t("proposals.reviews")}>
            {data.reviews.length ? (
              <DataTable label={t("proposals.reviews")} testId="admin-reviews-table">
                <Table.Header>
                  <Table.Row>
                    <Table.ColumnHeader>{t("proposals.dag")}</Table.ColumnHeader>
                    <Table.ColumnHeader>{t("proposals.version")}</Table.ColumnHeader>
                    <Table.ColumnHeader>{t("proposals.reviewer")}</Table.ColumnHeader>
                    <Table.ColumnHeader>{t("proposals.verdict")}</Table.ColumnHeader>
                    <Table.ColumnHeader>{t("proposals.comment")}</Table.ColumnHeader>
                    <Table.ColumnHeader>{t("proposals.submitted")}</Table.ColumnHeader>
                  </Table.Row>
                </Table.Header>
                <Table.Body>
                  {data.reviews.map((r, i) => (
                    <Table.Row key={`${r.dag_id}/${r.etag}/${r.user}/${i}`}>
                      <Table.Cell fontFamily="mono" fontSize="xs">
                        {r.dag_id}
                      </Table.Cell>
                      <Table.Cell>
                        <Code fontSize="xs">{r.etag.slice(0, 12)}</Code>
                      </Table.Cell>
                      <Table.Cell fontSize="xs">{r.user ?? "—"}</Table.Cell>
                      <Table.Cell>
                        <Badge size="xs" colorPalette={VERDICT_COLOR[r.verdict] ?? "gray"}>
                          {t(`proposals.verdicts.${r.verdict}`, { defaultValue: r.verdict })}
                        </Badge>
                      </Table.Cell>
                      <Table.Cell fontSize="xs" maxW="320px">
                        {r.comment ?? "—"}
                      </Table.Cell>
                      <Table.Cell fontSize="xs">{formatTime(r.timestamp, i18n.language)}</Table.Cell>
                    </Table.Row>
                  ))}
                </Table.Body>
              </DataTable>
            ) : (
              <Text fontSize="sm" color="fg.muted">
                —
              </Text>
            )}
          </Section>
        </Stack>
      ) : null}
    </QueryState>
  );
}
