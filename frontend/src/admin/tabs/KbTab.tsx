import {
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  HStack,
  Input,
  Link,
  Progress,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  Text,
  chakra,
} from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuDownload, LuRefreshCw, LuRotateCcw, LuSearch, LuSquare, LuUndo2, LuUpload } from "react-icons/lu";

import {
  kbCancelMutation,
  kbRefreshMutation,
  kbStatusOptions,
  kbStatusQueryKey,
} from "../../api/gen/@tanstack/react-query.gen";
import { kbExport, kbImport, kbPutSource, kbResetSource, kbSearch } from "../../api/gen/sdk.gen";
import type { ComponentInfo, KbSearchResult, KbStatus, SourceInfo } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { toaster } from "../../ui/toaster";
import { DataTable, Empty, QueryState, Section, StatCard } from "../common";
import { formatNumber, formatTime } from "../format";
import {
  componentRows,
  levelPalette,
  outcomePalette,
  parseVersions,
  progressPercent,
  safeHref,
  toggleComponent,
} from "../kbFormat";

const fail = (t: (k: string, o?: Record<string, unknown>) => string, err: unknown) =>
  toaster.create({ type: "error", title: t("error", { message: errorMessage(err) }) });

function Summary({ s }: { s: KbStatus }) {
  const { t, i18n } = useTranslation("admin");
  const store = s.store;
  const run = s.run ?? {};
  return (
    <Stack gap={3}>
      {store.ok === false ? (
        <Alert.Root status="error" size="sm" data-testid="admin-kb-store-error">
          <Alert.Indicator />
          <Alert.Title>{t("kb.storeError", { message: store.error ?? "?" })}</Alert.Title>
        </Alert.Root>
      ) : null}
      <SimpleGrid columns={{ base: 2, md: 3, xl: 6 }} gap={3} data-testid="admin-kb-summary">
        <StatCard label={t("kb.pages")} value={formatNumber(store.pages ?? 0, i18n.language)} testId="admin-kb-pages" />
        <StatCard label={t("kb.chunks")} value={formatNumber(store.chunks ?? 0, i18n.language)} />
        <StatCard
          label={t("kb.vectors")}
          value={formatNumber(store.vectors ?? 0, i18n.language)}
          hint={store.embedding_model ?? s.embedder ?? t("kb.noEmbedder")}
        />
        <StatCard
          label={t("kb.mode")}
          value={s.mode === "hybrid" ? t("kb.hybrid") : t("kb.lexical")}
          hint={s.reranker ? t("kb.reranker", { name: s.reranker }) : t("kb.noReranker")}
        />
        <StatCard
          label={t("kb.store")}
          value={store.kind}
          hint={<chakra.span wordBreak="break-all">{store.location}</chakra.span>}
        />
        <StatCard
          label={t("kb.lastRefresh")}
          value={formatTime(run.last_refresh, i18n.language)}
          hint={
            s.next_refresh
              ? t("kb.nextRefresh", { value: formatTime(s.next_refresh, i18n.language) })
              : t("kb.noSchedule")
          }
          testId="admin-kb-last-refresh"
        />
      </SimpleGrid>
      <HStack gap={2} wrap="wrap" fontSize="sm">
        <Badge colorPalette={s.online ? "green" : "gray"}>{s.online ? t("kb.online") : t("kb.airGapped")}</Badge>
        <Badge variant="outline">{t("kb.providers", { value: s.providers ?? "installed" })}</Badge>
        {run.outcome ? (
          <Badge colorPalette={outcomePalette(run.outcome)} data-testid="admin-kb-outcome">
            {t(`kb.outcome.${run.outcome}`, { defaultValue: run.outcome })}
          </Badge>
        ) : null}
      </HStack>
    </Stack>
  );
}

function Running({ s }: { s: KbStatus }) {
  const { t } = useTranslation("admin");
  const p = s.run?.progress ?? {};
  const pct = progressPercent(p.done, p.total);
  return (
    <Box borderWidth="1px" rounded="md" p={3} data-testid="admin-kb-running">
      <Text fontSize="sm" fontWeight="medium">
        {t("kb.running", { mode: s.run?.mode ?? "refresh", owner: s.run?.owner ?? "?" })}
      </Text>
      <Text fontSize="xs" color="fg.muted" wordBreak="break-all">
        {[p.source, p.unit, p.phase].filter(Boolean).join(" · ")}
        {typeof p.done === "number" ? ` — ${p.done}${typeof p.total === "number" ? ` / ${p.total}` : ""}` : ""}
      </Text>
      <Progress.Root size="xs" mt={2} value={pct} max={100}>
        <Progress.Track>
          <Progress.Range />
        </Progress.Track>
      </Progress.Root>
    </Box>
  );
}

function Components({ s }: { s: KbStatus }) {
  const { t, i18n } = useTranslation("admin");
  const rows = componentRows(s.store.by_component, s.store.versions);
  const titles = new Map((s.components ?? []).map((c) => [c.id, c.title]));
  return (
    <Section title={t("kb.components")}>
      {rows.length ? (
        <DataTable label={t("kb.components")} testId="admin-kb-components">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>{t("kb.component")}</Table.ColumnHeader>
              <Table.ColumnHeader textAlign="end">{t("kb.pages")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("kb.versions")}</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {rows.map((r) => (
              <Table.Row key={r.component}>
                <Table.Cell>{titles.get(r.component) ?? r.component}</Table.Cell>
                <Table.Cell textAlign="end">{formatNumber(r.pages, i18n.language)}</Table.Cell>
                <Table.Cell fontFamily="mono" fontSize="xs" wordBreak="break-all">
                  {r.versions.join(", ")}
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </DataTable>
      ) : (
        <Empty />
      )}
    </Section>
  );
}

function SourceRow({
  source,
  components,
  canWrite,
  onChanged,
}: {
  source: SourceInfo;
  components: ComponentInfo[];
  canWrite: boolean;
  onChanged: () => void;
}) {
  const { t, i18n } = useTranslation("admin");
  const [busy, setBusy] = useState(false);
  const [versions, setVersions] = useState((source.versions ?? []).join(", "));
  const parsed = parseVersions(versions);
  const save = async (body: { enabled?: boolean; components?: string[]; versions?: string[] }) => {
    setBusy(true);
    try {
      await kbPutSource({ path: { source_id: source.id }, body, throwOnError: true });
      onChanged();
    } catch (err) {
      fail(t, err);
    } finally {
      setBusy(false);
    }
  };
  const reset = async () => {
    setBusy(true);
    try {
      await kbResetSource({ path: { source_id: source.id }, throwOnError: true });
      onChanged();
    } catch (err) {
      fail(t, err);
    } finally {
      setBusy(false);
    }
  };
  const selected = source.components ?? [];
  return (
    <Table.Row verticalAlign="top" data-testid={`admin-kb-source-${source.id}`}>
      <Table.Cell>
        <Switch.Root
          size="sm"
          checked={source.enabled}
          disabled={!canWrite || busy}
          onCheckedChange={(e) => void save({ enabled: e.checked })}
        >
          <Switch.HiddenInput aria-label={t("kb.toggleSource", { id: source.id })} />
          <Switch.Control />
        </Switch.Root>
      </Table.Cell>
      <Table.Cell>
        <Text fontWeight="medium">{source.title}</Text>
        <HStack gap={1} wrap="wrap">
          <Badge size="xs" variant="outline">
            {source.kind}
          </Badge>
          {source.layout ? (
            <Badge size="xs" variant="outline">
              {source.layout}
            </Badge>
          ) : null}
          {source.overridden ? (
            <Badge size="xs" colorPalette="purple">
              {t("kb.overridden")}
            </Badge>
          ) : null}
        </HStack>
        {source.url ? (
          <Text fontSize="xs" color="fg.muted" fontFamily="mono" wordBreak="break-all">
            {source.url}
            {source.ref ? ` @ ${source.ref}` : ""}
          </Text>
        ) : null}
        {source.conn_id ? (
          <Text fontSize="xs" color="fg.muted">
            {t("kb.connId", { id: source.conn_id })}
          </Text>
        ) : null}
        {!source.usable ? (
          <Text fontSize="xs" color="fg.warning" data-testid="admin-kb-source-reason">
            {source.reason_code
              ? t(`kb.reason.${source.reason_code}`, { defaultValue: source.reason ?? t("kb.notUsable") })
              : (source.reason ?? t("kb.notUsable"))}
          </Text>
        ) : null}
      </Table.Cell>
      <Table.Cell>
        <HStack gap={2} wrap="wrap" maxW="md">
          {components.map((c) => (
            <Checkbox.Root
              key={c.id}
              size="sm"
              checked={selected.includes(c.id)}
              disabled={!canWrite || busy}
              onCheckedChange={() => void save({ components: toggleComponent(selected, c.id) })}
            >
              <Checkbox.HiddenInput />
              <Checkbox.Control />
              <Checkbox.Label fontSize="xs" title={c.description}>
                {c.title}
              </Checkbox.Label>
            </Checkbox.Root>
          ))}
        </HStack>
      </Table.Cell>
      <Table.Cell>
        <Stack gap={1}>
          <Input
            size="xs"
            value={versions}
            disabled={!canWrite || busy}
            aria-label={t("kb.versionsFor", { id: source.id })}
            onChange={(e) => setVersions(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !parsed.invalid.length) void save({ versions: parsed.versions });
            }}
            onBlur={() => {
              const current = (source.versions ?? []).join(",");
              if (!parsed.invalid.length && parsed.versions.join(",") !== current)
                void save({ versions: parsed.versions });
            }}
            minW="10rem"
          />
          {parsed.invalid.length ? (
            <Text fontSize="xs" color="fg.error">
              {t("kb.invalidVersions", { values: parsed.invalid.join(", ") })}
            </Text>
          ) : null}
        </Stack>
      </Table.Cell>
      <Table.Cell textAlign="end">{formatNumber(source.pages ?? 0, i18n.language)}</Table.Cell>
      <Table.Cell>
        {canWrite && source.overridden ? (
          <Button size="2xs" variant="ghost" onClick={() => void reset()} disabled={busy}>
            <LuUndo2 /> {t("kb.reset")}
          </Button>
        ) : null}
      </Table.Cell>
    </Table.Row>
  );
}

function Sources({ s, canWrite, onChanged }: { s: KbStatus; canWrite: boolean; onChanged: () => void }) {
  const { t } = useTranslation("admin");
  const sources = s.sources ?? [];
  return (
    <Section title={t("kb.sources")} help={t("kb.sourcesHelp")}>
      {sources.length ? (
        <DataTable label={t("kb.sources")} testId="admin-kb-sources">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>{t("kb.enabled")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("kb.source")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("kb.components")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("kb.versions")}</Table.ColumnHeader>
              <Table.ColumnHeader textAlign="end">{t("kb.pages")}</Table.ColumnHeader>
              <Table.ColumnHeader />
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {sources.map((src) => (
              <SourceRow
                key={`${src.id}-${(src.versions ?? []).join(",")}`}
                source={src}
                components={s.components ?? []}
                canWrite={canWrite}
                onChanged={onChanged}
              />
            ))}
          </Table.Body>
        </DataTable>
      ) : (
        <Empty />
      )}
    </Section>
  );
}

function LastRun({ s }: { s: KbStatus }) {
  const { t, i18n } = useTranslation("admin");
  const runs = s.run?.sources ?? [];
  const errors = s.run?.errors ?? [];
  return (
    <Section title={t("kb.lastRun")}>
      {runs.length ? (
        <DataTable label={t("kb.lastRun")} testId="admin-kb-runs">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>{t("kb.source")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("kb.state")}</Table.ColumnHeader>
              <Table.ColumnHeader textAlign="end">{t("kb.changed")}</Table.ColumnHeader>
              <Table.ColumnHeader textAlign="end">{t("kb.unchanged")}</Table.ColumnHeader>
              <Table.ColumnHeader textAlign="end">{t("kb.removed")}</Table.ColumnHeader>
              <Table.ColumnHeader textAlign="end">{t("kb.failed")}</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {runs.map((r, i) => (
              <Table.Row key={`${r.source}-${i}`} verticalAlign="top">
                <Table.Cell>
                  {r.source}
                  {r.detail ? (
                    <Text fontSize="xs" color="fg.muted" wordBreak="break-word">
                      {r.detail}
                    </Text>
                  ) : null}
                </Table.Cell>
                <Table.Cell>
                  <Badge size="xs" colorPalette={outcomePalette(r.state)}>
                    {t(`kb.sourceState.${r.state ?? "ok"}`, { defaultValue: r.state ?? "ok" })}
                  </Badge>
                </Table.Cell>
                <Table.Cell textAlign="end">{formatNumber(r.pages_changed ?? 0, i18n.language)}</Table.Cell>
                <Table.Cell textAlign="end">{formatNumber(r.pages_unchanged ?? 0, i18n.language)}</Table.Cell>
                <Table.Cell textAlign="end">{formatNumber(r.pages_removed ?? 0, i18n.language)}</Table.Cell>
                <Table.Cell textAlign="end">{formatNumber(r.pages_failed ?? 0, i18n.language)}</Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </DataTable>
      ) : (
        <Empty />
      )}
      {errors.length ? (
        <Stack gap={1} data-testid="admin-kb-errors">
          <Text fontSize="sm" fontWeight="medium">
            {t("kb.messages", { count: errors.length })}
          </Text>
          {errors.slice(0, 100).map((e, i) => (
            <HStack key={i} gap={2} align="start" fontSize="xs">
              <Badge size="xs" colorPalette={levelPalette(e.level)} flexShrink={0}>
                {t(`kb.level.${e.level ?? "error"}`, { defaultValue: e.level ?? "error" })}
              </Badge>
              <Text wordBreak="break-word">
                {[e.source, e.url].filter(Boolean).join(" · ")}
                {e.source || e.url ? " — " : ""}
                {e.message}
              </Text>
            </HStack>
          ))}
        </Stack>
      ) : null}
    </Section>
  );
}

function TestQuery({ s }: { s: KbStatus }) {
  const { t } = useTranslation("admin");
  const [q, setQ] = useState("");
  const [component, setComponent] = useState("");
  const [result, setResult] = useState<KbSearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async (e: FormEvent) => {
    e.preventDefault();
    const query = q.trim();
    if (!query) return;
    setBusy(true);
    try {
      const { data } = await kbSearch({
        query: { q: query.slice(0, 500), limit: 8, component: component ? [component] : undefined },
        throwOnError: true,
      });
      setResult(data);
    } catch (err) {
      fail(t, err);
    } finally {
      setBusy(false);
    }
  };
  const hits = result?.hits ?? [];
  return (
    <Section title={t("kb.testQuery")} help={t("kb.testQueryHelp")}>
      <chakra.form onSubmit={(e) => void run(e)} display="flex" gap={2} flexWrap="wrap">
        <Input
          size="sm"
          flex="1"
          minW="14rem"
          value={q}
          maxLength={500}
          placeholder={t("kb.queryPlaceholder")}
          aria-label={t("kb.testQuery")}
          onChange={(e) => setQ(e.target.value)}
          data-testid="admin-kb-query"
        />
        <chakra.select
          value={component}
          onChange={(e) => setComponent(e.target.value)}
          aria-label={t("kb.component")}
          fontSize="sm"
          borderWidth="1px"
          rounded="md"
          px={2}
          bg="bg"
          color="fg"
        >
          <option value="">{t("kb.allComponents")}</option>
          {(s.components ?? []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.title}
            </option>
          ))}
        </chakra.select>
        <Button size="sm" type="submit" loading={busy} disabled={!q.trim()} data-testid="admin-kb-search">
          <LuSearch /> {t("kb.search")}
        </Button>
      </chakra.form>
      {result ? (
        <Stack gap={2} data-testid="admin-kb-hits">
          <Text fontSize="xs" color="fg.muted">
            {t("kb.hits", {
              count: hits.length,
              mode: t(`kb.searchMode.${result.mode}`, { defaultValue: result.mode }),
            })}
            {result.reranked ? ` · ${t("kb.reranked")}` : ""}
          </Text>
          {hits.length ? (
            hits.map((h, i) => {
              const href = safeHref(h.url);
              return (
                <Box key={h.id ?? i} borderWidth="1px" rounded="md" p={2} data-testid="admin-kb-hit">
                  <HStack gap={2} wrap="wrap">
                    <Badge size="xs" variant="outline">
                      {i + 1}
                    </Badge>
                    {href ? (
                      <Link href={href} target="_blank" rel="noopener noreferrer" color="blue.fg" fontWeight="medium">
                        {h.title}
                        {h.heading ? ` — ${h.heading.split(" > ").pop()}` : ""}
                      </Link>
                    ) : (
                      <Text fontWeight="medium">{h.title}</Text>
                    )}
                    <Badge size="xs">{h.component}</Badge>
                    <Text fontSize="xs" color="fg.muted" fontFamily="mono">
                      {h.package} {h.version}
                    </Text>
                  </HStack>
                  <Text fontSize="xs" color="fg.muted" fontFamily="mono" wordBreak="break-all">
                    {h.url}
                  </Text>
                  <Text fontSize="sm" mt={1} wordBreak="break-word">
                    {h.snippet}
                  </Text>
                </Box>
              );
            })
          ) : (
            <Empty />
          )}
        </Stack>
      ) : null}
    </Section>
  );
}

function Bundles({ canWrite, onChanged }: { canWrite: boolean; onChanged: () => void }) {
  const { t } = useTranslation("admin");
  const file = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<"export" | "import" | null>(null);
  const download = async () => {
    setBusy("export");
    try {
      const { data } = await kbExport({ parseAs: "blob", throwOnError: true });
      const blob = data instanceof Blob ? data : new Blob([data as BlobPart], { type: "application/zip" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `autoair-kb-${new Date().toISOString().slice(0, 10)}.zip`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      fail(t, err);
    } finally {
      setBusy(null);
    }
  };
  const upload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    setBusy("import");
    try {
      const { data } = await kbImport({ body: f, throwOnError: true });
      toaster.create({
        type: "success",
        title: t("kb.imported", { pages: data.pages ?? 0, changed: data.changed ?? 0, removed: data.removed ?? 0 }),
      });
      onChanged();
    } catch (err) {
      fail(t, err);
    } finally {
      setBusy(null);
    }
  };
  if (!canWrite) return null;
  return (
    <Section title={t("kb.bundles")} help={t("kb.bundlesHelp")}>
      <HStack gap={2} wrap="wrap">
        <Button
          size="xs"
          variant="outline"
          loading={busy === "export"}
          onClick={() => void download()}
          data-testid="admin-kb-export"
        >
          <LuDownload /> {t("kb.export")}
        </Button>
        <chakra.input
          ref={file}
          type="file"
          accept=".zip,application/zip"
          display="none"
          onChange={(e) => void upload(e)}
          data-testid="admin-kb-import-file"
        />
        <Button
          size="xs"
          variant="outline"
          loading={busy === "import"}
          onClick={() => file.current?.click()}
          data-testid="admin-kb-import"
        >
          <LuUpload /> {t("kb.import")}
        </Button>
      </HStack>
    </Section>
  );
}

/** The documentation knowledge base: index status, sources, refreshes, test search, bundles. */
export function KbTab({ canWrite }: { canWrite: boolean }) {
  const { t } = useTranslation("admin");
  const client = useQueryClient();
  const q = useQuery({
    ...kbStatusOptions(),
    retry: false,
    refetchInterval: (query) => (query.state.data?.run?.state === "running" ? 2000 : false),
  });
  const reload = () => void client.invalidateQueries({ queryKey: kbStatusQueryKey() });
  const refresh = useMutation({
    ...kbRefreshMutation(),
    onSuccess: () => {
      toaster.create({ type: "info", title: t("kb.started") });
      reload();
    },
    onError: (err) => fail(t, err),
  });
  const cancel = useMutation({ ...kbCancelMutation(), onSuccess: reload, onError: (err) => fail(t, err) });
  const s = q.data;
  const running = s?.run?.state === "running";
  return (
    <Section
      help={t("kb.help")}
      actions={
        canWrite ? (
          <HStack gap={2}>
            <Button
              size="xs"
              variant="outline"
              loading={refresh.isPending && refresh.variables?.body?.mode !== "rebuild"}
              disabled={running}
              onClick={() => refresh.mutate({ body: { mode: "refresh" } })}
              data-testid="admin-kb-refresh"
            >
              <LuRefreshCw /> {t("kb.refresh")}
            </Button>
            <Button
              size="xs"
              variant="outline"
              colorPalette="orange"
              loading={refresh.isPending && refresh.variables?.body?.mode === "rebuild"}
              disabled={running}
              onClick={() => {
                if (window.confirm(t("kb.confirmRebuild"))) refresh.mutate({ body: { mode: "rebuild" } });
              }}
              data-testid="admin-kb-rebuild"
            >
              <LuRotateCcw /> {t("kb.rebuild")}
            </Button>
            {running ? (
              <Button
                size="xs"
                variant="outline"
                colorPalette="red"
                loading={cancel.isPending}
                onClick={() => cancel.mutate({})}
                data-testid="admin-kb-cancel"
              >
                <LuSquare /> {t("kb.cancel")}
              </Button>
            ) : null}
          </HStack>
        ) : null
      }
    >
      <QueryState isPending={q.isPending} error={q.error}>
        {s ? (
          <Stack gap={5} data-testid="admin-kb">
            {!canWrite ? (
              <Text fontSize="xs" color="fg.muted">
                {t("kb.readOnly")}
              </Text>
            ) : null}
            <Summary s={s} />
            {running ? <Running s={s} /> : null}
            <TestQuery s={s} />
            <Components s={s} />
            <Sources s={s} canWrite={canWrite} onChanged={reload} />
            <LastRun s={s} />
            <Bundles canWrite={canWrite} onChanged={reload} />
          </Stack>
        ) : (
          <Empty />
        )}
      </QueryState>
    </Section>
  );
}
