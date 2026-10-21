import { Badge, Box, HStack, SegmentGroup, Stack, Text, chakra } from "@chakra-ui/react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCircleAlert, LuInfo, LuTriangleAlert } from "react-icons/lu";

import type { Problem } from "../../api/gen/types.gen";
import { nodeKey } from "../../lib/types";
import { focusNode } from "../../canvas/actions";
import { isRawDag, useCodeMode } from "../../codemode/store";
import { useBuilder } from "../../store/builder";
import { useFocusRequest } from "../../store/focus";
import { problemText } from "../../lib/problemMessages";
import { ProblemCode, splitLine } from "../../widgets/ProblemCode";

type Filter = "all" | Problem["severity"];

const ICON = {
  error: <LuCircleAlert color="var(--chakra-colors-red-500)" aria-hidden />,
  warning: <LuTriangleAlert color="var(--chakra-colors-orange-400)" aria-hidden />,
  info: <LuInfo aria-hidden />,
};
const RANK = { error: 0, warning: 1, info: 2 } as const;
const SEVERITY_COLOR = { error: "red.500", warning: "orange.400", info: "blue.400" } as const;

/** Problems grouped by node, filterable by severity; click one to jump to its field. */
export function ProblemsTab() {
  const { t } = useTranslation("lifecycle");
  const problems = useBuilder((s) => s.problems);
  const nodes = useBuilder((s) => s.nodes);
  const select = useBuilder((s) => s.select);
  const requestFocus = useFocusRequest((s) => s.focus);
  /** Select the node (or the DAG settings), open the owning tab and focus the field. */
  const goTo = (p: Problem) => {
    // A raw code DAG: located problems point into its file.
    if (isRawDag(useBuilder.getState().dag) && !p.node_id && p.field !== "dag_id") {
      if (p.line) useCodeMode.getState().revealLine(p.line, p.column ?? null);
      return;
    }
    if (p.node_id) {
      if (!nodes.some((n) => n.id === p.node_id)) return;
      focusNode(p.node_id);
    } else select(null);
    requestFocus(p);
  };
  const [filter, setFilter] = useState<Filter>("all");
  const counts = useMemo(() => {
    const c = { error: 0, warning: 0, info: 0 };
    for (const p of problems) c[p.severity]++;
    return c;
  }, [problems]);
  const groups = useMemo(() => {
    const byNode = new Map<string, Problem[]>();
    for (const p of problems) {
      if (filter !== "all" && p.severity !== filter) continue;
      const key = p.node_id ?? "";
      byNode.set(key, [...(byNode.get(key) ?? []), p]);
    }
    return [...byNode.entries()]
      .map(([nodeId, list]) => ({ nodeId, list: [...list].sort((a, b) => RANK[a.severity] - RANK[b.severity]) }))
      .sort((a, b) =>
        a.nodeId === ""
          ? -1
          : b.nodeId === ""
            ? 1
            : RANK[a.list[0]?.severity ?? "info"] - RANK[b.list[0]?.severity ?? "info"],
      );
  }, [problems, filter]);
  const nameOf = (id: string) => {
    if (!id) return t("problems.dagLevel");
    const n = nodes.find((x) => x.id === id);
    return n ? nodeKey(n) : id;
  };

  return (
    <Stack gap={0}>
      <HStack px={3} py={1.5} borderBottomWidth="1px" gap={2}>
        <SegmentGroup.Root size="xs" value={filter} onValueChange={(e) => setFilter((e.value as Filter) ?? "all")}>
          <SegmentGroup.Indicator />
          <SegmentGroup.Items
            items={[
              { value: "all", label: t("problems.all", { count: problems.length }) },
              { value: "error", label: t("problems.errors", { count: counts.error }) },
              { value: "warning", label: t("problems.warnings", { count: counts.warning }) },
              { value: "info", label: t("problems.infos", { count: counts.info }) },
            ]}
          />
        </SegmentGroup.Root>
      </HStack>
      {!groups.length ? (
        <Text fontSize="sm" color="fg.muted" p={3}>
          {problems.length ? t("problems.noneForFilter") : t("problems.none")}
        </Text>
      ) : null}
      {groups.map(({ nodeId, list }) => (
        <Box key={nodeId || "__dag__"} as="section" aria-label={nameOf(nodeId)}>
          <HStack px={3} pt={2} pb={1} gap={2}>
            {nodeId ? (
              <chakra.button
                type="button"
                fontSize="xs"
                fontWeight="bold"
                fontFamily="mono"
                textDecoration="underline dotted"
                onClick={() => select(nodeId)}
              >
                {nameOf(nodeId)}
              </chakra.button>
            ) : (
              <Text fontSize="xs" fontWeight="bold">
                {nameOf(nodeId)}
              </Text>
            )}
            <Badge size="xs" variant="subtle">
              {list.length}
            </Badge>
          </HStack>
          <Stack as="ul" gap={0} listStyleType="none" m={0} p={0}>
            {list.map((p, i) => (
              <HStack
                as="li"
                key={`${p.code}-${i}`}
                ps={4}
                pe={3}
                py={1}
                gap={2}
                align="start"
                cursor="pointer"
                borderStartWidth="3px"
                borderStartColor={SEVERITY_COLOR[p.severity]}
                role="button"
                tabIndex={0}
                title={t("problems.goTo")}
                _hover={{ bg: "bg.muted" }}
                _focusVisible={{ outline: "2px solid", outlineColor: "blue.500", outlineOffset: "-2px" }}
                onClick={() => goTo(p)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    goTo(p);
                  }
                }}
              >
                <Box pt={0.5}>{ICON[p.severity]}</Box>
                <Text srOnly>{t(`problems.severity.${p.severity}`)}</Text>
                <Text fontSize="sm" whiteSpace="pre-wrap" flex="1">
                  {p.field ? (
                    <Text as="span" fontFamily="mono" color="fg.muted">
                      {p.field}:{" "}
                    </Text>
                  ) : null}
                  {splitLine(problemText(p), p.line)[1]}
                </Text>
                <ProblemCode code={p.code} line={splitLine(p.message, p.line)[0]} docUrl={p.doc_url} />
              </HStack>
            ))}
          </Stack>
        </Box>
      ))}
    </Stack>
  );
}
