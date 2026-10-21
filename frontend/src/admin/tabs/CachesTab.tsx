import { Button, Table, Text } from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { LuRefreshCw } from "react-icons/lu";

import { adminCachesOptions, adminClearCacheMutation } from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import { toaster } from "../../ui/toaster";
import { DataTable, QueryState, Section } from "../common";
import { formatNumber } from "../format";

type CacheName = "catalog" | "marketplace" | "ai-index";
const KNOWN: readonly string[] = ["catalog", "marketplace", "ai-index"];

export function CachesTab({ canWrite }: { canWrite: boolean }) {
  const { t, i18n } = useTranslation("admin");
  const client = useQueryClient();
  const q = useQuery(adminCachesOptions());
  const clear = useMutation({
    ...adminClearCacheMutation(),
    onSuccess: (res) => {
      toaster.create({
        type: "success",
        title: t("caches.done", { name: t(`caches.names.${res.cache}`, { defaultValue: res.cache }) }),
        description: res.detail ?? undefined,
      });
      void client.invalidateQueries();
    },
    onError: (err) => toaster.create({ type: "error", title: t("error", { message: errorMessage(err) }) }),
  });
  return (
    <Section help={t("caches.help")}>
      <QueryState isPending={q.isPending} error={q.error}>
        <DataTable label={t("tabs.caches")} testId="admin-caches-table">
          <Table.Body>
            {(q.data ?? []).map((c) => (
              <Table.Row key={c.name}>
                <Table.Cell>
                  <Text fontWeight="medium">{t(`caches.names.${c.name}`, { defaultValue: c.name })}</Text>
                  <Text fontSize="xs" color="fg.muted">
                    {c.description}
                  </Text>
                </Table.Cell>
                <Table.Cell textAlign="end">
                  <Text fontSize="xs" color="fg.muted">
                    {t("caches.entries")}
                  </Text>
                  {formatNumber(c.entries, i18n.language)}
                </Table.Cell>
                <Table.Cell fontSize="xs" color="fg.muted">
                  {c.detail ?? ""}
                </Table.Cell>
                <Table.Cell textAlign="end">
                  {canWrite && KNOWN.includes(c.name) ? (
                    <Button
                      size="xs"
                      variant="outline"
                      loading={clear.isPending && clear.variables?.path.cache === c.name}
                      onClick={() => clear.mutate({ path: { cache: c.name as CacheName } })}
                    >
                      <LuRefreshCw /> {t(`caches.clear.${c.name}`)}
                    </Button>
                  ) : null}
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </DataTable>
      </QueryState>
    </Section>
  );
}
