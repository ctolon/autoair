import { Badge, Code, Table, Text, Wrap } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { adminUsersOptions } from "../../api/gen/@tanstack/react-query.gen";
import { DataTable, Empty, QueryState, Section } from "../common";
import { formatTime } from "../format";

export function UsersTab() {
  const { t, i18n } = useTranslation("admin");
  const q = useQuery(adminUsersOptions());
  const users = q.data ?? [];
  return (
    <Section help={t("users.help")}>
      <QueryState isPending={q.isPending} error={q.error}>
        {users.length ? (
          <DataTable label={t("users.table")} testId="admin-users-table">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeader>{t("users.user")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("users.roles")}</Table.ColumnHeader>
                <Table.ColumnHeader textAlign="end">{t("users.permissions")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("users.lastSeen")}</Table.ColumnHeader>
                <Table.ColumnHeader textAlign="end">{t("users.actions")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("users.lastAction")}</Table.ColumnHeader>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {users.map((u) => (
                <Table.Row key={u.user}>
                  <Table.Cell fontWeight="medium">{u.user}</Table.Cell>
                  <Table.Cell>
                    {u.roles?.length ? (
                      <Wrap gap={1}>
                        {u.roles.map((r) => (
                          <Badge key={r} size="xs" variant="surface">
                            {r}
                          </Badge>
                        ))}
                      </Wrap>
                    ) : (
                      <Text fontSize="xs" color="fg.muted">
                        {t("users.noRoles")}
                      </Text>
                    )}
                  </Table.Cell>
                  <Table.Cell textAlign="end" fontSize="xs">
                    {u.permissions ?? 0}
                  </Table.Cell>
                  <Table.Cell fontSize="xs">{formatTime(u.last_seen, i18n.language)}</Table.Cell>
                  <Table.Cell textAlign="end" fontSize="xs">
                    {u.actions ?? 0}
                  </Table.Cell>
                  <Table.Cell fontSize="xs">
                    {u.last_action ? <Code fontSize="xs">{u.last_action}</Code> : "—"}{" "}
                    <Text as="span" color="fg.muted">
                      {u.last_action_at ? formatTime(u.last_action_at, i18n.language) : ""}
                    </Text>
                  </Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </DataTable>
        ) : (
          <Empty />
        )}
      </QueryState>
    </Section>
  );
}
