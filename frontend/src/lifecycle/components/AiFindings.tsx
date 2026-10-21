import { Alert, Badge, Box, Button, Checkbox, HStack, Spinner, Stack, Text, chakra } from "@chakra-ui/react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { LuCheck, LuCrosshair, LuGitCompare } from "react-icons/lu";

import { applyFindings, diffVersions } from "../../api/gen/sdk.gen";
import type { DagSpec, Finding, Usage } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { toaster } from "../../ui/toaster";
import { layoutCanvas, loadAsUnsaved } from "../actions";
import { SEVERITY_ORDER, formatUsage, revealFinding, severityPalette } from "../lib/ai";
import { DiffSummary } from "./DiffSummary";

/** "model · tokens · cost" line under AI results. */
export function UsageLine({ model, usage }: { model?: string | null; usage?: Usage | null }) {
  const text = [model, formatUsage(usage)].filter(Boolean).join(" · ");
  return text ? (
    <Text fontSize="2xs" color="fg.muted" data-testid="ai-usage">
      {text}
    </Text>
  ) : null;
}

export function Pending({ label }: { label: string }) {
  return (
    <HStack role="status" gap={2} py={2}>
      <Spinner size="sm" />
      <Text fontSize="sm">{label}</Text>
    </HStack>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  return (
    <Alert.Root status="error">
      <Alert.Indicator />
      <Alert.Description>{errorMessage(error)}</Alert.Description>
    </Alert.Root>
  );
}

function locationLabel(f: Finding): string | null {
  if (!f.node_id && !f.field) return null;
  return [f.node_id, f.field].filter(Boolean).join(" · ");
}

/**
 * Findings grouped by severity. A finding with a location reveals it in the editor (selects the
 * node, focuses the field); findings with a patch can be selected for "Review changes".
 */
export function FindingsList({
  findings,
  selected,
  onToggle,
  onReveal,
}: {
  findings: Finding[];
  selected?: Set<string>;
  onToggle?: (id: string, on: boolean) => void;
  onReveal?: (f: Finding) => void;
}) {
  const { t } = useTranslation("lifecycle");
  if (!findings.length) {
    return (
      <Text fontSize="sm" color="fg.muted">
        {t("ai.findings.none")}
      </Text>
    );
  }
  const ordered = [...findings].sort((a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity));
  return (
    <Stack gap={2} as="ul" listStyleType="none" m={0} p={0} aria-label={t("ai.findings.title")}>
      {ordered.map((f) => {
        const where = locationLabel(f);
        const canPatch = (f.patch ?? []).length > 0 && !!onToggle;
        return (
          <Box as="li" key={f.id} borderWidth="1px" rounded="md" p={2} data-testid="ai-finding">
            <HStack gap={2} align="start">
              {canPatch ? (
                <Checkbox.Root
                  size="sm"
                  mt={0.5}
                  checked={selected?.has(f.id) ?? false}
                  onCheckedChange={(e) => onToggle?.(f.id, e.checked === true)}
                  aria-label={t("ai.findings.include", { title: f.title })}
                >
                  <Checkbox.HiddenInput />
                  <Checkbox.Control />
                </Checkbox.Root>
              ) : null}
              <Stack gap={1} flex="1" minW={0}>
                <HStack gap={1.5} wrap="wrap">
                  <Badge size="xs" colorPalette={severityPalette(f.severity)}>
                    {t(`ai.severity.${f.severity}`)}
                  </Badge>
                  <Badge size="xs" variant="outline">
                    {t(`ai.category.${f.category}`)}
                  </Badge>
                  {f.source === "ai" ? (
                    <Badge size="xs" variant="subtle" colorPalette="purple">
                      AI
                    </Badge>
                  ) : null}
                  <Text fontSize="sm" fontWeight="semibold">
                    {f.title}
                  </Text>
                </HStack>
                {f.message ? (
                  <Text fontSize="xs" color="fg.muted" whiteSpace="pre-wrap">
                    {f.message}
                  </Text>
                ) : null}
                {f.evidence ? (
                  <chakra.code fontSize="2xs" bg="bg.muted" px={1} rounded="sm" wordBreak="break-all">
                    {f.evidence}
                  </chakra.code>
                ) : null}
                <HStack gap={2} wrap="wrap">
                  {where ? (
                    <Button
                      size="2xs"
                      variant="ghost"
                      onClick={() => (onReveal ? onReveal(f) : revealFinding(f))}
                      title={t("ai.findings.reveal")}
                    >
                      <LuCrosshair /> {where}
                    </Button>
                  ) : null}
                  {canPatch ? (
                    <Text fontSize="2xs" color="fg.muted">
                      {t("ai.findings.fixAvailable", { count: f.patch?.length ?? 0 })}
                    </Text>
                  ) : null}
                </HStack>
              </Stack>
            </HStack>
          </Box>
        );
      })}
    </Stack>
  );
}

/**
 * Applies the selected findings' patches on the server (validated), shows the result as a diff
 * against the snapshot the analysis ran on, then loads it into the editor (undoable, unsaved).
 */
export function ProposalReview({
  spec,
  findings,
  selected,
  proposed,
  onApplied,
}: {
  spec: DagSpec;
  findings?: Finding[];
  selected?: Set<string>;
  /** A complete proposed spec (agent answers): reviewed as is. */
  proposed?: DagSpec | null;
  onApplied: () => void;
}) {
  const { t } = useTranslation("lifecycle");
  const preview = useMutation({
    mutationFn: async () => {
      if (proposed) return { spec: proposed, ok: true, problems: [], applied: [], skipped: [] };
      const { data } = await applyFindings({
        body: { spec, findings: findings ?? [], only: [...(selected ?? [])] },
        throwOnError: true,
      });
      return data;
    },
    onError: (e) => toaster.create({ type: "error", title: t("ai.failed"), description: errorMessage(e) }),
  });
  const next = preview.data?.spec ?? null;
  const diff = useQuery({
    queryKey: ["lifecycle", "ai-proposal-diff", spec, next],
    enabled: !!next,
    queryFn: async ({ signal }) =>
      (await diffVersions({ body: { a: spec, b: next as DagSpec }, signal, throwOnError: true })).data,
    staleTime: Infinity,
  });
  const count = proposed ? 1 : (selected?.size ?? 0);
  return (
    <Stack gap={2}>
      <HStack gap={2}>
        <Button
          size="sm"
          variant="outline"
          disabled={!count}
          loading={preview.isPending}
          onClick={() => preview.mutate()}
        >
          <LuGitCompare /> {proposed ? t("ai.proposal.reviewSpec") : t("ai.proposal.review", { count })}
        </Button>
        <Button
          size="sm"
          colorPalette="blue"
          disabled={!next || diff.data?.identical}
          onClick={() => {
            if (!next) return;
            loadAsUnsaved(next);
            const added = diff.data?.added_nodes?.length ?? 0;
            onApplied();
            if (added) requestAnimationFrame(() => void layoutCanvas());
            toaster.create({ type: "success", title: t("ai.proposal.applied") });
          }}
        >
          <LuCheck /> {t("ai.apply")}
        </Button>
      </HStack>
      {preview.data ? (
        <HStack gap={2} wrap="wrap">
          <Badge colorPalette={preview.data.ok ? "green" : "orange"}>
            {preview.data.ok ? t("ai.valid") : t("ai.invalid")}
          </Badge>
          {preview.data.skipped.length ? (
            <Text fontSize="xs" color="orange.500">
              {t("ai.proposal.skipped", { count: preview.data.skipped.length })}
            </Text>
          ) : null}
        </HStack>
      ) : null}
      {preview.data && !preview.data.ok
        ? preview.data.problems
            .filter((p) => p.severity === "error")
            .slice(0, 5)
            .map((p, i) => (
              <Text key={i} fontSize="xs" color="red.500">
                {p.message}
              </Text>
            ))
        : null}
      {diff.isPending && next ? <Spinner size="sm" /> : null}
      {diff.data ? (
        <Box borderWidth="1px" rounded="md" p={3}>
          <Text fontWeight="semibold" fontSize="sm" mb={2}>
            {t("ai.proposedChanges")}
          </Text>
          <DiffSummary diff={diff.data} />
        </Box>
      ) : null}
    </Stack>
  );
}
