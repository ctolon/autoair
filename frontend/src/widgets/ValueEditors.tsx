import { Box, Button, HStack, IconButton, Input, NativeSelect, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuPlus, LuTrash2 } from "react-icons/lu";

import { hasJinja } from "../lib/formSchema";
import { type XComRef, isXComRef } from "../lib/types";
import { FIELD_ATTR, Hint, JsonField, SectionTitle } from "./fields";
import { TemplateField } from "./TemplateField";
import { useUpstreamOutputs } from "./upstream";
import { XComChip } from "./XComChip";
import { useInsertTarget, useLatest, useXcomDrop, xcomRef, xcomTemplate } from "./xcomDnd";

const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Value that is a JSON literal, a Jinja template, or the output (XCom) of an upstream task. */
export function ValueInput({
  nodeId,
  name,
  value,
  onChange,
  requireList,
  targetId,
}: {
  nodeId: string;
  name: string;
  value: unknown;
  onChange: (v: unknown) => void;
  requireList?: boolean;
  targetId: string;
}) {
  const { t } = useTranslation("inspector");
  const upstream = useUpstreamOutputs(nodeId);
  const setTarget = useInsertTarget((s) => s.setTarget);
  const latestChange = useLatest(onChange);
  const isRef = isXComRef(value);
  const isTemplate = !isRef && hasJinja(value);
  const [forceTemplate, setForceTemplate] = useState(false);
  const mode = isRef ? "xcom" : isTemplate || forceTemplate ? "template" : "literal";
  const { isOver, dropProps } = useXcomDrop((p) => {
    if (mode === "template") onChange(`${typeof value === "string" ? value : ""}${xcomTemplate(p)}`);
    else onChange(xcomRef(p));
  });
  return (
    <Stack
      gap={1}
      flex="1"
      minW={0}
      {...dropProps}
      rounded="md"
      outline={isOver ? "2px dashed" : undefined}
      outlineColor="teal.500"
      onFocusCapture={() => {
        if (mode === "literal") setTarget({ id: targetId, label: name, setXcom: latestChange });
      }}
    >
      <NativeSelect.Root size="xs">
        <NativeSelect.Field
          aria-label={t("args.sourceOf", { name })}
          value={isRef ? `xcom:${value.node}` : mode}
          onChange={(e) => {
            const v = e.target.value;
            setForceTemplate(v === "template");
            if (v === "literal") onChange(requireList ? [] : "");
            else if (v === "template") onChange(typeof value === "string" ? value : "");
            else onChange({ $type: "xcom", node: v.slice(5) } satisfies XComRef);
          }}
        >
          <option value="literal">{requireList ? t("args.literalList") : t("args.literal")}</option>
          <option value="template">{t("args.template")}</option>
          {upstream.map((u) => (
            <option key={u.node.id} value={`xcom:${u.node.id}`}>
              {t("args.outputOf", { task: u.label })}
            </option>
          ))}
        </NativeSelect.Field>
        <NativeSelect.Indicator />
      </NativeSelect.Root>
      {isRef ? (
        <HStack gap={1} wrap="wrap">
          <XComChip value={value} onRemove={() => onChange(requireList ? [] : "")} />
          <Input
            size="2xs"
            w="140px"
            fontFamily="mono"
            aria-label={t("params.xcomKeyLabel", { name })}
            placeholder={t("args.keyPlaceholder")}
            list={`${targetId}-keys`}
            value={value.key ?? ""}
            onChange={(e) => onChange({ ...value, key: e.target.value || null })}
          />
          <datalist id={`${targetId}-keys`}>
            {(upstream.find((u) => u.node.id === value.node)?.keys ?? []).map((k) => (
              <option key={k} value={k} />
            ))}
          </datalist>
        </HStack>
      ) : mode === "template" ? (
        <TemplateField
          label={name}
          hideLabel
          compact
          targetId={targetId}
          value={typeof value === "string" ? value : ""}
          onChange={(v) => onChange(v ?? "")}
        />
      ) : (
        <JsonField
          label={name}
          hideLabel
          rows={2}
          value={value}
          onChange={onChange}
          validate={(v) => (requireList && !Array.isArray(v) ? t("args.mustBeList") : undefined)}
        />
      )}
      {!upstream.length && !isRef ? <Hint>{t("args.connectUpstream")}</Hint> : null}
    </Stack>
  );
}

/** Keyword-argument editor (python_task arguments, mapping expand). */
export function ArgsEditor({
  nodeId,
  title,
  value,
  onChange,
  requireList,
  help,
  fieldPrefix,
}: {
  nodeId: string;
  title: string;
  fieldPrefix?: string;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
  requireList?: boolean;
  help?: string;
}) {
  const { t } = useTranslation("inspector");
  const [newName, setNewName] = useState("");
  const entries = Object.entries(value);
  const add = () => {
    if (!IDENT.test(newName) || newName in value) return;
    onChange({ ...value, [newName]: requireList ? [] : "" });
    setNewName("");
  };
  return (
    <Box {...(fieldPrefix ? { [FIELD_ATTR]: fieldPrefix } : {})}>
      <SectionTitle>{title}</SectionTitle>
      {help ? (
        <Text fontSize="xs" color="fg.muted" mb={1}>
          {help}
        </Text>
      ) : null}
      <Stack gap={2}>
        {entries.map(([name, v]) => (
          <HStack key={name} align="start" gap={1}>
            <Text fontFamily="mono" fontSize="xs" pt={1.5} minW="80px" maxW="120px" truncate title={name}>
              {name}
            </Text>
            <ValueInput
              nodeId={nodeId}
              name={name}
              targetId={`${fieldPrefix ?? title}.${name}`}
              value={v}
              requireList={requireList ?? false}
              onChange={(nv) => onChange({ ...value, [name]: nv })}
            />
            <IconButton
              aria-label={t("common.removeItem", { item: name })}
              size="xs"
              variant="ghost"
              onClick={() => {
                const { [name]: _removed, ...rest } = value;
                onChange(rest);
              }}
            >
              <LuTrash2 />
            </IconButton>
          </HStack>
        ))}
        <HStack gap={1}>
          <Input
            size="xs"
            fontFamily="mono"
            placeholder="argument_name"
            aria-label={t("args.newName")}
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && add()}
          />
          <Button size="xs" variant="outline" onClick={add} disabled={!IDENT.test(newName) || newName in value}>
            <LuPlus /> {t("common.add")}
          </Button>
        </HStack>
      </Stack>
    </Box>
  );
}
