import { Badge, Button, Code, HStack, Table, Text } from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { LuPackagePlus, LuPencil, LuTrash2 } from "react-icons/lu";

import { adminDeleteTemplateMutation, adminTemplatesOptions } from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import { useLifecycle } from "../../lifecycle/store";
import { toaster } from "../../ui/toaster";
import { useAdmin } from "../store";
import { DataTable, Empty, QueryState, Section } from "../common";

export function TemplatesTab({ canWrite }: { canWrite: boolean }) {
  const { t } = useTranslation("admin");
  const client = useQueryClient();
  const q = useQuery(adminTemplatesOptions());
  const remove = useMutation({
    ...adminDeleteTemplateMutation(),
    onSuccess: () => {
      toaster.create({ type: "success", title: t("templates.deleted") });
      void client.invalidateQueries();
    },
    onError: (err) => toaster.create({ type: "error", title: t("error", { message: errorMessage(err) }) }),
  });
  const rows = q.data ?? [];
  // The editor is a lifecycle dialog: the (full-screen) admin panel closes first.
  const openEditor = (mode: "new" | "edit", templateId?: string) => {
    useAdmin.getState().close();
    useLifecycle.getState().openDialog({ kind: "operatorTemplateEditor", mode, templateId });
  };
  return (
    <Section
      help={t("templates.help")}
      actions={
        canWrite ? (
          <Button
            size="xs"
            variant="outline"
            data-testid="admin-new-operator-template"
            onClick={() => openEditor("new")}
          >
            <LuPackagePlus /> {t("templates.newOperator")}
          </Button>
        ) : null
      }
    >
      <QueryState isPending={q.isPending} error={q.error}>
        {rows.length ? (
          <DataTable label={t("templates.table")} testId="admin-templates-table">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeader>{t("templates.title")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("templates.id")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("templates.kind")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("templates.category")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("templates.origin")}</Table.ColumnHeader>
                <Table.ColumnHeader />
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {rows.map((tpl) => (
                <Table.Row key={tpl.id}>
                  <Table.Cell>
                    <Text fontWeight="medium">{tpl.title}</Text>
                    {tpl.description ? (
                      <Text fontSize="xs" color="fg.muted" lineClamp={1}>
                        {tpl.description}
                      </Text>
                    ) : null}
                  </Table.Cell>
                  <Table.Cell>
                    <Code fontSize="xs">{tpl.id}</Code>
                  </Table.Cell>
                  <Table.Cell fontSize="xs">{tpl.kind}</Table.Cell>
                  <Table.Cell fontSize="xs">{tpl.category}</Table.Cell>
                  <Table.Cell>
                    <Badge size="xs" colorPalette={tpl.builtin ? "gray" : "blue"}>
                      {tpl.builtin ? t("templates.builtin") : t("templates.saved")}
                    </Badge>
                  </Table.Cell>
                  <Table.Cell textAlign="end">
                    {!tpl.builtin && canWrite ? (
                      <HStack gap={1} justify="end">
                        {tpl.kind === "operator" ? (
                          <Button
                            size="xs"
                            variant="ghost"
                            aria-label={t("templates.editLabel", { id: tpl.id })}
                            onClick={() => openEditor("edit", tpl.id)}
                          >
                            <LuPencil /> {t("templates.edit")}
                          </Button>
                        ) : null}
                        <Button
                          size="xs"
                          variant="ghost"
                          colorPalette="red"
                          aria-label={t("templates.deleteLabel", { id: tpl.id })}
                          loading={remove.isPending && remove.variables?.path.template_id === tpl.id}
                          onClick={() => {
                            if (window.confirm(t("templates.confirm", { id: tpl.id }))) {
                              remove.mutate({ path: { template_id: tpl.id } });
                            }
                          }}
                        >
                          <LuTrash2 /> {t("templates.delete")}
                        </Button>
                      </HStack>
                    ) : null}
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
