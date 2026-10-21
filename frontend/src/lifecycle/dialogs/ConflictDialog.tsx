import {
  Alert,
  Badge,
  Box,
  Button,
  Code,
  HStack,
  RadioGroup,
  SimpleGrid,
  Spinner,
  Stack,
  Text,
} from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuGitMerge, LuRocket, LuTriangleAlert } from "react-icons/lu";

import { mergeSpecs } from "../../api/gen/sdk.gen";
import type { Conflict, ConflictInfo, DagSpec } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { useAccess } from "../../lib/access";
import { useBuilder } from "../../store/builder";
import { deploy } from "../actions";
import { DialogShell } from "../components/DialogShell";
import { JsonBlock } from "../components/JsonBlock";
import { useRetryToken } from "../lib/retryToken";
import { type Side, allResolved, applyResolutions } from "../lib/conflicts";
import { type DeployOptions, useLifecycle } from "../store";

/** Puts `spec` on the canvas as unsaved work based on the stored version `theirs`. */
function adoptMerged(spec: DagSpec, theirs: DagSpec, etag: string | null | undefined): void {
  const st = useBuilder.getState();
  const prevSaved = st.savedDagId;
  const pendingPrUrl = st.pendingPrUrl;
  st.load(spec, true, { etag: etag ?? null, pendingPrUrl });
  useBuilder.setState({ dirty: true, baseSpec: theirs, savedDagId: prevSaved ?? spec.dag_id });
}

function SideValue({ label, present, value }: { label: string; present: boolean | undefined; value: unknown }) {
  const { t } = useTranslation("lifecycle");
  return (
    <Box minW={0}>
      <Text fontSize="xs" fontWeight="semibold" color="fg.muted">
        {label}
      </Text>
      {present === false ? (
        <Text fontSize="xs" fontStyle="italic" color="fg.muted">
          {t("conflict.absent")}
        </Text>
      ) : (
        <JsonBlock value={value} maxH="160px" />
      )}
    </Box>
  );
}

function ConflictRow({
  conflict,
  index,
  choice,
  onChoose,
}: {
  conflict: Conflict;
  index: number;
  choice: Side | undefined;
  onChoose: (s: Side) => void;
}) {
  const { t } = useTranslation("lifecycle");
  return (
    <Box borderWidth="1px" rounded="md" p={3} borderColor={choice ? "border" : "orange.400"}>
      <HStack justify="space-between" wrap="wrap" mb={2}>
        <Stack gap={0}>
          <Code fontSize="xs">{conflict.path || t("conflict.wholeDocument")}</Code>
          <Text fontSize="xs" color="fg.muted">
            {conflict.reason}
          </Text>
        </Stack>
        {conflict.path ? (
          <RadioGroup.Root
            size="sm"
            value={choice ?? null}
            onValueChange={(e) => e.value && onChoose(e.value as Side)}
            aria-label={t("conflict.chooseFor", { path: conflict.path })}
          >
            <HStack gap={4}>
              {(["ours", "theirs"] as const).map((side) => (
                <RadioGroup.Item key={side} value={side}>
                  <RadioGroup.ItemHiddenInput />
                  <RadioGroup.ItemIndicator />
                  <RadioGroup.ItemText>{t(`conflict.${side}`)}</RadioGroup.ItemText>
                </RadioGroup.Item>
              ))}
            </HStack>
          </RadioGroup.Root>
        ) : null}
      </HStack>
      <SimpleGrid columns={{ base: 1, md: 3 }} gap={2} data-index={index}>
        <SideValue label={t("conflict.base")} present={conflict.base_present} value={conflict.base} />
        <SideValue label={t("conflict.ours")} present={conflict.ours_present} value={conflict.ours} />
        <SideValue label={t("conflict.theirs")} present={conflict.theirs_present} value={conflict.theirs} />
      </SimpleGrid>
    </Box>
  );
}

function EjectedBody({ conflict, file, request }: { conflict: ConflictInfo; file: string; request: DeployOptions }) {
  const { t } = useTranslation("lifecycle");
  // The server says whether a forced deploy would be accepted for this user; the switch
  // (preserve.allow_force_overwrite) and the permission are checked here again.
  const access = useAccess();
  const canForce = conflict.force_allowed === true && access.forceOverwrite;
  const handWritten = conflict.hand_written === true;
  const close = useLifecycle((s) => s.closeDialog);
  const [busy, setBusy] = useState(false);
  const retry = useRetryToken();
  return (
    <DialogShell
      title={handWritten ? t("conflict.handWrittenTitle") : t("conflict.ejectedTitle")}
      size="md"
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={close}>
            {canForce ? t("common.cancel") : t("common.close")}
          </Button>
          {canForce ? (
            <Button
              size="sm"
              colorPalette="red"
              loading={busy}
              data-testid="conflict-force"
              onClick={async () => {
                const secret = retry.take();
                if (!secret) return;
                setBusy(true);
                close();
                await deploy({ ...request, force: true }, secret);
                setBusy(false);
              }}
            >
              {t("conflict.force")}
            </Button>
          ) : null}
        </>
      }
    >
      <Alert.Root status="warning">
        <Alert.Indicator />
        <Alert.Content>
          <Alert.Description fontSize="sm">
            {canForce
              ? handWritten
                ? t("conflict.handWrittenHelp")
                : t("conflict.ejectedHelp")
              : t("conflict.forceOff")}
          </Alert.Description>
        </Alert.Content>
      </Alert.Root>
      <Code mt={3} display="block" p={2} fontSize="xs">
        {file}
      </Code>
      {canForce ? <Box mt={3}>{retry.field}</Box> : null}
    </DialogShell>
  );
}

function MergeBody({ conflict, request, ours }: { conflict: ConflictInfo; request: DeployOptions; ours: DagSpec }) {
  const { t } = useTranslation("lifecycle");
  const close = useLifecycle((s) => s.closeDialog);
  const baseSpec = useBuilder((s) => s.baseSpec);
  const theirs = conflict.current_spec as DagSpec;
  const [choices, setChoices] = useState<Record<number, Side>>({});
  const [busy, setBusy] = useState(false);
  const retry = useRetryToken();

  const merge = useQuery({
    queryKey: ["lifecycle", "merge", conflict.current_etag, ours, baseSpec],
    queryFn: async ({ signal }) => {
      // A brand-new document has no common ancestor: merge from an empty DAG.
      const base: DagSpec = baseSpec ?? { dag_id: ours.dag_id, nodes: [], edges: [] };
      const { data } = await mergeSpecs({ body: { base, ours, theirs }, signal, throwOnError: true });
      return data;
    },
    staleTime: Infinity,
    retry: false,
  });

  const redeploy = async (spec: DagSpec) => {
    const secret = retry.take();
    if (!secret) return;
    setBusy(true);
    adoptMerged(spec, theirs, conflict.current_etag);
    close();
    await deploy({ ...request, spec: undefined, baseEtag: conflict.current_etag ?? null }, secret);
    setBusy(false);
  };

  const conflicts = merge.data?.conflicts ?? [];
  const resolved = allResolved(conflicts, choices);
  const unresolvable = conflicts.some((c) => !c.path);
  const setAll = (side: Side) =>
    setChoices(Object.fromEntries(conflicts.flatMap((c, i) => (c.path ? [[i, side] as const] : []))));

  let body;
  let footer;
  if (merge.isPending) {
    body = (
      <HStack>
        <Spinner size="sm" /> <Text fontSize="sm">{t("conflict.merging")}</Text>
      </HStack>
    );
  } else if (merge.isError) {
    body = <Text color="red.500">{errorMessage(merge.error)}</Text>;
  } else if (merge.data?.merged && !conflicts.length) {
    const merged = merge.data.merged;
    body = (
      <Alert.Root status="success">
        <Alert.Indicator />
        <Alert.Content>
          <Alert.Title>{t("conflict.cleanTitle")}</Alert.Title>
          <Alert.Description fontSize="sm">{t("conflict.cleanHelp")}</Alert.Description>
        </Alert.Content>
      </Alert.Root>
    );
    footer = (
      <>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            adoptMerged(merged, theirs, conflict.current_etag);
            close();
          }}
        >
          {t("conflict.loadOnly")}
        </Button>
        <Button size="sm" colorPalette="blue" loading={busy} onClick={() => void redeploy(merged)}>
          <LuGitMerge /> {t("conflict.applyRedeploy")}
        </Button>
      </>
    );
  } else {
    body = (
      <Stack gap={3}>
        <HStack wrap="wrap" gap={2}>
          <LuTriangleAlert color="var(--chakra-colors-orange-500)" />
          <Text fontSize="sm" flex="1">
            {t("conflict.count", { count: conflicts.length })}
          </Text>
          <Button size="xs" variant="outline" onClick={() => setAll("ours")}>
            {t("conflict.allOurs")}
          </Button>
          <Button size="xs" variant="outline" onClick={() => setAll("theirs")}>
            {t("conflict.allTheirs")}
          </Button>
        </HStack>
        {conflicts.map((c, i) => (
          <ConflictRow
            key={`${c.path}-${i}`}
            conflict={c}
            index={i}
            choice={choices[i]}
            onChoose={(side) => setChoices((prev) => ({ ...prev, [i]: side }))}
          />
        ))}
        {unresolvable ? (
          <Text fontSize="xs" color="orange.500">
            {t("conflict.invalidCombined")}
          </Text>
        ) : null}
      </Stack>
    );
    footer = (
      <>
        <Badge colorPalette={resolved ? "green" : "orange"}>
          {t("conflict.resolved", {
            done: Object.keys(choices).length,
            total: conflicts.filter((c) => c.path).length,
          })}
        </Badge>
        <Button
          size="sm"
          colorPalette="blue"
          disabled={!resolved || !merge.data?.draft}
          loading={busy}
          onClick={() => {
            const finalSpec = applyResolutions(merge.data?.draft ?? {}, conflicts, choices) as DagSpec;
            void redeploy(finalSpec);
          }}
        >
          <LuRocket /> {t("conflict.resolveRedeploy")}
        </Button>
      </>
    );
  }

  return (
    <DialogShell
      title={t("conflict.title")}
      description={t("conflict.description", { version: conflict.current_version ?? conflict.current_etag ?? "?" })}
      size="xl"
      footer={
        <HStack w="100%" justify="space-between" wrap="wrap">
          <Button
            size="sm"
            variant="outline"
            colorPalette="red"
            disabled={busy}
            title={t("conflict.keepMineHelp")}
            onClick={async () => {
              const secret = retry.take();
              if (!secret) return;
              close();
              await deploy({ ...request, baseEtag: conflict.current_etag ?? null }, secret);
            }}
          >
            {t("conflict.keepMine")}
          </Button>
          <HStack>{footer}</HStack>
        </HStack>
      }
    >
      {body}
      <Box mt={3}>{retry.field}</Box>
    </DialogShell>
  );
}

/** Deploy conflict: three-way merge with the stored version, or a hand-edited file. */
export default function ConflictDialog({
  conflict,
  request,
  spec,
}: {
  conflict: ConflictInfo;
  request: DeployOptions;
  spec: DagSpec;
}) {
  if (conflict.ejected_file && !conflict.current_spec)
    return <EjectedBody conflict={conflict} file={conflict.ejected_file} request={request} />;
  return <MergeBody conflict={conflict} request={request} ours={spec} />;
}
