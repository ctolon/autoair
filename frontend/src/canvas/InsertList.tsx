import { Badge, Box, HStack, Spinner, Text } from "@chakra-ui/react";
import { Command } from "cmdk";
import { useDeferredValue, useMemo } from "react";
import { useTranslation } from "react-i18next";

import { useCatalogPrefs } from "./catalogPrefs";
import { IconImage } from "./icons";
import { type InsertItem, useInsertables } from "./insertables";
import { rankItems } from "./search";

const MAX_OPERATOR_RESULTS = 50;

/** Chakra styling for cmdk's unstyled parts (attribute selectors). */
export const cmdkCss = {
  "& [cmdk-input]": {
    width: "100%",
    border: "none",
    outline: "none",
    bg: "transparent",
    fontSize: "sm",
    px: "3",
    py: "2.5",
    borderBottomWidth: "1px",
    borderColor: "border.muted",
    color: "fg",
  },
  "& [cmdk-list]": { overflowY: "auto", overscrollBehavior: "contain", p: "1" },
  "& [cmdk-item]": {
    display: "flex",
    alignItems: "center",
    gap: "2",
    px: "2",
    py: "1.5",
    rounded: "md",
    cursor: "pointer",
    fontSize: "sm",
    userSelect: "none",
  },
  "& [cmdk-item][data-selected=true]": { bg: "bg.emphasized" },
  "& [cmdk-item][data-disabled=true]": { opacity: 0.45, cursor: "not-allowed" },
  "& [cmdk-group-heading]": {
    px: "2",
    pt: "2",
    pb: "1",
    fontSize: "xs",
    fontWeight: "bold",
    color: "fg.muted",
    textTransform: "uppercase",
    letterSpacing: "wide",
  },
  "& [cmdk-empty]": { px: "3", py: "4", fontSize: "sm", color: "fg.muted", textAlign: "center" },
} as const;

function InsertRow({ item }: { item: InsertItem }) {
  const { t } = useTranslation("canvas");
  const Icon = item.icon;
  return (
    <>
      <Box color="fg.muted" flexShrink={0} aria-hidden>
        <IconImage id={item.iconId} size={18} fallback={<Icon size={15} />} />
      </Box>
      <Box minW={0} flex="1">
        <HStack gap={1}>
          <Text truncate>{item.label}</Text>
          {item.deprecated ? (
            <Badge size="xs" colorPalette="orange">
              {t("palette.deprecated")}
            </Badge>
          ) : null}
        </HStack>
        <Text fontSize="xs" color="fg.muted" truncate>
          {item.disabledReason != null ? t("palette.unsupported", { reason: item.disabledReason }) : item.hint}
        </Text>
      </Box>
      {item.group || item.provider ? (
        <Text fontSize="xs" color="fg.subtle" flexShrink={0}>
          {item.group || item.provider}
        </Text>
      ) : null}
    </>
  );
}

type Props = {
  search: string;
  onPick: (item: InsertItem) => void;
  /** Hide items that cannot be inserted in this context (e.g. notes on an edge). */
  filter?: (item: InsertItem) => boolean;
  /** Include template fragments. */
  fragments?: boolean;
};

/**
 * cmdk groups listing everything insertable. Catalog operators (often >1000) are only rendered
 * once the user types, and then only the best-ranked matches.
 */
export function InsertGroups({ search, onPick, filter = () => true, fragments = true }: Props) {
  const { t } = useTranslation("canvas");
  const ins = useInsertables();
  const favorites = useCatalogPrefs((s) => s.favorites);
  const recent = useCatalogPrefs((s) => s.recent);

  const all = useMemo(
    () =>
      [
        ...ins.builtins,
        ...ins.operatorTemplates,
        ...ins.blueprints,
        ...(fragments ? ins.fragments : []),
        ...ins.operators,
      ].filter(filter),
    [ins.builtins, ins.operatorTemplates, ins.blueprints, ins.fragments, ins.operators, fragments, filter],
  );
  const byKey = useMemo(() => new Map(all.map((i) => [i.key, i])), [all]);
  const pinned = useMemo(() => {
    const keys = [...new Set([...favorites, ...recent])];
    return keys.map((k) => byKey.get(k)).filter((i): i is InsertItem => !!i);
  }, [favorites, recent, byKey]);
  const pinnedKeys = new Set(pinned.map((i) => i.key));
  const typing = search.trim().length > 0;
  // Only the best matches are mounted: cmdk scores every rendered item on each keystroke.
  const deferredSearch = useDeferredValue(search);
  const operatorMatches = useMemo(
    () => (typing ? rankItems(ins.operators.filter(filter), deferredSearch, MAX_OPERATOR_RESULTS) : []),
    [typing, ins.operators, filter, deferredSearch],
  );

  const item = (i: InsertItem, prefix = "") => (
    <Command.Item
      key={prefix + i.key}
      value={prefix + i.key}
      keywords={[i.label, i.hint, ...i.keywords]}
      disabled={i.disabledReason != null}
      onSelect={() => onPick(i)}
    >
      <InsertRow item={i} />
    </Command.Item>
  );

  return (
    <>
      {!typing && pinned.length ? (
        <Command.Group heading={t("palette.favoritesAndRecent")}>{pinned.map((i) => item(i, "pin:"))}</Command.Group>
      ) : null}
      <Command.Group heading={t("palette.builtin")}>
        {ins.builtins
          .filter(filter)
          .filter((i) => typing || !pinnedKeys.has(i.key))
          .map((i) => item(i))}
      </Command.Group>
      {ins.operatorTemplates.some(filter) ? (
        <Command.Group heading={t("palette.operatorTemplates")}>
          {ins.operatorTemplates
            .filter(filter)
            .filter((i) => typing || !pinnedKeys.has(i.key))
            .map((i) => item(i))}
        </Command.Group>
      ) : null}
      {ins.blueprints.some(filter) ? (
        <Command.Group heading={t("palette.blueprints")}>
          {ins.blueprints
            .filter(filter)
            .filter((i) => typing || !pinnedKeys.has(i.key))
            .map((i) => item(i))}
        </Command.Group>
      ) : null}
      {fragments && ins.fragments.some(filter) ? (
        <Command.Group heading={t("palette.fragments")}>
          {ins.fragments.filter(filter).map((i) => item(i))}
        </Command.Group>
      ) : null}
      {typing ? (
        <Command.Group heading={t("palette.operators")}>{operatorMatches.map((i) => item(i))}</Command.Group>
      ) : null}
      {typing && ins.isPending ? (
        <Command.Loading>
          <HStack px={3} py={2}>
            <Spinner size="xs" />
            <Text fontSize="xs">{t("palette.loading")}</Text>
          </HStack>
        </Command.Loading>
      ) : null}
    </>
  );
}
