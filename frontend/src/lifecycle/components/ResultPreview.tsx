import { Badge, HStack, Stack, Text } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";

import { JsonBlock } from "./JsonBlock";

type Row = Record<string, unknown>;
const isRow = (v: unknown): v is Row => typeof v === "object" && v !== null && !Array.isArray(v);

/** First array of objects inside an Airflow API response (e.g. `backfills`, `task_instances`). */
function rowsOf(result: unknown): Row[] | null {
  if (Array.isArray(result)) return result.filter(isRow);
  if (!isRow(result)) return null;
  for (const v of Object.values(result)) if (Array.isArray(v)) return v.filter(isRow);
  return null;
}

const str = (v: unknown) => (v === null || v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v));

/** Dry-run preview of an Airflow bulk operation (list of runs / task instances). */
export function ResultPreview({ result, columns }: { result: unknown; columns: string[] }) {
  const { t } = useTranslation("lifecycle");
  const rows = rowsOf(result);
  if (!rows) return <JsonBlock value={result} />;
  if (!rows.length) {
    return (
      <Text fontSize="sm" color="fg.muted">
        {t("common.nothingAffected")}
      </Text>
    );
  }
  return (
    <Stack gap={1} maxH="260px" overflowY="auto" as="ul" listStyleType="none" m={0} p={0}>
      <Text fontSize="xs" color="fg.muted">
        {t("common.affected", { count: rows.length })}
      </Text>
      {rows.map((r, i) => (
        <HStack as="li" key={i} gap={2} fontSize="xs" fontFamily="mono">
          {columns.map((c) =>
            c === "state" ? (
              <Badge key={c} size="xs">
                {str(r[c]) || "—"}
              </Badge>
            ) : (
              <Text key={c}>{str(r[c])}</Text>
            ),
          )}
        </HStack>
      ))}
    </Stack>
  );
}
