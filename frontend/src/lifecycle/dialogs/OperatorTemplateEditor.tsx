import {
  Alert,
  Badge,
  Box,
  Button,
  Field,
  HStack,
  Input,
  InputGroup,
  SegmentGroup,
  Spinner,
  Stack,
  Table,
  Tabs,
  Text,
  Textarea,
} from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  LuCircleAlert,
  LuCircleCheck,
  LuCopy,
  LuDownload,
  LuHistory,
  LuInfo,
  LuPackageCheck,
  LuRefreshCw,
  LuSave,
  LuSearch,
  LuTrash2,
  LuTriangleAlert,
  LuUpload,
} from "react-icons/lu";

import {
  deleteTemplateMutation,
  getTemplateOptions,
  getTemplateQueryKey,
  getTemplateVersionOptions,
  listTemplateVersionsOptions,
  listTemplateVersionsQueryKey,
  listTemplatesOptions,
  listTemplatesQueryKey,
  saveTemplateMutation,
} from "../../api/gen/@tanstack/react-query.gen";
import { generateCode, validateSpec } from "../../api/gen/sdk.gen";
import type { Problem, Template } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { IconImage } from "../../canvas/icons";
import { type InsertItem, operatorGroups, useInsertables } from "../../canvas/insertables";
import type { FieldMode } from "../../canvas/operatorTemplates";
import { matchesQuery, rankItems, searchTerms } from "../../canvas/search";
import type { JsonSchema } from "../../lib/formSchema";
import { problemText } from "../../lib/problemMessages";
import type { OperatorSpec, TaskSpec } from "../../lib/types";
import { ExecutionFields } from "../../panels/inspector/ExecutionTab";
import { IconField } from "../../panels/inspector/IconPicker";
import { OperatorParamsForm, useOperatorDetail } from "../../panels/inspector/OperatorParams";
import { useBuilder } from "../../store/builder";
import { toaster } from "../../ui/toaster";
import { focusField, JsonField, TextField } from "../../widgets/fields";
import { DialogShell } from "../components/DialogShell";
import { CodeView, DiffView } from "../components/lazy";
import { useMeta } from "../hooks";
import {
  BLUEPRINT_NODE_ID,
  type ConflictInfo,
  type EditorMode,
  type EditorState,
  addableFields,
  conflictInfo,
  diffText,
  effectiveId,
  emptyState,
  exportJson,
  fieldErrors,
  fieldRows,
  metaErrors,
  parseImport,
  previewSpec,
  problemTab,
  stateFromTemplate,
  templateProblems,
  toTemplateInput,
  usesInDag,
  valuePreview,
} from "../lib/operatorTemplateEditor";
import { useLifecycle } from "../store";

const MODES: FieldMode[] = ["default", "locked", "required"];
const MODE_PALETTE: Record<FieldMode, string> = { default: "gray", locked: "orange", required: "blue" };
/** Operators rendered by the picker at once (the catalog can hold >1200). */
const PICKER_LIMIT = 150;

type Tab = "params" | "settings" | "modes" | "code" | "history";

type Checks = { problems: Problem[]; code: string | null; checking: boolean; error: string | null };

/**
 * Debounced validation and code preview of the template's task, in a throwaway one-task
 * DAG through the regular `/validate` and `/codegen` endpoints (nothing is stored).
 */
function useTemplateChecks(spec: TaskSpec | null, delay = 500): Checks {
  const [state, setState] = useState<Checks>({ problems: [], code: null, checking: false, error: null });
  const body = useMemo(() => previewSpec(spec), [spec]);
  useEffect(() => {
    if (!body) return;
    const ctrl = new AbortController();
    const timer = setTimeout(() => {
      setState((s) => ({ ...s, checking: true }));
      void Promise.all([
        validateSpec({ body, query: { import_check: false }, signal: ctrl.signal, throwOnError: true }),
        generateCode({ body, signal: ctrl.signal, throwOnError: true }),
      ])
        .then(([report, code]) => {
          if (ctrl.signal.aborted) return;
          setState({
            problems: [...report.data.problems, ...code.data.problems],
            code: code.data.code ?? null,
            checking: false,
            error: null,
          });
        })
        .catch((err: unknown) => {
          if (ctrl.signal.aborted) return;
          setState({ problems: [], code: null, checking: false, error: errorMessage(err) });
        });
    }, delay);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [body, delay]);
  return body ? state : { problems: [], code: null, checking: false, error: null };
}

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** Catalog operator picker: search, provider/catalog groups, icons; unsupported ones disabled. */
function OperatorPicker({ onPick, current }: { onPick: (item: InsertItem) => void; current?: string | undefined }) {
  const { t } = useTranslation("lifecycle");
  const ins = useInsertables();
  const [query, setQuery] = useState("");
  const groups = useMemo(() => {
    const terms = searchTerms(query);
    const items = terms.length
      ? rankItems(ins.operators, query, PICKER_LIMIT)
      : ins.operators.filter((i) => matchesQuery(i, terms)).slice(0, PICKER_LIMIT);
    return operatorGroups(items);
  }, [ins.operators, query]);
  const total = groups.reduce((n, g) => n + g.items.length, 0);
  return (
    <Stack gap={2} data-testid="optpl-operator-picker">
      <InputGroup startElement={<LuSearch />}>
        <Input
          size="sm"
          autoFocus
          value={query}
          placeholder={t("operatorTemplateEditor.pickerSearch")}
          aria-label={t("operatorTemplateEditor.pickerSearch")}
          onChange={(e) => setQuery(e.target.value)}
        />
      </InputGroup>
      <Box
        maxH="320px"
        overflowY="auto"
        borderWidth="1px"
        rounded="md"
        p={1}
        role="listbox"
        aria-label={t("operatorTemplateEditor.operator")}
      >
        {ins.isPending ? <Spinner size="sm" m={2} /> : null}
        {ins.operatorsQuery.isError ? (
          <Text fontSize="xs" color="fg.error" p={2}>
            {errorMessage(ins.operatorsQuery.error)}
          </Text>
        ) : null}
        {!ins.isPending && !total ? (
          <Text fontSize="xs" color="fg.muted" p={2}>
            {t("operatorTemplateEditor.pickerEmpty")}
          </Text>
        ) : null}
        {groups.map((g) => (
          <Box key={g.id} mb={1}>
            <Text px={2} py={1} fontSize="xs" fontWeight="bold" textTransform="uppercase" color="fg.muted">
              {g.title}
            </Text>
            {g.items.map((item) => {
              const disabled = item.disabledReason != null;
              const Icon = item.icon;
              const fqn = item.key.slice("op:".length);
              return (
                <HStack
                  key={item.key}
                  role="option"
                  aria-selected={current === fqn}
                  aria-disabled={disabled || undefined}
                  data-fqn={fqn}
                  tabIndex={disabled ? -1 : 0}
                  px={2}
                  py={1}
                  gap={2}
                  rounded="sm"
                  cursor={disabled ? "not-allowed" : "pointer"}
                  opacity={disabled ? 0.5 : 1}
                  bg={current === fqn ? "bg.emphasized" : undefined}
                  _hover={{ bg: "bg.muted" }}
                  title={disabled ? (item.disabledReason ?? "") : item.hint}
                  onClick={() => !disabled && onPick(item)}
                  onKeyDown={(e) => {
                    if (!disabled && (e.key === "Enter" || e.key === " ")) {
                      e.preventDefault();
                      onPick(item);
                    }
                  }}
                >
                  <Box color="fg.muted" flexShrink={0} aria-hidden>
                    <IconImage id={item.iconId} size={16} fallback={<Icon size={14} />} />
                  </Box>
                  <Stack gap={0} minW={0} flex="1">
                    <Text fontSize="sm" truncate>
                      {item.label}
                    </Text>
                    <Text fontSize="xs" color={disabled ? "fg.warning" : "fg.muted"} truncate>
                      {disabled ? item.disabledReason : item.hint}
                    </Text>
                  </Stack>
                </HStack>
              );
            })}
          </Box>
        ))}
      </Box>
      {total >= PICKER_LIMIT ? (
        <Text fontSize="xs" color="fg.muted">
          {t("operatorTemplateEditor.pickerMore", { count: PICKER_LIMIT })}
        </Text>
      ) : null}
    </Stack>
  );
}

/** Field modes: one row per configured (or added) field with a Default / Locked / Required switch. */
function ModesTable({
  spec,
  modes,
  paramNames,
  onChange,
}: {
  spec: TaskSpec;
  modes: Record<string, FieldMode>;
  paramNames: string[];
  onChange: (path: string, mode: FieldMode) => void;
}) {
  const { t } = useTranslation("lifecycle");
  const rows = fieldRows(spec, modes);
  const addable = addableFields(spec, paramNames, rows);
  return (
    <Stack gap={2}>
      <Text fontSize="xs" color="fg.muted">
        {t("operatorTemplates.fieldsHelp")}
      </Text>
      {rows.length ? (
        <Box borderWidth="1px" rounded="md" overflowX="auto">
          <Table.Root size="sm" aria-label={t("operatorTemplates.fields")}>
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeader>{t("operatorTemplateEditor.field")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("operatorTemplateEditor.value")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("operatorTemplateEditor.mode")}</Table.ColumnHeader>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {rows.map((path) => {
                const mode = modes[path] ?? "default";
                const value = valuePreview(spec, path);
                return (
                  <Table.Row key={path} data-path={path}>
                    <Table.Cell fontFamily="mono" fontSize="xs" whiteSpace="nowrap">
                      {path}
                    </Table.Cell>
                    <Table.Cell fontSize="xs" color="fg.muted" maxW="260px">
                      {mode === "required" ? (
                        <Text color="fg.info">{t("operatorTemplateEditor.askedOnInsert")}</Text>
                      ) : value === null ? (
                        <Text color={mode === "locked" ? "fg.warning" : "fg.subtle"}>
                          {mode === "locked" ? t("operatorTemplateEditor.lockedEmpty") : "—"}
                        </Text>
                      ) : (
                        <Text fontFamily="mono" truncate title={value}>
                          {value}
                        </Text>
                      )}
                    </Table.Cell>
                    <Table.Cell w="260px">
                      <SegmentGroup.Root
                        size="xs"
                        value={mode}
                        aria-label={t("operatorTemplates.modeOf", { field: path })}
                        onValueChange={(e) => onChange(path, (e.value as FieldMode | null) ?? "default")}
                      >
                        <SegmentGroup.Indicator />
                        <SegmentGroup.Items
                          items={MODES.map((m) => ({ value: m, label: t(`operatorTemplates.modes.${m}`) }))}
                        />
                      </SegmentGroup.Root>
                    </Table.Cell>
                  </Table.Row>
                );
              })}
            </Table.Body>
          </Table.Root>
        </Box>
      ) : (
        <Badge alignSelf="start">{t("operatorTemplates.noFields")}</Badge>
      )}
      {addable.length ? (
        <HStack gap={2} wrap="wrap">
          <Text fontSize="xs" color="fg.muted">
            {t("operatorTemplateEditor.addField")}
          </Text>
          <select
            aria-label={t("operatorTemplateEditor.addField")}
            value=""
            style={{ fontSize: "12px", maxWidth: "260px", background: "transparent", color: "inherit" }}
            onChange={(e) => e.target.value && onChange(e.target.value, "required")}
          >
            <option value="">{t("operatorTemplateEditor.addFieldPlaceholder")}</option>
            {addable.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </HStack>
      ) : null}
    </Stack>
  );
}

function ProblemsPanel({
  problems,
  checks,
  onOpen,
}: {
  problems: Problem[];
  checks: Checks;
  onOpen: (p: Problem) => void;
}) {
  const { t } = useTranslation("lifecycle");
  const errors = problems.filter((p) => p.severity === "error").length;
  return (
    <Stack gap={2} data-testid="optpl-problems">
      <HStack>
        <Text fontSize="sm" fontWeight="semibold" flex="1">
          {t("operatorTemplateEditor.validation")}
        </Text>
        {checks.checking ? <Spinner size="xs" /> : null}
        {!checks.checking && !checks.error ? (
          errors ? (
            <Badge colorPalette="red">{t("operatorTemplateEditor.errors", { count: errors })}</Badge>
          ) : (
            <Badge colorPalette="green">
              <LuCircleCheck /> {t("operatorTemplateEditor.valid")}
            </Badge>
          )
        ) : null}
      </HStack>
      {checks.error ? (
        <Text fontSize="xs" color="fg.error">
          {t("operatorTemplateEditor.checkFailed", { message: checks.error })}
        </Text>
      ) : null}
      {problems.map((p, i) => (
        <HStack
          key={`${p.code}-${p.field ?? ""}-${i}`}
          as="button"
          align="start"
          textAlign="start"
          gap={2}
          p={1.5}
          rounded="sm"
          _hover={{ bg: "bg.muted" }}
          onClick={() => onOpen(p)}
        >
          <Box
            pt={0.5}
            color={p.severity === "error" ? "fg.error" : p.severity === "warning" ? "fg.warning" : "fg.info"}
          >
            {p.severity === "error" ? <LuCircleAlert /> : p.severity === "warning" ? <LuTriangleAlert /> : <LuInfo />}
          </Box>
          <Stack gap={0} minW={0}>
            <Text fontSize="xs">{problemText(p)}</Text>
            {p.field ? (
              <Text fontSize="xs" fontFamily="mono" color="fg.muted">
                {p.field}
              </Text>
            ) : null}
          </Stack>
        </HStack>
      ))}
    </Stack>
  );
}

function VersionHistory({ id, draft, onRestore }: { id: string; draft: string; onRestore: (tpl: Template) => void }) {
  const { t } = useTranslation("lifecycle");
  const versions = useQuery(listTemplateVersionsOptions({ path: { template_id: id } }));
  const [selected, setSelected] = useState<string | null>(null);
  const older = (versions.data ?? []).filter((v) => !v.current);
  const pick = selected ?? older[0]?.version ?? null;
  const version = useQuery({
    ...getTemplateVersionOptions({ path: { template_id: id, version: pick ?? "" } }),
    enabled: !!pick,
  });
  if (versions.isPending) return <Spinner size="sm" />;
  if (versions.isError) return <Text color="fg.error">{errorMessage(versions.error)}</Text>;
  return (
    <Stack gap={3} data-testid="optpl-history">
      <Box borderWidth="1px" rounded="md" overflowX="auto">
        <Table.Root size="sm" aria-label={t("operatorTemplateEditor.history")}>
          <Table.Body>
            {versions.data.map((v) => (
              <Table.Row
                key={v.version}
                data-version={v.version}
                cursor={v.current ? undefined : "pointer"}
                bg={pick === v.version ? "bg.emphasized" : undefined}
                onClick={() => !v.current && setSelected(v.version)}
              >
                <Table.Cell fontFamily="mono" fontSize="xs">
                  v{v.version}
                </Table.Cell>
                <Table.Cell fontSize="xs">{v.title}</Table.Cell>
                <Table.Cell fontSize="xs" color="fg.muted">
                  {[v.updated_by, v.updated_at ? new Date(v.updated_at).toLocaleString() : null]
                    .filter(Boolean)
                    .join(" · ")}
                </Table.Cell>
                <Table.Cell textAlign="end">
                  {v.current ? <Badge colorPalette="green">{t("operatorTemplateEditor.currentVersion")}</Badge> : null}
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
      </Box>
      {!older.length ? (
        <Text fontSize="xs" color="fg.muted">
          {t("operatorTemplateEditor.noOlderVersions")}
        </Text>
      ) : version.data ? (
        <Stack gap={1}>
          <HStack>
            <Text fontSize="xs" color="fg.muted" flex="1">
              {t("operatorTemplateEditor.diffLabel", { version: pick })}
            </Text>
            <Button size="xs" variant="outline" onClick={() => onRestore(version.data)}>
              <LuHistory /> {t("operatorTemplateEditor.restore", { version: pick })}
            </Button>
          </HStack>
          <DiffView
            before={diffText(version.data)}
            after={draft}
            language="json"
            height="320px"
            label={t("operatorTemplateEditor.diffLabel", { version: pick })}
          />
        </Stack>
      ) : version.isError ? (
        <Text color="fg.error">{errorMessage(version.error)}</Text>
      ) : (
        <Spinner size="sm" />
      )}
    </Stack>
  );
}

function EditorBody({ mode, base, initial }: { mode: EditorMode; base: Template | null; initial: EditorState }) {
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const close = useLifecycle((s) => s.closeDialog);
  const [state, setState] = useState<EditorState>(initial);
  const [etag, setEtag] = useState<string | null>(mode === "edit" ? (base?.etag ?? null) : null);
  const [conflict, setConflict] = useState<ConflictInfo | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [picking, setPicking] = useState(!initial.spec);
  const [tab, setTab] = useState<Tab>("params");
  const fileInput = useRef<HTMLInputElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const notice = useRef<HTMLDivElement>(null);
  // A conflict or a delete confirmation appears at the top: bring it into view.
  useEffect(() => {
    if (conflict || confirmDelete) notice.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [conflict, confirmDelete]);
  const list = useQuery({ ...listTemplatesOptions(), retry: false });

  const { meta, spec, modes } = state;
  const id = effectiveId(meta);
  const editing = mode === "edit";
  const checks = useTemplateChecks(spec);
  const problems = useMemo(() => templateProblems(checks.problems, modes), [checks.problems, modes]);
  const errors = useMemo(() => fieldErrors(problems, problemText), [problems]);
  const blocking = problems.filter((p) => p.severity === "error").length;
  const metaProblems = metaErrors(meta);
  const clash = !editing ? list.data?.find((tpl) => tpl.id === id) : undefined;
  const operator = spec?.kind === "operator" ? spec.operator : null;
  const detail = useOperatorDetail(operator ?? "");
  const paramNames = useMemo(
    () => Object.keys((detail.data?.params_schema as JsonSchema | undefined)?.properties ?? {}),
    [detail.data],
  );
  const input = useMemo(() => toTemplateInput(state), [state]);
  const draft = useMemo(() => (input ? diffText(input as unknown as Template) : ""), [input]);
  const counts = useMemo(() => {
    const c = { locked: 0, required: 0 };
    for (const m of Object.values(modes)) if (m !== "default") c[m] += 1;
    return c;
  }, [modes]);
  const usedHere = useBuilder((s) => (editing ? usesInDag(s.nodes, base?.id ?? "") : 0));

  const setMeta = (patch: Partial<EditorState["meta"]>) => setState((s) => ({ ...s, meta: { ...s.meta, ...patch } }));
  const setSpec = (next: TaskSpec) => setState((s) => ({ ...s, spec: next }));
  const setMode = (path: string, m: FieldMode) =>
    // "default" is kept (not deleted): a field added to the table stays listed.
    setState((s) => ({ ...s, modes: { ...s.modes, [path]: m } }));

  const refresh = async (templateId: string) => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: listTemplatesQueryKey() }),
      qc.invalidateQueries({ queryKey: getTemplateQueryKey({ path: { template_id: templateId } }) }),
      qc.invalidateQueries({ queryKey: listTemplateVersionsQueryKey({ path: { template_id: templateId } }) }),
    ]);
  };

  const save = useMutation({
    ...saveTemplateMutation(),
    onSuccess: async (tpl) => {
      await refresh(tpl.id);
      toaster.create({
        type: "success",
        title: t("operatorTemplates.saved", { title: tpl.title, version: tpl.version ?? "1" }),
      });
      close();
    },
    onError: (e) => {
      const info = conflictInfo(e);
      if (info) setConflict(info);
      else toaster.create({ type: "error", title: t("templates.saveFailed"), description: errorMessage(e) });
    },
  });
  const remove = useMutation({
    ...deleteTemplateMutation(),
    onSuccess: async (res) => {
      if (base) await refresh(base.id);
      toaster.create({
        type: "info",
        title: res.deleted ? t("templates.deleted") : t("operatorTemplateEditor.alreadyGone"),
      });
      close();
    },
    onError: (e) => {
      const info = conflictInfo(e);
      setConfirmDelete(false);
      if (info) setConflict(info);
      else toaster.create({ type: "error", title: t("templates.deleteFailed"), description: errorMessage(e) });
    },
  });

  const submit = (ifMatch: string | null) => {
    if (!input) return;
    setConflict(null);
    save.mutate({
      body: input,
      headers: ifMatch ? { "if-match": ifMatch } : { "if-none-match": "*" },
    });
  };

  const reload = async () => {
    const fresh = await qc.fetchQuery({ ...getTemplateOptions({ path: { template_id: id } }), staleTime: 0 });
    setState(stateFromTemplate(fresh, "edit"));
    setEtag(fresh.etag ?? null);
    setConflict(null);
  };

  const openProblem = (p: Problem) => {
    setTab(problemTab(p));
    const field = p.field;
    if (field) setTimeout(() => focusField(field, root.current ?? document), 50);
  };

  const pick = (item: InsertItem) => {
    const payload = item.payload();
    if (payload.kind !== "spec" || payload.spec.kind !== "operator") return;
    const picked = payload.spec as OperatorSpec;
    setState((s) => ({
      ...s,
      // Settings carry over to the new operator; parameters and their modes do not.
      spec: s.spec
        ? ({ ...s.spec, kind: "operator", operator: picked.operator, params: {} } as OperatorSpec)
        : {
            ...picked,
            task_id: (s.meta.title ? effectiveId(s.meta) : picked.task_id).replace(/-/g, "_") || picked.task_id,
          },
      modes: Object.fromEntries(Object.entries(s.modes).filter(([p]) => !p.startsWith("params."))),
      meta: { ...s.meta, icon: s.meta.icon ?? item.iconId ?? null },
    }));
    setPicking(false);
    setTab("params");
  };

  const onImport = async (file: File) => {
    const parsed = parseImport(await file.text());
    if (!parsed.ok) {
      toaster.create({ type: "error", title: t(`operatorTemplateEditor.importError.${parsed.error}`) });
      return;
    }
    // Editing: the imported content becomes the next version of this template.
    const next = editing
      ? { ...parsed.state, meta: { ...parsed.state.meta, id: meta.id, idTouched: true } }
      : parsed.state;
    setState(next);
    setPicking(!next.spec);
    toaster.create({ type: "info", title: t("operatorTemplateEditor.imported") });
  };

  const title =
    mode === "edit"
      ? t("operatorTemplateEditor.editTitle", { title: base?.title ?? id })
      : mode === "duplicate"
        ? t("operatorTemplateEditor.duplicateTitle", { title: base?.title ?? "" })
        : t("operatorTemplateEditor.newTitle");
  const canSave =
    !!input && !metaProblems.length && !blocking && !save.isPending && !(clash && clash.kind !== "operator");
  const typeChanged = editing && base?.blueprint && spec && specChangedType(base, spec);

  return (
    <DialogShell
      title={title}
      description={t("operatorTemplateEditor.description")}
      size="cover"
      footer={
        <HStack w="100%" justify="space-between" wrap="wrap" gap={2}>
          <HStack gap={2} wrap="wrap">
            <input
              ref={fileInput}
              type="file"
              accept="application/json,.json"
              hidden
              data-testid="optpl-import-file"
              onChange={(e) => {
                const file = e.target.files?.[0];
                e.target.value = "";
                if (file) void onImport(file);
              }}
            />
            <Button size="sm" variant="ghost" onClick={() => fileInput.current?.click()}>
              <LuUpload /> {t("operatorTemplateEditor.import")}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={!input}
              onClick={() => input && download(`${input.id || "operator-template"}.json`, exportJson(input))}
            >
              <LuDownload /> {t("operatorTemplateEditor.export")}
            </Button>
            {editing && base ? (
              <>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    useLifecycle
                      .getState()
                      .openDialog({ kind: "operatorTemplateEditor", mode: "duplicate", templateId: base.id })
                  }
                >
                  <LuCopy /> {t("operatorTemplateEditor.duplicate")}
                </Button>
                <Button size="sm" variant="ghost" colorPalette="red" onClick={() => setConfirmDelete(true)}>
                  <LuTrash2 /> {t("common.delete")}
                </Button>
              </>
            ) : null}
          </HStack>
          <HStack gap={2}>
            {!canSave && input && blocking ? (
              <Text fontSize="xs" color="fg.error">
                {t("operatorTemplateEditor.fixErrors", { count: blocking })}
              </Text>
            ) : null}
            <Button size="sm" variant="ghost" onClick={close}>
              {t("common.cancel")}
            </Button>
            <Button
              size="sm"
              colorPalette="blue"
              disabled={!canSave}
              loading={save.isPending}
              data-testid="optpl-save"
              onClick={() => submit(editing ? etag : null)}
            >
              <LuSave /> {editing ? t("operatorTemplates.publishVersion") : t("operatorTemplateEditor.create")}
            </Button>
          </HStack>
        </HStack>
      }
    >
      <Stack ref={root} direction={{ base: "column", xl: "row" }} gap={4} align="stretch" data-testid="optpl-editor">
        <Stack flex="3" minW={0} gap={4}>
          {conflict ? (
            <Alert.Root ref={notice} status="warning" data-testid="optpl-conflict">
              <Alert.Indicator />
              <Alert.Content>
                <Alert.Title>
                  {editing
                    ? t("operatorTemplateEditor.conflictTitle", {
                        version: conflict.currentVersion ?? "?",
                        user: conflict.updatedBy ?? t("operatorTemplateEditor.someone"),
                      })
                    : conflict.currentVersion
                      ? t("operatorTemplateEditor.idExistsTitle", { version: conflict.currentVersion })
                      : t("operatorTemplateEditor.deletedTitle")}
                </Alert.Title>
                <Alert.Description>
                  <Stack gap={2} mt={1}>
                    <Text fontSize="sm">
                      {editing ? t("operatorTemplateEditor.conflictHelp") : t("operatorTemplateEditor.idExistsHelp")}
                    </Text>
                    <HStack gap={2} wrap="wrap">
                      {editing && conflict.currentEtag ? (
                        <Button size="xs" variant="outline" onClick={() => void reload()}>
                          <LuRefreshCw /> {t("operatorTemplateEditor.reloadLatest")}
                        </Button>
                      ) : null}
                      <Button
                        size="xs"
                        colorPalette="orange"
                        onClick={() => {
                          setEtag(conflict.currentEtag);
                          submit(conflict.currentEtag);
                        }}
                      >
                        <LuSave />{" "}
                        {conflict.currentEtag
                          ? t("operatorTemplateEditor.saveAnyway")
                          : t("operatorTemplateEditor.createAgain")}
                      </Button>
                      <Button size="xs" variant="ghost" onClick={() => setConflict(null)}>
                        {t("operatorTemplateEditor.dismiss")}
                      </Button>
                    </HStack>
                  </Stack>
                </Alert.Description>
              </Alert.Content>
            </Alert.Root>
          ) : null}
          {confirmDelete && base ? (
            <Alert.Root ref={notice} status="error" data-testid="optpl-delete-confirm">
              <Alert.Indicator />
              <Alert.Content>
                <Alert.Title>{t("operatorTemplateEditor.deleteTitle", { title: base.title })}</Alert.Title>
                <Alert.Description>
                  <Stack gap={2} mt={1}>
                    <Text fontSize="sm">{t("operatorTemplateEditor.deleteHelp")}</Text>
                    {usedHere ? (
                      <Text fontSize="sm" fontWeight="semibold">
                        {t("operatorTemplateEditor.deleteUsedHere", { count: usedHere })}
                      </Text>
                    ) : null}
                    <HStack gap={2}>
                      <Button
                        size="xs"
                        colorPalette="red"
                        loading={remove.isPending}
                        onClick={() =>
                          remove.mutate({
                            path: { template_id: base.id },
                            headers: etag ? { "if-match": etag } : {},
                          })
                        }
                      >
                        <LuTrash2 /> {t("operatorTemplateEditor.deleteConfirm")}
                      </Button>
                      <Button size="xs" variant="ghost" onClick={() => setConfirmDelete(false)}>
                        {t("common.cancel")}
                      </Button>
                    </HStack>
                  </Stack>
                </Alert.Description>
              </Alert.Content>
            </Alert.Root>
          ) : null}

          <Stack gap={3} as="section" aria-label={t("operatorTemplateEditor.metadata")}>
            <HStack align="start" gap={3} wrap="wrap">
              <Field.Root required flex="2" minW="200px" invalid={metaProblems.includes("title") && !!meta.title}>
                <Field.Label>{t("templates.fieldTitle")}</Field.Label>
                <Input
                  size="sm"
                  autoFocus={!initial.spec ? false : true}
                  value={meta.title}
                  onChange={(e) => setMeta({ title: e.target.value })}
                />
              </Field.Root>
              <Field.Root
                flex="1"
                minW="160px"
                invalid={(metaProblems.includes("id") && !!id) || !!clash}
                disabled={editing}
              >
                <Field.Label>{t("templates.fieldId")}</Field.Label>
                <Input
                  size="sm"
                  fontFamily="mono"
                  value={id}
                  aria-label={t("templates.fieldId")}
                  onChange={(e) => setMeta({ id: e.target.value, idTouched: true })}
                />
                {clash ? (
                  <Field.ErrorText>
                    {clash.kind === "operator"
                      ? t("operatorTemplateEditor.idInUse", { version: clash.version ?? "1" })
                      : t("operatorTemplates.idTaken")}
                  </Field.ErrorText>
                ) : (
                  <Field.HelperText>{t("operatorTemplateEditor.idHelp")}</Field.HelperText>
                )}
              </Field.Root>
            </HStack>
            <Field.Root>
              <Field.Label>{t("templates.fieldDescription")}</Field.Label>
              <Textarea
                size="sm"
                rows={2}
                value={meta.description}
                onChange={(e) => setMeta({ description: e.target.value })}
              />
            </Field.Root>
            <HStack align="start" gap={3} wrap="wrap">
              <Field.Root flex="1" minW="140px">
                <Field.Label>{t("operatorTemplates.ownerTeam")}</Field.Label>
                <Input size="sm" value={meta.ownerTeam} onChange={(e) => setMeta({ ownerTeam: e.target.value })} />
              </Field.Root>
              <Field.Root flex="2" minW="160px">
                <Field.Label>{t("templates.fieldTags")}</Field.Label>
                <Input size="sm" value={meta.tags} onChange={(e) => setMeta({ tags: e.target.value })} />
              </Field.Root>
              <Field.Root flex="1" minW="100px">
                <Field.Label>{t("operatorTemplates.version")}</Field.Label>
                <Input
                  size="sm"
                  placeholder={
                    base?.version && editing
                      ? t("operatorTemplateEditor.nextVersion", { version: base.version })
                      : t("operatorTemplates.versionAuto")
                  }
                  value={meta.version}
                  onChange={(e) => setMeta({ version: e.target.value })}
                />
              </Field.Root>
            </HStack>
            <HStack align="start" gap={3} wrap="wrap">
              <Field.Root flex="2" minW="200px" invalid={metaProblems.includes("docsUrl")}>
                <Field.Label>{t("operatorTemplateEditor.docsUrl")}</Field.Label>
                <Input
                  size="sm"
                  type="url"
                  placeholder="https://"
                  value={meta.docsUrl}
                  onChange={(e) => setMeta({ docsUrl: e.target.value })}
                />
                {metaProblems.includes("docsUrl") ? (
                  <Field.ErrorText>{t("operatorTemplateEditor.docsUrlInvalid")}</Field.ErrorText>
                ) : null}
              </Field.Root>
              {spec ? (
                <Box flex="1" minW="160px">
                  <TextField
                    label={t("operatorTemplateEditor.taskId")}
                    help={t("operatorTemplateEditor.taskIdHelp")}
                    mono
                    value={spec.task_id}
                    onChange={(v) => setSpec({ ...spec, task_id: v ?? "" } as TaskSpec)}
                  />
                </Box>
              ) : null}
            </HStack>
            <IconField value={meta.icon} effective={null} onChange={(v) => setMeta({ icon: v ?? null })} />
          </Stack>

          <Stack gap={2} as="section" aria-label={t("operatorTemplateEditor.operator")}>
            <HStack>
              <Text fontSize="sm" fontWeight="semibold" flex="1">
                {t("operatorTemplateEditor.operator")}
              </Text>
              {spec && spec.kind === "operator" ? (
                <Button size="xs" variant="outline" onClick={() => setPicking((p) => !p)}>
                  {picking ? t("operatorTemplateEditor.keepOperator") : t("operatorTemplateEditor.changeOperator")}
                </Button>
              ) : null}
            </HStack>
            {spec ? (
              <HStack gap={2}>
                <LuPackageCheck />
                <Text fontFamily="mono" fontSize="sm" data-testid="optpl-operator" truncate>
                  {spec.kind === "operator"
                    ? spec.operator
                    : spec.kind === "python_task"
                      ? `@task ${spec.function_name}`
                      : spec.handler}
                </Text>
              </HStack>
            ) : null}
            {typeChanged ? (
              <Text fontSize="xs" color="fg.warning">
                {t("operatorTemplateEditor.typeChanged")}
              </Text>
            ) : null}
            {picking || !spec ? <OperatorPicker onPick={pick} current={operator ?? undefined} /> : null}
          </Stack>

          {spec ? (
            <Tabs.Root value={tab} onValueChange={(e) => setTab(e.value as Tab)} size="sm" lazyMount>
              <Tabs.List flexWrap="wrap">
                <Tabs.Trigger value="params">{t("operatorTemplateEditor.tabs.params")}</Tabs.Trigger>
                <Tabs.Trigger value="settings">{t("operatorTemplateEditor.tabs.settings")}</Tabs.Trigger>
                <Tabs.Trigger value="modes">
                  {t("operatorTemplateEditor.tabs.modes")}
                  {counts.locked ? (
                    <Badge size="xs" colorPalette={MODE_PALETTE.locked}>
                      {t("operatorTemplateEditor.lockedCount", { count: counts.locked })}
                    </Badge>
                  ) : null}
                  {counts.required ? (
                    <Badge size="xs" colorPalette={MODE_PALETTE.required}>
                      {t("operatorTemplateEditor.requiredCount", { count: counts.required })}
                    </Badge>
                  ) : null}
                </Tabs.Trigger>
                <Tabs.Trigger value="code">{t("operatorTemplateEditor.tabs.code")}</Tabs.Trigger>
                {editing && base ? (
                  <Tabs.Trigger value="history">
                    <LuHistory /> {t("operatorTemplateEditor.tabs.history")}
                  </Tabs.Trigger>
                ) : null}
              </Tabs.List>
              <Tabs.Content value="params">
                {spec.kind === "operator" ? (
                  <OperatorParamsForm
                    nodeId={BLUEPRINT_NODE_ID}
                    operator={spec.operator}
                    params={spec.params ?? {}}
                    errors={errors}
                    allowXcom={false}
                    onChange={(params) => setSpec({ ...spec, params })}
                  />
                ) : (
                  <JsonField
                    label={t("operatorTemplateEditor.rawTask")}
                    help={t("operatorTemplateEditor.rawTaskHelp")}
                    rows={14}
                    value={spec}
                    validate={(v) =>
                      v && typeof v === "object" && !Array.isArray(v) && (v as { kind?: unknown }).kind === spec.kind
                        ? undefined
                        : t("operatorTemplateEditor.rawTaskInvalid")
                    }
                    onChange={(v) => v && setSpec(v as TaskSpec)}
                  />
                )}
              </Tabs.Content>
              <Tabs.Content value="settings">
                <ExecutionFields
                  spec={spec}
                  update={(patch) => setSpec({ ...spec, ...patch } as TaskSpec)}
                  errors={errors}
                />
              </Tabs.Content>
              <Tabs.Content value="modes">
                <ModesTable spec={spec} modes={modes} paramNames={paramNames} onChange={setMode} />
              </Tabs.Content>
              <Tabs.Content value="code">
                <Stack gap={1}>
                  <Text fontSize="xs" color="fg.muted">
                    {t("operatorTemplateEditor.codeHelp")}
                  </Text>
                  {checks.code ? (
                    <CodeView
                      value={checks.code}
                      language="python"
                      readOnly
                      height="360px"
                      label={t("operatorTemplateEditor.tabs.code")}
                    />
                  ) : checks.checking ? (
                    <Spinner size="sm" />
                  ) : (
                    <Text fontSize="xs" color="fg.muted">
                      {t("operatorTemplateEditor.noCode")}
                    </Text>
                  )}
                </Stack>
              </Tabs.Content>
              {editing && base ? (
                <Tabs.Content value="history">
                  <VersionHistory
                    id={base.id}
                    draft={draft}
                    onRestore={(tpl) => {
                      const restored = stateFromTemplate(tpl, "edit");
                      setState(restored);
                      setPicking(!restored.spec);
                      setTab("params");
                      toaster.create({
                        type: "info",
                        title: t("operatorTemplateEditor.restored", { version: tpl.version ?? "" }),
                      });
                    }}
                  />
                </Tabs.Content>
              ) : null}
            </Tabs.Root>
          ) : null}
        </Stack>
        <Box flex="2" minW={0} borderLeftWidth={{ xl: "1px" }} pl={{ xl: 4 }}>
          {spec ? (
            <ProblemsPanel problems={problems} checks={checks} onOpen={openProblem} />
          ) : (
            <Text fontSize="sm" color="fg.muted">
              {t("operatorTemplateEditor.pickFirst")}
            </Text>
          )}
        </Box>
      </Stack>
    </DialogShell>
  );
}

/** The task type of the draft differs from the saved template's (tasks inserted from it break). */
function specChangedType(base: Template, spec: TaskSpec): boolean {
  const node = base.blueprint?.node;
  if (!node) return false;
  if (node.kind !== spec.kind) return true;
  return node.kind === "operator" && spec.kind === "operator" && node.operator !== spec.operator;
}

/**
 * Standalone operator template editor: create a template without a DAG, publish a new
 * version of one (older versions stay readable), duplicate, delete, import and export.
 * Works in every editor mode: it never touches the canvas.
 */
export default function OperatorTemplateEditor({
  mode,
  templateId,
}: {
  mode: EditorMode;
  templateId?: string | undefined;
}) {
  const { t } = useTranslation("lifecycle");
  const meta = useMeta();
  const needsSource = mode !== "new" && !!templateId;
  const source = useQuery({
    ...getTemplateOptions({ path: { template_id: templateId ?? "" } }),
    enabled: needsSource,
    staleTime: 0,
  });
  if (!needsSource) {
    return <EditorBody key="new" mode="new" base={null} initial={emptyState(meta?.team ?? "")} />;
  }
  if (source.isPending || source.isError || source.data.kind !== "operator") {
    return (
      <DialogShell title={t("operatorTemplateEditor.newTitle")} size="md">
        {source.isPending ? (
          <Spinner size="sm" />
        ) : (
          <Text color="fg.error">
            {source.isError ? errorMessage(source.error) : t("operatorTemplateEditor.notOperator")}
          </Text>
        )}
      </DialogShell>
    );
  }
  // Keyed by template and mode: a later refetch does not reset the user's edits.
  return (
    <EditorBody
      key={`${mode}:${templateId}`}
      mode={mode}
      base={source.data}
      initial={stateFromTemplate(source.data, mode === "edit" ? "edit" : "duplicate")}
    />
  );
}
