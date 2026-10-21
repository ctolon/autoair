import { Box, SegmentGroup, Stack, Text } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";

import type { MappingSpec } from "../../api/gen/types.gen";
import type { TaskFlowNode } from "../../lib/types";
import { AssetListEditor } from "../../widgets/assets/AssetRefEditor";
import { FIELD_ATTR, Hint, SectionTitle } from "../../widgets/fields";
import { TemplateField } from "../../widgets/TemplateField";
import { ArgsEditor, ValueInput } from "../../widgets/ValueEditors";
import { useFieldError, useUpdate } from "./shared";

type MappingMode = "none" | "expand" | "expand_kwargs";

function mappingMode(m: MappingSpec | null | undefined): MappingMode {
  if (!m) return "none";
  return m.expand_kwargs !== undefined && m.expand_kwargs !== null ? "expand_kwargs" : "expand";
}

/** Dynamic task mapping: `.expand(...)` / `.expand_kwargs(...)` + map_index_template. */
function MappingEditor({ node }: { node: TaskFlowNode }) {
  const { t } = useTranslation("inspector");
  const update = useUpdate<"operator">(node.id);
  const mapping = node.data.spec.mapping;
  const mode = mappingMode(mapping);
  const error = useFieldError(node.id, "mapping");
  const set = (next: MappingMode) => {
    if (next === mode) return;
    const tpl = mapping?.map_index_template ?? null;
    if (next === "none") update({ mapping: null });
    else if (next === "expand") update({ mapping: { expand: mapping?.expand ?? {}, map_index_template: tpl } });
    else update({ mapping: { expand_kwargs: [], map_index_template: tpl } });
  };
  return (
    <Stack gap={2} {...{ [FIELD_ATTR]: "mapping" }}>
      <Hint>{t("mapping.help")}</Hint>
      <SegmentGroup.Root size="xs" value={mode} onValueChange={(e) => set((e.value as MappingMode | null) ?? "none")}>
        <SegmentGroup.Indicator />
        <SegmentGroup.Items
          items={(["none", "expand", "expand_kwargs"] as const).map((m) => ({
            value: m,
            label: t(`mapping.modes.${m}`),
          }))}
        />
      </SegmentGroup.Root>
      {error ? <Hint tone="error">{error}</Hint> : null}
      {mode === "expand" && mapping ? (
        <ArgsEditor
          nodeId={node.id}
          title={t("mapping.expandOver")}
          fieldPrefix="mapping"
          requireList
          help={node.type === "python_task" ? t("mapping.expandHelpPython") : t("mapping.expandHelpOperator")}
          value={mapping.expand ?? {}}
          onChange={(expand) => update({ mapping: { ...mapping, expand } })}
        />
      ) : null}
      {mode === "expand_kwargs" && mapping ? (
        <Box>
          <Text fontSize="xs" fontWeight="medium" mb={1}>
            {t("mapping.expandKwargs")}
          </Text>
          <Hint>{t("mapping.expandKwargsHelp")}</Hint>
          <ValueInput
            nodeId={node.id}
            name="expand_kwargs"
            targetId="mapping.expand_kwargs"
            requireList
            value={mapping.expand_kwargs}
            onChange={(v) => update({ mapping: { ...mapping, expand_kwargs: v } })}
          />
        </Box>
      ) : null}
      {mapping ? (
        <TemplateField
          label={t("mapping.mapIndexTemplate")}
          help={t("mapping.mapIndexTemplateHelp")}
          field="mapping.map_index_template"
          compact
          placeholder="{{ task.op_kwargs['file'] }}"
          value={mapping.map_index_template ?? ""}
          onChange={(v) => update({ mapping: { ...mapping, map_index_template: v } })}
        />
      ) : null}
    </Stack>
  );
}

/** Mapping + data lineage (inlets / outlets assets). */
export function DataTab({ node }: { node: TaskFlowNode }) {
  const { t } = useTranslation("inspector");
  const update = useUpdate<"operator">(node.id);
  const spec = node.data.spec;
  return (
    <Stack gap={2}>
      <SectionTitle>{t("mapping.title")}</SectionTitle>
      <MappingEditor node={node} />
      <SectionTitle>{t("data.outlets")}</SectionTitle>
      <AssetListEditor
        label={t("data.outlets")}
        help={t("data.outletsHelp")}
        field="outlets"
        value={spec.outlets ?? []}
        onChange={(outlets) => update({ outlets })}
      />
      <SectionTitle>{t("data.inlets")}</SectionTitle>
      <AssetListEditor
        label={t("data.inlets")}
        help={t("data.inletsHelp")}
        field="inlets"
        value={spec.inlets ?? []}
        onChange={(inlets) => update({ inlets })}
      />
    </Stack>
  );
}
