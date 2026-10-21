import { Box, Button, HStack, Spinner, Stack, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCode } from "react-icons/lu";

import { diffVersions, generateCode } from "../api/gen/sdk.gen";
import type { DagSpec, SpecProblem } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { DiffSummary } from "../lifecycle/components/DiffSummary";
import { DiffView } from "../lifecycle/components/lazy";

async function codeOf(spec: DagSpec, signal: AbortSignal): Promise<string> {
  const { data } = await generateCode({ body: spec, signal, throwOnError: true });
  return data.code ?? "";
}

/**
 * Review of a proposed DAG change: the structured spec diff, validation problems of the
 * proposal and (on demand) the side-by-side diff of the generated Python file.
 */
export function SpecDiffReview({
  base,
  proposed,
  problems,
  codeBefore,
  codeAfter,
  testId = "spec-diff-review",
}: {
  base: DagSpec | null | undefined;
  proposed: DagSpec | null | undefined;
  problems?: SpecProblem[] | null;
  codeBefore?: string | null;
  codeAfter?: string | null;
  testId?: string;
}) {
  const { t } = useTranslation("ai");
  const [showCode, setShowCode] = useState(false);
  const diff = useQuery({
    queryKey: ["ai", "spec-diff", base, proposed],
    enabled: !!base && !!proposed,
    queryFn: async ({ signal }) =>
      (await diffVersions({ body: { a: base as DagSpec, b: proposed as DagSpec }, signal, throwOnError: true })).data,
    staleTime: Infinity,
    retry: false,
  });
  const code = useQuery({
    queryKey: ["ai", "code-diff", base, proposed, codeBefore, codeAfter],
    enabled: showCode && !!proposed,
    queryFn: async ({ signal }) => ({
      before: codeBefore ?? (base ? await codeOf(base, signal) : ""),
      after: codeAfter ?? (proposed ? await codeOf(proposed, signal) : ""),
    }),
    staleTime: Infinity,
    retry: false,
  });
  const errors = (problems ?? []).filter((p) => p.severity === "error");
  return (
    <Stack gap={2} data-testid={testId}>
      {diff.isPending && base && proposed ? <Spinner size="xs" /> : null}
      {diff.isError ? (
        <Text fontSize="xs" color="fg.error">
          {errorMessage(diff.error)}
        </Text>
      ) : null}
      {diff.data ? (
        <Box borderWidth="1px" rounded="md" p={2} bg="bg.subtle" maxH="260px" overflowY="auto">
          <DiffSummary diff={diff.data} />
        </Box>
      ) : null}
      {!base && proposed ? (
        <Text fontSize="xs" color="fg.muted">
          {t("proposal.newDag")}
        </Text>
      ) : null}
      {errors.length ? (
        <Stack gap={0.5} role="alert">
          <Text fontSize="xs" fontWeight="semibold" color="fg.error">
            {t("proposal.problems", { count: errors.length })}
          </Text>
          {errors.slice(0, 6).map((p, i) => (
            <Text key={i} fontSize="xs" color="fg.error">
              {p.message}
            </Text>
          ))}
        </Stack>
      ) : null}
      {proposed ? (
        <HStack>
          <Button size="2xs" variant="ghost" onClick={() => setShowCode((v) => !v)} aria-expanded={showCode}>
            <LuCode /> {showCode ? t("proposal.hideCode") : t("proposal.showCode")}
          </Button>
        </HStack>
      ) : null}
      {showCode ? (
        code.data ? (
          <DiffView
            before={code.data.before}
            after={code.data.after}
            language="python"
            height="320px"
            label={t("proposal.codeDiff")}
          />
        ) : code.isError ? (
          <Text fontSize="xs" color="fg.error">
            {errorMessage(code.error)}
          </Text>
        ) : (
          <Spinner size="xs" />
        )
      ) : null}
    </Stack>
  );
}
