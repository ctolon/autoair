import { Box, Field, HStack, Heading, Stack, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { listNodeKindsOptions } from "../../api/gen/@tanstack/react-query.gen";
import { ancestors } from "../../canvas/graph";
import { nodeKindExtension } from "../../extensions/registry";
import type { JsonSchema } from "../../lib/formSchema";
import type { BuilderNode, CustomFlowNode, NoteFlowNode, TaskGroupFlowNode } from "../../lib/types";
import { isTaskNode, nodeKey } from "../../lib/types";
import { effectiveTaskId } from "../../lib/xcom";
import { useBuilder } from "../../store/builder";
import { CodeEditor } from "../../widgets/code";
import { useAirflowGate } from "../../widgets/airflowMeta";
import { Hint, NumberField, SelectField, SwitchField, TextField } from "../../widgets/fields";
import { SchemaForm } from "../../widgets/SchemaForm";
import { VersionBadge } from "../../widgets/VersionBadge";
import { useFieldError, useUpdate } from "./shared";

export function ParentGroupSelect({ node }: { node: BuilderNode }) {
  const { t } = useTranslation("inspector");
  const nodes = useBuilder((s) => s.nodes);
  const setParent = useBuilder((s) => s.setParent);
  const groups = nodes.filter(
    (n) => n.type === "task_group" && n.id !== node.id && !ancestors(nodes, n.id).has(node.id),
  );
  return (
    <SelectField
      help={t("node.taskGroupHelp")}
      label={t("node.taskGroup")}
      field="parent_group"
      value={node.parentId ?? null}
      emptyLabel={t("common.none")}
      options={groups.map((g) => ({ value: g.id, label: nodeKey(g) }))}
      onChange={(v) => setParent(node.id, v)}
    />
  );
}

export function CustomParams({ node }: { node: CustomFlowNode }) {
  const { t } = useTranslation("inspector");
  const update = useUpdate<"custom">(node.id);
  const kinds = useQuery(listNodeKindsOptions());
  const kind = kinds.data?.find((k) => k.name === node.data.spec.handler);
  const ext = nodeKindExtension(node.data.spec.handler);
  const config = node.data.spec.config ?? {};
  return (
    <Stack gap={3}>
      <Text fontSize="sm" fontWeight="semibold">
        {kind?.title ?? node.data.spec.handler}
      </Text>
      {!kind && !kinds.isPending ? (
        <Hint tone="error">{t("custom.missing", { handler: node.data.spec.handler })}</Hint>
      ) : null}
      {ext?.Inspector ? (
        <ext.Inspector spec={node.data.spec} onChange={(c) => update({ config: c })} />
      ) : kind ? (
        <SchemaForm schema={kind.config_schema as JsonSchema} value={config} onChange={(c) => update({ config: c })} />
      ) : null}
    </Stack>
  );
}

export function GroupInspector({ node }: { node: TaskGroupFlowNode }) {
  const { t } = useTranslation("inspector");
  const update = useUpdate<"task_group">(node.id);
  const spec = node.data.spec;
  const err = useFieldError(node.id, "task_id");
  const nodes = useBuilder((s) => s.nodes);
  const members = nodes.filter((n) => isTaskNode(n) && ancestors(nodes, n.id).has(node.id));
  return (
    <Stack gap={2}>
      <TextField
        label={t("group.groupId")}
        field="task_id"
        mono
        required
        value={spec.group_id}
        error={err}
        help={t("group.effectiveId", { id: effectiveTaskId(nodes, node.id) })}
        onChange={(v) => update({ group_id: v ?? "" })}
      />
      <TextField
        help={t("node.labelHelp")}
        label={t("node.label")}
        field="label"
        value={spec.label}
        onChange={(v) => update({ label: v })}
      />
      <TextField
        help={t("group.tooltipHelp")}
        label={t("group.tooltip")}
        field="tooltip"
        value={spec.tooltip}
        onChange={(v) => update({ tooltip: v })}
      />
      <SwitchField
        label={t("group.prefix")}
        help={t("group.prefixHelp")}
        checked={spec.prefix_group_id ?? true}
        onChange={(v) => update({ prefix_group_id: v })}
      />
      <ParentGroupSelect node={node} />
      <LoopEditor node={node} />
      <Hint>{t("group.help")}</Hint>
      {members.length ? (
        <Box>
          <Text fontSize="xs" fontWeight="medium">
            {t("group.members", { count: members.length })}
          </Text>
          <Stack gap={0} mt={1}>
            {members.map((m) => (
              <Text key={m.id} fontSize="xs" fontFamily="mono" color="fg.muted" truncate>
                {effectiveTaskId(nodes, m.id)}
              </Text>
            ))}
          </Stack>
        </Box>
      ) : null}
    </Stack>
  );
}

/** Airflow 3.4 Task Loops: repeat the group body until a condition holds. */
function LoopEditor({ node }: { node: TaskGroupFlowNode }) {
  const { t } = useTranslation("inspector");
  const update = useUpdate<"task_group">(node.id);
  const gate = useAirflowGate();
  const loop = node.data.spec.loop ?? null;
  const supported = gate.supports("3.4.0");
  const untilError = useFieldError(node.id, "loop.until_code");
  return (
    <Stack gap={2} borderWidth="1px" rounded="md" p={2}>
      <HStack justify="space-between">
        <SwitchField
          label={t("group.loop")}
          help={t("group.loopHelp")}
          field="loop"
          checked={!!loop}
          onChange={(v) => update({ loop: v ? { max_iterations: 5, until_name: "until_done" } : null })}
        />
        {!supported ? <VersionBadge min="3.4" /> : null}
      </HStack>
      {loop ? (
        <>
          <NumberField
            help={t("group.maxIterationsHelp")}
            label={t("group.maxIterations")}
            min={1}
            value={loop.max_iterations}
            onChange={(v) => update({ loop: { ...loop, max_iterations: Math.max(1, v ?? 1) } })}
          />
          <TextField
            help={t("group.untilNameHelp")}
            label={t("group.untilName")}
            mono
            value={loop.until_name ?? "until_done"}
            onChange={(v) => update({ loop: { ...loop, until_name: v || "until_done" } })}
          />
          <Field.Root invalid={!!untilError}>
            <Field.Label fontSize="xs">{t("group.untilCode")}</Field.Label>
            <Box w="100%" borderWidth="1px" rounded="md" overflow="hidden">
              <Box as="pre" fontFamily="mono" fontSize="xs" px={2} py={1} bg="bg.muted" color="fg.muted">
                {`def ${loop.until_name ?? "until_done"}(**context):\n    loop = context["loop"]`}
              </Box>
              <CodeEditor
                language="python"
                ariaLabel={t("group.untilCode")}
                minHeight="80px"
                placeholder="return loop.result >= 0.99"
                value={loop.until_code ?? ""}
                onChange={(code) => update({ loop: { ...loop, until_code: code || null } })}
              />
            </Box>
            {untilError ? (
              <Field.ErrorText>{untilError}</Field.ErrorText>
            ) : (
              <Field.HelperText fontSize="xs">{t("group.untilHelp")}</Field.HelperText>
            )}
          </Field.Root>
        </>
      ) : null}
    </Stack>
  );
}

const NOTE_COLORS = ["yellow", "orange", "red", "pink", "purple", "blue", "cyan", "teal", "green", "gray"];

export function NoteInspector({ node }: { node: NoteFlowNode }) {
  const { t } = useTranslation("inspector");
  const update = useUpdate<"note">(node.id);
  const spec = node.data.spec;
  const color = spec.color ?? "yellow";
  return (
    <Stack gap={2}>
      <TextField
        help={t("node.labelHelp")}
        label={t("node.label")}
        field="label"
        value={spec.label}
        onChange={(v) => update({ label: v })}
      />
      <Field.Root>
        <Field.Label fontSize="xs">{t("note.text")}</Field.Label>
        <Box w="100%">
          <CodeEditor
            language="markdown"
            ariaLabel={t("note.text")}
            minHeight="120px"
            value={spec.text ?? ""}
            onChange={(text) => update({ text })}
          />
        </Box>
      </Field.Root>
      <Field.Root>
        <Field.Label fontSize="xs" id={`${node.id}-color`}>
          {t("note.color")}
        </Field.Label>
        <HStack gap={1} wrap="wrap" role="radiogroup" aria-labelledby={`${node.id}-color`}>
          {NOTE_COLORS.map((c) => (
            <Box
              key={c}
              as="button"
              role="radio"
              aria-checked={color === c}
              aria-label={t(`note.colors.${c}`)}
              title={t(`note.colors.${c}`)}
              w="6"
              h="6"
              rounded="full"
              bg={`${c}.300`}
              borderWidth="2px"
              borderColor={color === c ? "fg" : "transparent"}
              cursor="pointer"
              _focusVisible={{ outline: "2px solid", outlineColor: "colorPalette.focusRing" }}
              onClick={() => update({ color: c })}
            />
          ))}
        </HStack>
      </Field.Root>
      <SwitchField
        label={t("note.includeInDocs")}
        help={t("note.includeInDocsHelp")}
        checked={!!spec.include_in_docs}
        onChange={(v) => update({ include_in_docs: v })}
      />
    </Stack>
  );
}

export function EdgeInspector() {
  const { t } = useTranslation("inspector");
  const edge = useBuilder((s) => s.edges.find((e) => e.selected));
  const nodes = useBuilder((s) => s.nodes);
  const setEdgeLabel = useBuilder((s) => s.setEdgeLabel);
  if (!edge) return null;
  const name = (id: string) => {
    const n = nodes.find((x) => x.id === id);
    return n ? nodeKey(n) : id;
  };
  return (
    <Stack gap={2} p={3}>
      <Heading size="sm">{t("edge.title")}</Heading>
      <Text fontSize="xs" fontFamily="mono" color="fg.muted">
        {name(edge.source)} → {name(edge.target)}
      </Text>
      <TextField
        label={t("edge.label")}
        help={t("edge.labelHelp")}
        value={typeof edge.label === "string" ? edge.label : ""}
        onChange={(v) => setEdgeLabel(edge.id, v ?? "")}
      />
    </Stack>
  );
}
