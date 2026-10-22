import {
  Badge,
  Box,
  Button,
  Checkbox,
  Field,
  HStack,
  IconButton,
  Input,
  NativeSelect,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  Text,
} from "@chakra-ui/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuPencil, LuPlug, LuPlus, LuSave, LuTrash2, LuX } from "react-icons/lu";

import { adminMcpServersOptions } from "../../api/gen/@tanstack/react-query.gen";
import { adminDeleteMcpServer, adminPutMcpServer, adminTestMcpServer } from "../../api/gen/sdk.gen";
import type { McpServerView, McpToolInfo } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { toaster } from "../../ui/toaster";
import { DataTable, Empty, QueryState, Section } from "../common";

const AGENTS = ["troubleshoot", "developer", "admin"] as const;
type Agent = (typeof AGENTS)[number];
const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,39}$/;
const MASK = "***";

type Draft = {
  name: string;
  description: string;
  transport: "http" | "sse";
  url: string;
  tools: string;
  readOnlyTools: string;
  headers: Array<{ key: string; value: string }>;
  timeout: number;
  enabled: boolean;
  agents: Agent[];
};

function draftOf(view: McpServerView | null): Draft {
  return {
    name: view?.name ?? "",
    description: view?.description ?? "",
    transport: view?.transport === "sse" ? "sse" : "http",
    url: view?.url ?? "https://",
    tools: (view?.tools ?? ["*"]).join(", "),
    readOnlyTools: (view?.read_only_tools ?? []).join(", "),
    headers: (view?.headers ?? []).map((key) => ({ key, value: MASK })),
    timeout: view?.timeout_seconds ?? 30,
    enabled: view?.enabled ?? true,
    agents: (view?.agents as Agent[] | undefined) ?? ["troubleshoot"],
  };
}

const splitNames = (text: string) =>
  text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

function ServerForm({ initial, onDone }: { initial: McpServerView | null; onDone: () => void }) {
  const { t } = useTranslation("admin");
  const [d, setD] = useState<Draft>(() => draftOf(initial));
  const [busy, setBusy] = useState(false);
  const isNew = !initial;
  const valid = NAME_RE.test(d.name) && /^https?:\/\/.+/.test(d.url);
  const save = async () => {
    setBusy(true);
    try {
      const removed = (initial?.headers ?? []).filter((h) => !d.headers.some((x) => x.key === h));
      await adminPutMcpServer({
        path: { name: d.name },
        body: {
          description: d.description,
          transport: d.transport,
          url: d.url,
          tools: splitNames(d.tools),
          read_only_tools: splitNames(d.readOnlyTools),
          headers: {
            ...Object.fromEntries(d.headers.filter((h) => h.key.trim()).map((h) => [h.key.trim(), h.value])),
            ...Object.fromEntries(removed.map((h) => [h, ""])),
          },
          timeout_seconds: d.timeout,
          enabled: d.enabled,
          agents: d.agents,
        },
        throwOnError: true,
      });
      toaster.create({ type: "success", title: t("mcp.saved", { name: d.name }) });
      onDone();
    } catch (err) {
      toaster.create({ type: "error", title: t("error", { message: errorMessage(err) }) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Box borderWidth="1px" rounded="md" p={3} data-testid="admin-mcp-form">
      <SimpleGrid columns={{ base: 1, md: 2 }} gap={3}>
        <Field.Root required invalid={!!d.name && !NAME_RE.test(d.name)}>
          <Field.Label>{t("mcp.name")}</Field.Label>
          <Input size="sm" value={d.name} disabled={!isNew} onChange={(e) => setD({ ...d, name: e.target.value })} />
          <Field.ErrorText>{t("mcp.badName")}</Field.ErrorText>
        </Field.Root>
        <Field.Root>
          <Field.Label>{t("mcp.description")}</Field.Label>
          <Input size="sm" value={d.description} onChange={(e) => setD({ ...d, description: e.target.value })} />
        </Field.Root>
        <Field.Root required invalid={!/^https?:\/\/.+/.test(d.url)}>
          <Field.Label>{t("mcp.url")}</Field.Label>
          <HStack w="full">
            <NativeSelect.Root size="sm" w="110px" flexShrink={0}>
              <NativeSelect.Field
                aria-label={t("mcp.transport")}
                value={d.transport}
                onChange={(e) => setD({ ...d, transport: e.target.value as "http" | "sse" })}
              >
                <option value="http">HTTP</option>
                <option value="sse">SSE</option>
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
            <Input size="sm" value={d.url} onChange={(e) => setD({ ...d, url: e.target.value })} />
          </HStack>
        </Field.Root>
        <Field.Root>
          <Field.Label>{t("mcp.tools")}</Field.Label>
          <Input size="sm" value={d.tools} onChange={(e) => setD({ ...d, tools: e.target.value })} />
          <Field.HelperText>{t("mcp.toolsHelp")}</Field.HelperText>
        </Field.Root>
        <Field.Root>
          <Field.Label>{t("mcp.readOnlyTools")}</Field.Label>
          <Input
            size="sm"
            value={d.readOnlyTools}
            onChange={(e) => setD({ ...d, readOnlyTools: e.target.value })}
            data-testid="admin-mcp-read-only-tools"
          />
          <Field.HelperText>{t("mcp.readOnlyToolsHelp")}</Field.HelperText>
        </Field.Root>
        <Field.Root>
          <Field.Label>{t("mcp.timeout")}</Field.Label>
          <Input
            size="sm"
            type="number"
            min={1}
            max={600}
            value={d.timeout}
            onChange={(e) => setD({ ...d, timeout: Math.min(600, Math.max(1, Number(e.target.value) || 30)) })}
          />
        </Field.Root>
        <Field.Root>
          <Field.Label>{t("mcp.agents")}</Field.Label>
          <HStack gap={3} wrap="wrap">
            {AGENTS.map((a) => (
              <Checkbox.Root
                key={a}
                size="sm"
                checked={d.agents.includes(a)}
                onCheckedChange={(e) =>
                  setD({ ...d, agents: e.checked ? [...d.agents, a] : d.agents.filter((x) => x !== a) })
                }
              >
                <Checkbox.HiddenInput />
                <Checkbox.Control />
                <Checkbox.Label>{t(`mcp.agent.${a}`)}</Checkbox.Label>
              </Checkbox.Root>
            ))}
          </HStack>
        </Field.Root>
      </SimpleGrid>
      <Box mt={3}>
        <Text fontSize="sm" fontWeight="medium" mb={1}>
          {t("mcp.headers")}
        </Text>
        <Text fontSize="xs" color="fg.muted" mb={2}>
          {t("mcp.headersHelp")}
        </Text>
        <Stack gap={1.5}>
          {d.headers.map((h, i) => (
            <HStack key={i}>
              <Input
                size="sm"
                maxW="220px"
                placeholder="Authorization"
                aria-label={t("mcp.headerName")}
                value={h.key}
                onChange={(e) =>
                  setD({ ...d, headers: d.headers.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)) })
                }
              />
              <Input
                size="sm"
                type="password"
                autoComplete="off"
                placeholder="Bearer …"
                aria-label={t("mcp.headerValue")}
                value={h.value}
                onChange={(e) =>
                  setD({ ...d, headers: d.headers.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)) })
                }
              />
              <IconButton
                size="xs"
                variant="ghost"
                aria-label={t("mcp.removeHeader")}
                onClick={() => setD({ ...d, headers: d.headers.filter((_, j) => j !== i) })}
              >
                <LuX />
              </IconButton>
            </HStack>
          ))}
          <Button
            size="xs"
            variant="ghost"
            alignSelf="start"
            onClick={() => setD({ ...d, headers: [...d.headers, { key: "", value: "" }] })}
          >
            <LuPlus /> {t("mcp.addHeader")}
          </Button>
        </Stack>
      </Box>
      <HStack mt={3} gap={2}>
        <Switch.Root size="sm" checked={d.enabled} onCheckedChange={(e) => setD({ ...d, enabled: e.checked })}>
          <Switch.HiddenInput />
          <Switch.Control />
          <Switch.Label>{t("mcp.enabled")}</Switch.Label>
        </Switch.Root>
        <Box flex="1" />
        <Button size="xs" variant="ghost" onClick={onDone}>
          {t("mcp.cancel")}
        </Button>
        <Button size="xs" colorPalette="blue" loading={busy} disabled={!valid} onClick={() => void save()}>
          <LuSave /> {t("mcp.save")}
        </Button>
      </HStack>
    </Box>
  );
}

/** External MCP servers the AI agents may attach (configuration + admin-defined). */
export function McpServersTab({ canWrite }: { canWrite: boolean }) {
  const { t } = useTranslation("admin");
  const client = useQueryClient();
  const q = useQuery(adminMcpServersOptions());
  const [editing, setEditing] = useState<McpServerView | "new" | null>(null);
  const [tested, setTested] = useState<Record<string, McpToolInfo[] | string>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const refresh = () => {
    setEditing(null);
    void client.invalidateQueries({ queryKey: adminMcpServersOptions().queryKey });
  };
  const test = async (name: string) => {
    setTesting(name);
    try {
      const { data } = await adminTestMcpServer({ path: { name }, throwOnError: true });
      setTested((s) => ({ ...s, [name]: data }));
    } catch (err) {
      setTested((s) => ({ ...s, [name]: errorMessage(err) }));
    } finally {
      setTesting(null);
    }
  };
  const remove = async (name: string) => {
    if (!window.confirm(t("mcp.confirmDelete", { name }))) return;
    try {
      await adminDeleteMcpServer({ path: { name }, throwOnError: true });
      refresh();
    } catch (err) {
      toaster.create({ type: "error", title: t("error", { message: errorMessage(err) }) });
    }
  };
  const servers = q.data ?? [];
  return (
    <Section
      help={t("mcp.help")}
      actions={
        canWrite ? (
          <Button size="xs" variant="outline" onClick={() => setEditing("new")} disabled={editing !== null}>
            <LuPlus /> {t("mcp.add")}
          </Button>
        ) : null
      }
    >
      {editing ? <ServerForm initial={editing === "new" ? null : editing} onDone={refresh} /> : null}
      <QueryState isPending={q.isPending} error={q.error}>
        {servers.length ? (
          <DataTable label={t("tabs.mcp")} testId="admin-mcp-table">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeader>{t("mcp.name")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("mcp.url")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("mcp.tools")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("mcp.agents")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("mcp.source")}</Table.ColumnHeader>
                <Table.ColumnHeader />
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {servers.map((s) => {
                const result = tested[s.name];
                return (
                  <Table.Row key={s.name}>
                    <Table.Cell>
                      <HStack gap={1.5}>
                        <Text fontWeight="medium">{s.name}</Text>
                        {!s.enabled ? <Badge size="xs">{t("mcp.disabled")}</Badge> : null}
                      </HStack>
                      <Text fontSize="xs" color="fg.muted">
                        {s.description}
                      </Text>
                      {result ? (
                        typeof result === "string" ? (
                          <Text fontSize="xs" color="fg.error">
                            {result}
                          </Text>
                        ) : (
                          <HStack gap={1} wrap="wrap" mt={1} data-testid="admin-mcp-tools">
                            {result.map((tool) => (
                              <Badge
                                key={tool.name}
                                size="xs"
                                variant={tool.allowed ? "subtle" : "outline"}
                                colorPalette={tool.allowed ? "green" : "gray"}
                                title={tool.description}
                              >
                                {tool.name}
                                {tool.read_only ? ` · ${t("mcp.readOnly")}` : ""}
                              </Badge>
                            ))}
                          </HStack>
                        )
                      ) : null}
                    </Table.Cell>
                    <Table.Cell>
                      <Text fontFamily="mono" fontSize="xs" wordBreak="break-all">
                        {s.transport === "stdio" ? s.command : s.url}
                      </Text>
                      <Text fontSize="2xs" color="fg.muted">
                        {s.transport.toUpperCase()}
                        {s.headers?.length ? ` · ${t("mcp.headerCount", { list: s.headers.join(", ") })}` : ""}
                        {` · ${s.timeout_seconds ?? 30}s`}
                      </Text>
                    </Table.Cell>
                    <Table.Cell fontFamily="mono" fontSize="xs">
                      {(s.tools ?? []).join(", ")}
                    </Table.Cell>
                    <Table.Cell fontSize="xs">{(s.agents ?? []).map((a) => t(`mcp.agent.${a}`)).join(", ")}</Table.Cell>
                    <Table.Cell>
                      <Badge size="xs" variant="outline">
                        {t(`mcp.sources.${s.source}`)}
                      </Badge>
                    </Table.Cell>
                    <Table.Cell textAlign="end" whiteSpace="nowrap">
                      {canWrite ? (
                        <>
                          <IconButton
                            size="xs"
                            variant="ghost"
                            aria-label={t("mcp.test")}
                            title={t("mcp.test")}
                            loading={testing === s.name}
                            onClick={() => void test(s.name)}
                          >
                            <LuPlug />
                          </IconButton>
                          {s.source === "database" ? (
                            <>
                              <IconButton
                                size="xs"
                                variant="ghost"
                                aria-label={t("mcp.edit")}
                                title={t("mcp.edit")}
                                onClick={() => setEditing(s)}
                              >
                                <LuPencil />
                              </IconButton>
                              <IconButton
                                size="xs"
                                variant="ghost"
                                colorPalette="red"
                                aria-label={t("mcp.delete")}
                                title={t("mcp.delete")}
                                onClick={() => void remove(s.name)}
                              >
                                <LuTrash2 />
                              </IconButton>
                            </>
                          ) : null}
                        </>
                      ) : null}
                    </Table.Cell>
                  </Table.Row>
                );
              })}
            </Table.Body>
          </DataTable>
        ) : (
          <Empty />
        )}
      </QueryState>
    </Section>
  );
}
