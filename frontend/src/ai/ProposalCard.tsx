import { Badge, Box, Button, HStack, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCheck, LuCopy, LuRocket, LuX } from "react-icons/lu";

import { decideAssistantProposal } from "../api/gen/sdk.gen";
import type { Proposal } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { useAccess } from "../lib/access";
import { confirmDiscard, loadAsUnsaved } from "../lifecycle/actions";
import { severityPalette } from "../lifecycle/lib/ai";
import { useLifecycle } from "../lifecycle/store";
import { useBuilder } from "../store/builder";
import { toaster } from "../ui/toaster";
import { CodeBlock } from "../widgets/markdown/Markdown";
import { copyText } from "./ChatMarkdown";
import { watchNextDeploy } from "./deployWatch";
import { SpecDiffReview } from "./SpecDiffReview";

const STATUS_PALETTE: Record<string, string> = {
  proposed: "blue",
  approved: "green",
  applied: "green",
  rejected: "gray",
};

/** A change proposed by an assistant: reviewed as a diff, applied only on approval. */
export function ProposalCard({
  proposal,
  threadId,
  onChange,
}: {
  proposal: Proposal;
  threadId: string | null;
  onChange: (p: Proposal) => void;
}) {
  const { t } = useTranslation("ai");
  const access = useAccess();
  const [busy, setBusy] = useState(false);
  const status = proposal.status ?? "proposed";
  const decide = async (decision: "approve" | "reject" | "applied", detail?: Record<string, unknown>) => {
    if (!threadId || !proposal.id) return null;
    const { data } = await decideAssistantProposal({
      path: { thread_id: threadId, proposal_id: proposal.id },
      body: { decision, ...(detail ? { detail } : {}) },
      throwOnError: true,
    });
    onChange(data);
    return data;
  };
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      toaster.create({ type: "error", title: t("proposal.failed"), description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };

  const apply = () =>
    run(async () => {
      const spec = proposal.proposed_spec;
      if (!spec) return;
      const current = useBuilder.getState();
      if (current.savedDagId && current.savedDagId !== spec.dag_id && !confirmDiscard()) return;
      loadAsUnsaved(spec);
      await decide("approve");
      toaster.create({ type: "success", title: t("proposal.applied"), description: t("proposal.appliedHelp") });
    });
  const deploy = () => {
    const spec = proposal.proposed_spec;
    if (!spec) return;
    watchNextDeploy(spec.dag_id, (info) => {
      void decide("applied", { etag: info.etag, pr_url: info.prUrl }).catch(() => undefined);
    });
    useLifecycle.getState().openDialog({ kind: "deploy", focus: "message" });
  };

  if (proposal.kind === "config_snippet") {
    return (
      <Box borderWidth="1px" rounded="md" p={3} bg="bg.panel" data-testid="proposal-card" data-kind="config">
        <HStack justify="space-between" gap={2} mb={1}>
          <Text fontWeight="semibold" fontSize="sm">
            {proposal.title}
          </Text>
          <Badge size="xs" colorPalette={STATUS_PALETTE[status]}>
            {t(`proposal.status.${status}`)}
          </Badge>
        </HStack>
        {proposal.summary ? (
          <Text fontSize="xs" color="fg.muted" mb={1}>
            {proposal.summary}
          </Text>
        ) : null}
        <CodeBlock
          code={proposal.snippet ?? ""}
          language={proposal.format === "env" ? "bash" : (proposal.format ?? "yaml")}
        />
        <Text fontSize="2xs" color="fg.muted">
          {t("proposal.snippetHelp")}
        </Text>
        <HStack mt={2} gap={2} wrap="wrap">
          <Button
            size="xs"
            variant="outline"
            onClick={() =>
              void copyText(proposal.snippet ?? "").then(
                (ok) => ok && toaster.create({ type: "success", title: t("chat.copied") }),
              )
            }
          >
            <LuCopy /> {t("chat.copy")}
          </Button>
          {status === "proposed" ? (
            <>
              <Button
                size="xs"
                colorPalette="green"
                variant="subtle"
                loading={busy}
                onClick={() => void run(() => decide("applied"))}
              >
                <LuCheck /> {t("proposal.markApplied")}
              </Button>
              <Button size="xs" variant="ghost" loading={busy} onClick={() => void run(() => decide("reject"))}>
                <LuX /> {t("proposal.dismiss")}
              </Button>
            </>
          ) : null}
        </HStack>
      </Box>
    );
  }

  const canApply = access.can("spec:write") && !!proposal.proposed_spec;
  return (
    <Box
      borderWidth="1px"
      borderColor="blue.muted"
      rounded="md"
      p={3}
      bg="bg.panel"
      data-testid="proposal-card"
      data-kind="spec"
    >
      <HStack justify="space-between" gap={2} mb={1} align="start">
        <Box minW={0}>
          <Text fontWeight="semibold" fontSize="sm">
            {proposal.title}
          </Text>
          {proposal.summary ? (
            <Text fontSize="xs" color="fg.muted">
              {proposal.summary}
            </Text>
          ) : null}
        </Box>
        <HStack gap={1} flexShrink={0}>
          <Badge size="xs" colorPalette={proposal.proposed_ok ? "green" : "orange"}>
            {proposal.proposed_ok ? t("proposal.valid") : t("proposal.invalid")}
          </Badge>
          <Badge size="xs" colorPalette={STATUS_PALETTE[status]}>
            {t(`proposal.status.${status}`)}
          </Badge>
        </HStack>
      </HStack>
      {proposal.findings?.length ? (
        <Stack gap={1} my={2} as="ul" listStyleType="none" m={0} p={0}>
          {proposal.findings.map((f) => (
            <HStack as="li" key={f.id} gap={1.5} align="start" fontSize="xs">
              <Badge size="xs" colorPalette={severityPalette(f.severity)} flexShrink={0}>
                {f.severity}
              </Badge>
              <Text>
                <Text as="span" fontWeight="medium">
                  {f.title}
                </Text>
                {f.node_id ? (
                  <Text as="span" color="fg.muted" fontFamily="mono">
                    {" "}
                    @{f.node_id}
                  </Text>
                ) : null}
              </Text>
            </HStack>
          ))}
        </Stack>
      ) : null}
      <SpecDiffReview base={proposal.base_spec} proposed={proposal.proposed_spec} problems={proposal.problems} />
      <HStack mt={2} gap={2} wrap="wrap">
        {status === "proposed" ? (
          <>
            <Button
              size="xs"
              colorPalette="blue"
              disabled={!canApply}
              title={!access.can("spec:write") ? t("proposal.needsWrite") : undefined}
              loading={busy}
              onClick={() => void apply()}
              data-testid="proposal-apply"
            >
              <LuCheck /> {t("proposal.apply")}
            </Button>
            <Button
              size="xs"
              variant="ghost"
              loading={busy}
              onClick={() => void run(() => decide("reject"))}
              data-testid="proposal-reject"
            >
              <LuX /> {t("proposal.reject")}
            </Button>
          </>
        ) : null}
        {status === "approved" && access.can("deploy") ? (
          <Button size="xs" variant="outline" onClick={deploy} data-testid="proposal-deploy">
            <LuRocket /> {t("proposal.deploy")}
          </Button>
        ) : null}
      </HStack>
    </Box>
  );
}
