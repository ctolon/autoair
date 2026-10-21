import { Badge, Box, HStack, IconButton, Menu, NativeSelect, Portal, Stack, Text } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";
import { LuBraces, LuDatabase, LuLink, LuPlus, LuTrash2, LuUngroup } from "react-icons/lu";

import { type AssetExpr, type AssetExprNode, aliasLeaf, assetLeaf, combine } from "../../lib/schedule";
import { FIELD_ATTR, TextField } from "../fields";
import { AssetRefEditor } from "./AssetRefEditor";

type NodeProps = {
  node: AssetExprNode;
  onChange: (n: AssetExprNode) => void;
  /** Absent for the root (it can be wrapped / unwrapped but not removed). */
  onRemove?: (() => void) | undefined;
  depth: number;
};

function AddMenu({ onAdd, label }: { onAdd: (n: AssetExprNode) => void; label: string }) {
  const { t } = useTranslation("inspector");
  return (
    <Menu.Root>
      <Menu.Trigger asChild>
        <IconButton aria-label={label} size="2xs" variant="outline">
          <LuPlus />
        </IconButton>
      </Menu.Trigger>
      <Portal>
        <Menu.Positioner>
          <Menu.Content>
            <Menu.Item value="asset" onClick={() => onAdd(assetLeaf(""))}>
              <LuDatabase /> {t("assets.expr.addAsset")}
            </Menu.Item>
            <Menu.Item value="alias" onClick={() => onAdd(aliasLeaf(""))}>
              <LuLink /> {t("assets.expr.addAlias")}
            </Menu.Item>
            <Menu.Item value="all" onClick={() => onAdd(combine("all", [assetLeaf("")]))}>
              <LuBraces /> {t("assets.expr.addAllGroup")}
            </Menu.Item>
            <Menu.Item value="any" onClick={() => onAdd(combine("any", [assetLeaf("")]))}>
              <LuBraces /> {t("assets.expr.addAnyGroup")}
            </Menu.Item>
          </Menu.Content>
        </Menu.Positioner>
      </Portal>
    </Menu.Root>
  );
}

function NodeActions({ node, onChange, onRemove }: Omit<NodeProps, "depth">) {
  const { t } = useTranslation("inspector");
  return (
    <HStack gap={0}>
      <IconButton
        aria-label={t("assets.expr.wrap")}
        title={t("assets.expr.wrap")}
        size="2xs"
        variant="ghost"
        onClick={() => onChange(combine("all", [node]))}
      >
        <LuBraces />
      </IconButton>
      {node.op !== "asset" && node.op !== "alias" && node.items.length === 1 ? (
        <IconButton
          aria-label={t("assets.expr.unwrap")}
          title={t("assets.expr.unwrap")}
          size="2xs"
          variant="ghost"
          onClick={() => node.items[0] && onChange(node.items[0])}
        >
          <LuUngroup />
        </IconButton>
      ) : null}
      {onRemove ? (
        <IconButton
          aria-label={t("assets.expr.remove")}
          size="2xs"
          variant="ghost"
          colorPalette="red"
          onClick={onRemove}
        >
          <LuTrash2 />
        </IconButton>
      ) : null}
    </HStack>
  );
}

function ExprNode({ node, onChange, onRemove, depth }: NodeProps) {
  const { t } = useTranslation("inspector");
  if (node.op === "asset") {
    return (
      <Box borderWidth="1px" rounded="md" p={2} role="group" aria-label={t("assets.expr.asset")}>
        <HStack justify="space-between" mb={1}>
          <HStack gap={1}>
            <LuDatabase aria-hidden />
            <Badge size="xs" colorPalette="blue">
              {t("assets.expr.asset")}
            </Badge>
            <Text fontSize="xs" fontFamily="mono" truncate>
              {node.asset.name || node.asset.uri}
            </Text>
          </HStack>
          <NodeActions node={node} onChange={onChange} onRemove={onRemove} />
        </HStack>
        <AssetRefEditor value={node.asset} onChange={(asset) => onChange({ op: "asset", asset })} />
      </Box>
    );
  }
  if (node.op === "alias") {
    return (
      <Box borderWidth="1px" rounded="md" p={2} role="group" aria-label={t("assets.expr.alias")}>
        <HStack justify="space-between" mb={1}>
          <HStack gap={1}>
            <LuLink aria-hidden />
            <Badge size="xs" colorPalette="purple">
              {t("assets.expr.alias")}
            </Badge>
          </HStack>
          <NodeActions node={node} onChange={onChange} onRemove={onRemove} />
        </HStack>
        <TextField
          label={t("assets.expr.aliasName")}
          help={t("assets.expr.aliasHelp")}
          mono
          required
          value={node.name}
          onChange={(v) => onChange({ op: "alias", name: v ?? "" })}
        />
      </Box>
    );
  }
  const items = node.items;
  const label = node.op === "all" ? t("assets.expr.allOf") : t("assets.expr.anyOf");
  return (
    <Box
      borderWidth="1px"
      borderStyle="dashed"
      rounded="md"
      p={2}
      bg={depth % 2 ? "bg.subtle" : undefined}
      role="group"
      aria-label={label}
    >
      <HStack justify="space-between" mb={2}>
        <HStack gap={1}>
          <NativeSelect.Root size="xs" width="auto">
            <NativeSelect.Field
              aria-label={t("assets.expr.operator")}
              value={node.op}
              onChange={(e) => onChange({ op: e.target.value === "any" ? "any" : "all", items })}
            >
              <option value="all">{t("assets.expr.all")}</option>
              <option value="any">{t("assets.expr.any")}</option>
            </NativeSelect.Field>
            <NativeSelect.Indicator />
          </NativeSelect.Root>
          <Text fontSize="xs" color="fg.muted">
            {node.op === "all" ? t("assets.expr.allHelp") : t("assets.expr.anyHelp")}
          </Text>
        </HStack>
        <HStack gap={1}>
          <AddMenu
            label={t("assets.expr.addTo", { group: label })}
            onAdd={(n) => onChange({ op: node.op, items: [...items, n] })}
          />
          <NodeActions node={node} onChange={onChange} onRemove={onRemove} />
        </HStack>
      </HStack>
      <Stack gap={2} pl={2} borderLeftWidth="2px" borderColor={node.op === "all" ? "blue.400" : "orange.400"}>
        {items.map((child, i) => (
          <Box key={i}>
            {i > 0 ? (
              <Text fontSize="2xs" fontWeight="bold" color="fg.muted" textTransform="uppercase" mb={1}>
                {node.op === "all" ? t("assets.expr.and") : t("assets.expr.or")}
              </Text>
            ) : null}
            <ExprNode
              node={child}
              depth={depth + 1}
              onChange={(n) => onChange({ op: node.op, items: items.map((x, j) => (j === i ? n : x)) })}
              onRemove={
                items.length > 1 ? () => onChange({ op: node.op, items: items.filter((_, j) => j !== i) }) : undefined
              }
            />
          </Box>
        ))}
      </Stack>
    </Box>
  );
}

/** Tree editor for asset conditions: assets, aliases and nested ALL (&) / ANY (|) groups. */
export function AssetExprEditor({
  value,
  onChange,
  field,
}: {
  value: AssetExpr;
  onChange: (v: AssetExpr) => void;
  field?: string;
}) {
  const { t } = useTranslation("inspector");
  return (
    <Stack gap={1} {...(field ? { [FIELD_ATTR]: field } : {})}>
      <Text fontSize="xs" fontWeight="medium">
        {t("assets.expr.title")}
      </Text>
      <ExprNode node={value} depth={0} onChange={onChange} />
    </Stack>
  );
}
