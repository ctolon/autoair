import { Badge, Box, Code, HStack, Stack, Text } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";

import type { FieldChange, SpecDiff } from "../../api/gen/types.gen";
import { pretty } from "./JsonBlock";

const short = (v: unknown) => {
  const s = pretty(v);
  return s.length > 120 ? `${s.slice(0, 117)}…` : s;
};

function Changes({ changes }: { changes: FieldChange[] | undefined }) {
  const { t } = useTranslation("lifecycle");
  if (!changes?.length) return null;
  // An absent key and an explicit null differ (null overrides a DAG default; absence inherits).
  const shown = (value: unknown, present: boolean | undefined) => (present === false ? t("diff.absent") : short(value));
  return (
    <Stack gap={0.5} pl={4} as="ul" listStyleType="none" m={0}>
      {changes.map((c) => (
        <Text as="li" key={c.path} fontSize="xs" wordBreak="break-word">
          <Code fontSize="xs">{c.path || "·"}</Code>{" "}
          <Text as="span" color="red.500" textDecoration="line-through">
            {shown(c.before, c.before_present)}
          </Text>{" "}
          →{" "}
          <Text as="span" color="green.600">
            {shown(c.after, c.after_present)}
          </Text>
        </Text>
      ))}
    </Stack>
  );
}

const nodeLabel = (n: Record<string, unknown>) => String(n.task_id ?? n.group_id ?? n.label ?? n.id ?? "?");

/** Structured, readable summary of a spec diff (nodes, edges, DAG fields). */
export function DiffSummary({ diff, showLayout = false }: { diff: SpecDiff; showLayout?: boolean }) {
  const { t } = useTranslation("lifecycle");
  if (diff.identical) {
    return (
      <Text fontSize="sm" color="fg.muted">
        {t("diff.identical")}
      </Text>
    );
  }
  const changed = (diff.changed_nodes ?? []).filter((n) => showLayout || !n.layout_only);
  const layoutOnly = (diff.changed_nodes ?? []).length - changed.length;
  return (
    <Stack gap={2} fontSize="sm">
      <HStack gap={2} wrap="wrap">
        <Badge colorPalette="green">{t("diff.added", { count: diff.added_nodes?.length ?? 0 })}</Badge>
        <Badge colorPalette="red">{t("diff.removed", { count: diff.removed_nodes?.length ?? 0 })}</Badge>
        <Badge colorPalette="orange">{t("diff.changed", { count: changed.length })}</Badge>
        <Badge variant="outline">
          {t("diff.edges", { added: diff.added_edges?.length ?? 0, removed: diff.removed_edges?.length ?? 0 })}
        </Badge>
        {layoutOnly ? <Badge variant="outline">{t("diff.layoutOnly", { count: layoutOnly })}</Badge> : null}
      </HStack>
      {diff.changed_fields?.length ? (
        <Box>
          <Text fontWeight="semibold" fontSize="xs">
            {t("diff.dagFields")}
          </Text>
          <Changes changes={diff.changed_fields} />
        </Box>
      ) : null}
      {(diff.added_nodes ?? []).map((n) => (
        <Text key={`a-${String(n.id)}`} fontSize="xs" color="green.600">
          + {nodeLabel(n)}{" "}
          <Text as="span" color="fg.muted">
            ({String(n.kind ?? "")})
          </Text>
        </Text>
      ))}
      {(diff.removed_nodes ?? []).map((n) => (
        <Text key={`r-${String(n.id)}`} fontSize="xs" color="red.500">
          − {nodeLabel(n)}
        </Text>
      ))}
      {changed.map((n) => (
        <Box key={`c-${n.id}`}>
          <Text fontSize="xs" color="orange.500">
            ~ {n.id}{" "}
            {n.kind ? (
              <Text as="span" color="fg.muted">
                ({n.kind})
              </Text>
            ) : null}
          </Text>
          <Changes changes={n.changes?.filter((c) => showLayout || !c.path.startsWith("position"))} />
        </Box>
      ))}
      {(diff.added_edges ?? []).map((e) => (
        <Text key={`ae-${e.source}-${e.target}`} fontSize="xs" color="green.600">
          + {e.source} → {e.target}
        </Text>
      ))}
      {(diff.removed_edges ?? []).map((e) => (
        <Text key={`re-${e.source}-${e.target}`} fontSize="xs" color="red.500">
          − {e.source} → {e.target}
        </Text>
      ))}
    </Stack>
  );
}
