import {
  Badge,
  Box,
  Button,
  Card,
  Field,
  HStack,
  Input,
  InputGroup,
  SegmentGroup,
  SimpleGrid,
  Spinner,
  Stack,
  Text,
} from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCopy, LuLayoutTemplate, LuPackagePlus, LuPencil, LuSearch, LuTrash2 } from "react-icons/lu";
import { useAccess } from "../../lib/access";

import {
  deleteTemplateMutation,
  getTemplateOptions,
  listTemplatesOptions,
  listTemplatesQueryKey,
} from "../../api/gen/@tanstack/react-query.gen";
import type { DagSpec, Template, TemplateSummary } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { insertOperatorTemplate } from "../../canvas/actions";
import { useBuilder } from "../../store/builder";
import { toaster } from "../../ui/toaster";
import { canInsertFragment, confirmDiscard, insertFragment } from "../actions";
import { usesInDag } from "../lib/operatorTemplateEditor";
import { DialogShell } from "../components/DialogShell";
import { MarketplacePanel } from "../components/MarketplacePanel";
import { MiniGraph } from "../components/MiniGraph";
import { syncDeepLink } from "../lib/deepLink";
import { fitViewSoon } from "../runtime";
import { useLifecycle } from "../store";

type KindFilter = "all" | "dag" | "fragment" | "operator";

const KIND_PALETTE: Record<Template["kind"], string> = { dag: "blue", fragment: "purple", operator: "teal" };
const DAG_ID = /^[A-Za-z0-9_.-]+$/;

function matches(tpl: TemplateSummary, q: string, kind: KindFilter): boolean {
  if (kind !== "all" && tpl.kind !== kind) return false;
  if (!q) return true;
  const hay = [tpl.id, tpl.title, tpl.description ?? "", tpl.category ?? "", ...(tpl.tags ?? [])]
    .join(" ")
    .toLowerCase();
  return q
    .toLowerCase()
    .split(/\s+/)
    .every((w) => hay.includes(w));
}

function Preview({ id, onUsed }: { id: string; onUsed: () => void }) {
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const canWrite = useAccess().can("template:write");
  const usedHere = useBuilder((s) => usesInDag(s.nodes, id));
  const openEditor = (mode: "edit" | "duplicate") =>
    useLifecycle.getState().openDialog({ kind: "operatorTemplateEditor", mode, templateId: id });
  const tpl = useQuery(getTemplateOptions({ path: { template_id: id } }));
  const [dagId, setDagId] = useState<string | null>(null);
  const remove = useMutation({
    ...deleteTemplateMutation(),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: listTemplatesQueryKey() });
      toaster.create({ type: "info", title: t("templates.deleted") });
    },
    onError: (e) => toaster.create({ type: "error", title: t("templates.deleteFailed"), description: errorMessage(e) }),
  });
  if (tpl.isPending) return <Spinner size="sm" />;
  if (tpl.isError) return <Text color="red.500">{errorMessage(tpl.error)}</Text>;
  const data: Template = tpl.data;
  const previewSpec: DagSpec | null =
    data.kind === "dag"
      ? (data.spec ?? null)
      : data.fragment
        ? { dag_id: "fragment", nodes: data.fragment.nodes, edges: data.fragment.edges ?? [] }
        : data.blueprint
          ? { dag_id: "operator", nodes: [data.blueprint.node], edges: [] }
          : null;
  // Fragments and operator templates are inserted into the open DAG (operator templates the
  // same way as from the palette: required inputs are asked in a form first).
  const inserts = data.kind !== "dag";
  const canInsert = data.kind === "operator" ? !!data.blueprint : canInsertFragment();
  const targetId = dagId ?? data.spec?.dag_id ?? "";

  const use = () => {
    if (data.kind === "dag" && data.spec) {
      if (!confirmDiscard()) return;
      useBuilder.getState().load({ ...data.spec, dag_id: targetId }, false);
      useBuilder.setState({ dirty: true });
      useLifecycle.getState().setSelectedRunId(null);
      syncDeepLink(null);
      fitViewSoon();
      onUsed();
    } else if (data.kind === "operator" && data.blueprint) {
      // Close the gallery first: the required-inputs form (if any) is the next dialog.
      onUsed();
      insertOperatorTemplate(data);
    } else if (data.fragment && insertFragment(data.fragment)) {
      onUsed();
    }
  };

  return (
    <Stack gap={3}>
      <HStack wrap="wrap">
        <Text fontWeight="semibold">{data.title}</Text>
        <Badge colorPalette={KIND_PALETTE[data.kind]}>{t(`templates.kind.${data.kind}`)}</Badge>
        {data.builtin ? <Badge variant="outline">{t("templates.builtin")}</Badge> : null}
      </HStack>
      {data.description ? (
        <Text fontSize="sm" color="fg.muted">
          {data.description}
        </Text>
      ) : null}
      {data.requires?.length ? (
        <Text fontSize="xs" color="fg.muted">
          {t("templates.requires", { list: data.requires.join(", ") })}
        </Text>
      ) : null}
      {data.blueprint?.locked?.length ? (
        <Text fontSize="xs" color="fg.muted">
          {t("templates.blueprintLocked", { list: data.blueprint.locked.join(", ") })}
        </Text>
      ) : null}
      {data.blueprint?.required?.length ? (
        <Text fontSize="xs" color="fg.muted">
          {t("templates.blueprintRequired", { list: data.blueprint.required.join(", ") })}
        </Text>
      ) : null}
      {previewSpec ? (
        <MiniGraph
          spec={previewSpec}
          label={t("templates.previewLabel", { title: data.title })}
          {...(data.kind === "operator" ? { height: "140px" } : {})}
        />
      ) : null}
      {data.kind === "dag" ? (
        <Field.Root invalid={!DAG_ID.test(targetId)}>
          <Field.Label>{t("templates.newDagId")}</Field.Label>
          <Input size="sm" fontFamily="mono" value={targetId} onChange={(e) => setDagId(e.target.value)} />
          <Field.ErrorText>{t("templates.invalidDagId")}</Field.ErrorText>
        </Field.Root>
      ) : null}
      <HStack>
        <Button
          size="sm"
          colorPalette="blue"
          onClick={use}
          disabled={(data.kind === "dag" && !DAG_ID.test(targetId)) || (inserts && !canInsert)}
          title={
            inserts && !canInsert
              ? t("templates.insertUnavailable")
              : data.kind === "operator"
                ? t("templates.operatorInsertHint")
                : undefined
          }
        >
          <LuLayoutTemplate /> {inserts ? t("templates.insert") : t("templates.useDag")}
        </Button>
        {data.kind === "operator" && data.blueprint && canWrite ? (
          <>
            {!data.builtin ? (
              <Button size="sm" variant="outline" data-testid="template-edit" onClick={() => openEditor("edit")}>
                <LuPencil /> {t("templates.edit")}
              </Button>
            ) : null}
            <Button size="sm" variant="ghost" onClick={() => openEditor("duplicate")}>
              <LuCopy /> {t("templates.duplicate")}
            </Button>
          </>
        ) : null}
        {!data.builtin ? (
          <Button
            size="sm"
            variant="ghost"
            colorPalette="red"
            loading={remove.isPending}
            onClick={() =>
              window.confirm(
                [
                  t("templates.confirmDelete", { title: data.title }),
                  ...(data.kind === "operator" ? [t("templates.confirmDeleteOperator")] : []),
                  ...(usedHere ? [t("templates.confirmDeleteUsed", { count: usedHere })] : []),
                ].join("\n\n"),
              ) &&
              remove.mutate({ path: { template_id: data.id }, headers: data.etag ? { "if-match": data.etag } : {} })
            }
          >
            <LuTrash2 /> {t("common.delete")}
          </Button>
        ) : null}
      </HStack>
    </Stack>
  );
}

/** Template gallery: DAG starters and reusable fragments. */
export default function TemplatesDialog({ filter }: { filter?: "dag" | "fragment" | "operator" }) {
  const access = useAccess();
  const { t } = useTranslation("lifecycle");
  const list = useQuery(listTemplatesOptions());
  const close = useLifecycle((s) => s.closeDialog);
  const openDialog = useLifecycle((s) => s.openDialog);
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<KindFilter>(filter ?? "all");
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState<"gallery" | "marketplace">("gallery");
  const groups = useMemo(() => {
    const out = new Map<string, TemplateSummary[]>();
    for (const tpl of list.data ?? []) {
      if (!matches(tpl, q, kind)) continue;
      const cat = tpl.category || t("templates.uncategorized");
      out.set(cat, [...(out.get(cat) ?? []), tpl]);
    }
    return [...out.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [list.data, q, kind, t]);

  return (
    <DialogShell
      title={t("templates.title")}
      description={t("templates.description")}
      size="cover"
      footer={
        <HStack w="100%" justify="space-between">
          <HStack>
            <Button size="sm" variant="outline" onClick={() => openDialog({ kind: "saveTemplate", mode: "dag" })}>
              {t("menu.saveTemplate")}
            </Button>
            <Button size="sm" variant="outline" onClick={() => openDialog({ kind: "saveTemplate", mode: "fragment" })}>
              {t("menu.saveFragment")}
            </Button>
            {access.allows("templates", "template:write") ? (
              <Button
                size="sm"
                variant="outline"
                data-testid="templates-new-operator"
                onClick={() => openDialog({ kind: "operatorTemplateEditor", mode: "new" })}
              >
                <LuPackagePlus /> {t("templates.newOperator")}
              </Button>
            ) : null}
          </HStack>
          <Button size="sm" variant="ghost" onClick={close}>
            {t("common.close")}
          </Button>
        </HStack>
      }
    >
      <SegmentGroup.Root
        size="sm"
        mb={3}
        value={view}
        onValueChange={(e) => setView(e.value === "marketplace" ? "marketplace" : "gallery")}
      >
        <SegmentGroup.Indicator />
        <SegmentGroup.Items
          items={[
            { value: "gallery", label: t("templates.gallery") },
            ...(access.feature("marketplace") ? [{ value: "marketplace", label: t("marketplace.title") }] : []),
          ]}
        />
      </SegmentGroup.Root>
      {view === "marketplace" ? (
        <MarketplacePanel
          onInstalled={(id) => {
            setView("gallery");
            setSelected(id);
          }}
        />
      ) : (
        <Stack direction={{ base: "column", lg: "row" }} gap={4} align="stretch" h="100%">
          <Stack flex="3" gap={3} minW={0}>
            <HStack wrap="wrap">
              <InputGroup startElement={<LuSearch />} flex="1" minW="200px">
                <Input
                  size="sm"
                  autoFocus
                  value={q}
                  placeholder={t("templates.search")}
                  aria-label={t("templates.search")}
                  onChange={(e) => setQ(e.target.value)}
                />
              </InputGroup>
              <SegmentGroup.Root
                size="sm"
                value={kind}
                onValueChange={(e) => setKind((e.value as KindFilter) ?? "all")}
              >
                <SegmentGroup.Indicator />
                <SegmentGroup.Items
                  items={[
                    { value: "all", label: t("templates.kind.all") },
                    { value: "dag", label: t("templates.kind.dag") },
                    { value: "fragment", label: t("templates.kind.fragment") },
                    { value: "operator", label: t("templates.kind.operator") },
                  ]}
                />
              </SegmentGroup.Root>
            </HStack>
            {list.isPending ? <Spinner size="sm" /> : null}
            {list.isError ? <Text color="red.500">{errorMessage(list.error)}</Text> : null}
            {list.data && !groups.length ? (
              <Text fontSize="sm" color="fg.muted">
                {t("templates.empty")}
              </Text>
            ) : null}
            {groups.map(([cat, items]) => (
              <Box key={cat} as="section" aria-label={cat}>
                <Text fontSize="xs" fontWeight="bold" textTransform="uppercase" color="fg.muted" mb={2}>
                  {cat}
                </Text>
                <SimpleGrid columns={{ base: 1, md: 2, xl: 3 }} gap={2}>
                  {items.map((tpl) => (
                    <Card.Root
                      key={tpl.id}
                      size="sm"
                      variant={selected === tpl.id ? "subtle" : "outline"}
                      borderColor={selected === tpl.id ? "blue.500" : undefined}
                      cursor="pointer"
                      tabIndex={0}
                      role="button"
                      aria-pressed={selected === tpl.id}
                      onClick={() => setSelected(tpl.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setSelected(tpl.id);
                        }
                      }}
                    >
                      <Card.Body gap={1}>
                        <HStack>
                          <Text fontWeight="semibold" fontSize="sm" flex="1" truncate>
                            {tpl.title}
                          </Text>
                          <Badge size="xs" colorPalette={KIND_PALETTE[tpl.kind]}>
                            {t(`templates.kind.${tpl.kind}`)}
                          </Badge>
                        </HStack>
                        {tpl.description ? (
                          <Text fontSize="xs" color="fg.muted" lineClamp={2}>
                            {tpl.description}
                          </Text>
                        ) : null}
                        <HStack gap={1} wrap="wrap">
                          {!tpl.builtin ? (
                            <Badge size="xs" variant="outline" colorPalette="green">
                              {t("templates.custom")}
                            </Badge>
                          ) : null}
                          {(tpl.tags ?? []).map((tag) => (
                            <Badge key={tag} size="xs" variant="outline">
                              {tag}
                            </Badge>
                          ))}
                        </HStack>
                      </Card.Body>
                    </Card.Root>
                  ))}
                </SimpleGrid>
              </Box>
            ))}
          </Stack>
          <Box flex="2" minW={0} borderLeftWidth={{ lg: "1px" }} pl={{ lg: 4 }}>
            {selected ? (
              <Preview key={selected} id={selected} onUsed={close} />
            ) : (
              <Text fontSize="sm" color="fg.muted">
                {t("templates.selectHint")}
              </Text>
            )}
          </Box>
        </Stack>
      )}
    </DialogShell>
  );
}
