import { Badge, Box, HStack, IconButton, Input, Spinner, Stack, Tabs, Text } from "@chakra-ui/react";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  type DragEvent,
  type KeyboardEvent,
  type ReactNode,
  memo,
  useCallback,
  useDeferredValue,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { LuChevronDown, LuChevronRight, LuPackagePlus, LuStar } from "react-icons/lu";

import { errorMessage } from "../api/setup";
import { insertItem } from "../canvas/actions";
import { useCatalogPrefs } from "../canvas/catalogPrefs";
import { type InsertItem, operatorGroups, useInsertables } from "../canvas/insertables";
import { IconImage } from "../canvas/icons";
import { matchesQuery, rankItems, searchTerms } from "../canvas/search";
import { useAccess } from "../lib/access";
import { DND_MIME, FRAGMENT_MIME, encodeFragmentItem, encodePaletteItem } from "../lib/dnd";
import { useLifecycle } from "../lifecycle/store";

const TABS = ["all", "operators", "sensors", "transfers", "builtin", "favorites", "recent"] as const;
type Tab = (typeof TABS)[number];

type Section = { id: string; title: string; items: InsertItem[]; collapsible: boolean };

type Row =
  | { kind: "header"; section: Section }
  | { kind: "item"; item: InsertItem; index: number /* index in the flat item list */ };

const ROW_HEIGHT = 52;
const HEADER_HEIGHT = 30;

function startDrag(e: DragEvent, item: InsertItem) {
  const payload = item.payload();
  if (payload.kind === "fragment") {
    e.dataTransfer.setData(FRAGMENT_MIME, encodeFragmentItem({ templateId: payload.templateId, label: item.label }));
  } else {
    e.dataTransfer.setData(DND_MIME, encodePaletteItem({ spec: payload.spec, label: item.label, key: item.key }));
  }
  e.dataTransfer.effectAllowed = "copy";
}

const PaletteRow = memo(function PaletteRow({
  item,
  domId,
  active,
  favorite,
  index,
  onHover,
}: {
  item: InsertItem;
  domId: string;
  active: boolean;
  favorite: boolean;
  index: number;
  onHover: (index: number) => void;
}) {
  const { t } = useTranslation("canvas");
  const disabled = item.disabledReason != null;
  const Icon = item.icon;
  return (
    <HStack
      id={domId}
      role="option"
      aria-selected={active}
      aria-disabled={disabled || undefined}
      draggable={!disabled}
      onDragStart={(e) => startDrag(e, item)}
      onDoubleClick={disabled ? undefined : () => void insertItem(item)}
      onMouseEnter={() => onHover(index)}
      title={
        disabled
          ? t("palette.unsupported", { reason: item.disabledReason })
          : `${item.hint}\n\n${t("palette.dragHint")}`
      }
      opacity={disabled ? 0.5 : 1}
      cursor={disabled ? "not-allowed" : "grab"}
      px={2}
      py={1.5}
      rounded="md"
      bg={active ? "bg.emphasized" : undefined}
      _hover={{ bg: active ? "bg.emphasized" : "bg.muted" }}
      gap={2}
      className="group"
    >
      <Box flexShrink={0} color="fg.muted" aria-hidden>
        <IconImage id={item.iconId} size={18} fallback={<Icon size={15} />} />
      </Box>
      <Stack gap={0} minW={0} flex="1">
        <HStack gap={1} minW={0}>
          <Text fontSize="sm" truncate>
            {item.label}
          </Text>
          {item.deprecated ? (
            <Badge size="xs" colorPalette="orange" flexShrink={0}>
              {t("palette.deprecated")}
            </Badge>
          ) : null}
        </HStack>
        <Text fontSize="xs" color={disabled ? "fg.warning" : "fg.muted"} truncate>
          {disabled ? t("palette.unsupported", { reason: item.disabledReason }) : item.hint}
        </Text>
      </Stack>
      <IconButton
        size="2xs"
        variant="ghost"
        tabIndex={-1}
        aria-pressed={favorite}
        aria-label={
          favorite ? t("palette.unfavorite", { name: item.label }) : t("palette.favorite", { name: item.label })
        }
        title={favorite ? t("palette.unfavorite", { name: item.label }) : t("palette.favorite", { name: item.label })}
        color={favorite ? "yellow.500" : "fg.subtle"}
        opacity={favorite || active ? 1 : 0}
        _groupHover={{ opacity: 1 }}
        _focusVisible={{ opacity: 1 }}
        onClick={(e) => {
          e.stopPropagation();
          useCatalogPrefs.getState().toggleFavorite(item.key);
        }}
      >
        <LuStar fill={favorite ? "currentColor" : "none"} />
      </IconButton>
    </HStack>
  );
});

function SectionHeader({
  title,
  count,
  collapsible,
  collapsed,
  onToggle,
}: {
  title: string;
  count: number;
  collapsible: boolean;
  collapsed: boolean;
  onToggle: () => void;
}) {
  const content: ReactNode = (
    <>
      {collapsible ? collapsed ? <LuChevronRight size={12} /> : <LuChevronDown size={12} /> : null}
      <Text fontSize="xs" fontWeight="bold" textTransform="uppercase" color="fg.muted" letterSpacing="wide" truncate>
        {title}
      </Text>
      <Badge size="xs">{count}</Badge>
    </>
  );
  return collapsible ? (
    <HStack
      as="button"
      w="100%"
      px={2}
      py={1}
      gap={1.5}
      rounded="sm"
      color="fg.muted"
      cursor="pointer"
      _hover={{ bg: "bg.muted" }}
      aria-expanded={!collapsed}
      onClick={onToggle}
    >
      {content}
    </HStack>
  ) : (
    <HStack px={2} py={1} gap={1.5}>
      {content}
    </HStack>
  );
}

export function Palette() {
  const { t } = useTranslation("canvas");
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<Tab>("all");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [active, setActive] = useState(0);
  const ins = useInsertables();
  const favorites = useCatalogPrefs((s) => s.favorites);
  const recent = useCatalogPrefs((s) => s.recent);
  const listId = useId();
  const canWriteTemplates = useAccess().allows("templates", "template:write");

  // Typing stays responsive: filtering/rendering >1000 operators follows at lower priority.
  const deferredQuery = useDeferredValue(query);
  const sections = useMemo((): Section[] => {
    const terms = searchTerms(deferredQuery);
    const matches = (i: InsertItem) => matchesQuery(i, terms);
    const all = [...ins.builtins, ...ins.operatorTemplates, ...ins.blueprints, ...ins.fragments, ...ins.operators];
    const byKey = new Map(all.map((i) => [i.key, i]));
    const pick = (keys: string[]) => keys.map((k) => byKey.get(k)).filter((i): i is InsertItem => !!i && matches(i));
    if (tab === "favorites")
      return [{ id: "favorites", title: t("palette.tabs.favorites"), items: pick(favorites), collapsible: false }];
    if (tab === "recent")
      return [{ id: "recent", title: t("palette.tabs.recent"), items: pick(recent), collapsible: false }];

    const out: Section[] = [];
    if (tab === "all" || tab === "builtin") {
      out.push({
        id: "builtin",
        title: t("palette.builtin"),
        items: ins.builtins.filter(matches),
        collapsible: false,
      });
      out.push({
        id: "operatorTemplates",
        title: t("palette.operatorTemplates"),
        items: ins.operatorTemplates.filter(matches),
        collapsible: true,
      });
      out.push({
        id: "blueprints",
        title: t("palette.blueprints"),
        items: ins.blueprints.filter(matches),
        collapsible: true,
      });
      out.push({
        id: "fragments",
        title: t("palette.fragments"),
        items: ins.fragments.filter(matches),
        collapsible: true,
      });
    }
    if (tab !== "builtin") {
      const category =
        tab === "sensors" ? "sensor" : tab === "transfers" ? "transfer" : tab === "operators" ? "operator" : null;
      const candidates = category ? ins.operators.filter((op) => op.category === category) : ins.operators;
      // Catalog groups (catalog.groups / package metadata) or providers, ordered; best
      // matches first within each group.
      for (const g of operatorGroups(terms.length ? rankItems(candidates, deferredQuery) : candidates)) {
        out.push({ id: g.id, title: g.title, items: g.items, collapsible: true });
      }
    }
    return out.filter((s) => s.items.length);
  }, [
    ins.builtins,
    ins.operatorTemplates,
    ins.blueprints,
    ins.fragments,
    ins.operators,
    tab,
    deferredQuery,
    favorites,
    recent,
    t,
  ]);

  // Searching expands everything; otherwise collapsed sections hide their rows.
  const searching = query.trim().length > 0;
  const isCollapsed = (id: string) => !searching && collapsed.has(id);
  const { flat, rows, rowOfItem } = useMemo(() => {
    const flat: InsertItem[] = [];
    const rows: Row[] = [];
    const rowOfItem: number[] = [];
    for (const section of sections) {
      rows.push({ kind: "header", section });
      if (!searching && collapsed.has(section.id)) continue;
      for (const item of section.items) {
        rowOfItem.push(rows.length);
        rows.push({ kind: "item", item, index: flat.length });
        flat.push(item);
      }
    }
    return { flat, rows, rowOfItem };
  }, [sections, collapsed, searching]);
  const activeIndex = Math.min(active, Math.max(0, flat.length - 1));
  const favoriteSet = useMemo(() => new Set(favorites), [favorites]);
  const optionId = (key: string) => `${listId}-${key.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
  const activeItem = flat[activeIndex];
  const onHover = useCallback((i: number) => setActive(i), []);

  // Only the visible rows are mounted (>1200 operators with every provider installed).
  const scrollRef = useRef<HTMLDivElement>(null);
  // eslint-disable-next-line react-hooks/incompatible-library -- virtualizer state is read during render by design
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (i) => (rows[i]?.kind === "header" ? HEADER_HEIGHT : ROW_HEIGHT),
    overscan: 12,
  });
  const activeRow = rowOfItem[activeIndex];
  useEffect(() => {
    if (activeRow !== undefined) virtualizer.scrollToIndex(activeRow, { align: "auto" });
  }, [activeRow, virtualizer]);

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (!flat.length) return;
    if (e.key === "ArrowDown") setActive((i) => Math.min(flat.length - 1, Math.min(i, flat.length - 1) + 1));
    else if (e.key === "ArrowUp") setActive((i) => Math.max(0, Math.min(i, flat.length - 1) - 1));
    else if (e.key === "Home") setActive(0);
    else if (e.key === "End") setActive(flat.length - 1);
    else if (e.key === "Enter" && activeItem) void insertItem(activeItem);
    else if (e.key === "s" && e.altKey && activeItem) useCatalogPrefs.getState().toggleFavorite(activeItem.key);
    else return;
    e.preventDefault();
  };

  const toggle = (id: string) =>
    setCollapsed((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <Stack h="100%" gap={2} p={2} overflow="hidden">
      <HStack gap={1}>
        <Input
          size="sm"
          flex="1"
          placeholder={t("palette.search")}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
          role="combobox"
          aria-expanded
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={activeItem ? optionId(activeItem.key) : undefined}
          aria-label={t("palette.search")}
        />
        {canWriteTemplates ? (
          <IconButton
            size="sm"
            variant="ghost"
            aria-label={t("palette.newOperatorTemplate")}
            title={t("palette.newOperatorTemplate")}
            data-testid="palette-new-operator-template"
            onClick={() => useLifecycle.getState().openDialog({ kind: "operatorTemplateEditor", mode: "new" })}
          >
            <LuPackagePlus />
          </IconButton>
        ) : null}
      </HStack>
      <Tabs.Root
        size="sm"
        variant="subtle"
        value={tab}
        onValueChange={(e) => {
          setTab(e.value as Tab);
          setActive(0);
        }}
        activationMode="automatic"
      >
        <Tabs.List flexWrap="wrap" gap={0.5}>
          {TABS.map((v) => (
            <Tabs.Trigger key={v} value={v} px={1.5} h={6} fontSize="xs">
              {t(`palette.tabs.${v}`)}
            </Tabs.Trigger>
          ))}
        </Tabs.List>
      </Tabs.Root>
      <Box ref={scrollRef} overflowY="auto" flex="1" id={listId} role="listbox" aria-label={t(`palette.tabs.${tab}`)}>
        <Box position="relative" h={`${virtualizer.getTotalSize()}px`} w="100%">
          {virtualizer.getVirtualItems().map((v) => {
            const row = rows[v.index];
            if (!row) return null;
            return (
              <Box
                key={v.key}
                data-index={v.index}
                ref={virtualizer.measureElement}
                position="absolute"
                top={0}
                left={0}
                w="100%"
                style={{ transform: `translateY(${v.start}px)` }}
                role={row.kind === "header" ? "presentation" : undefined}
              >
                {row.kind === "header" ? (
                  <SectionHeader
                    title={row.section.title}
                    count={row.section.items.length}
                    collapsible={row.section.collapsible && !searching}
                    collapsed={isCollapsed(row.section.id)}
                    onToggle={() => toggle(row.section.id)}
                  />
                ) : (
                  <PaletteRow
                    item={row.item}
                    domId={optionId(row.item.key)}
                    active={row.index === activeIndex}
                    favorite={favoriteSet.has(row.item.key)}
                    index={row.index}
                    onHover={onHover}
                  />
                )}
              </Box>
            );
          })}
        </Box>
        {ins.isPending ? <Spinner size="sm" m={3} /> : null}
        {ins.operatorsQuery.isError ? (
          <Text fontSize="xs" color="fg.error" p={2}>
            {errorMessage(ins.operatorsQuery.error)}
          </Text>
        ) : null}
        {!ins.isPending && !sections.length ? (
          <Text fontSize="xs" color="fg.muted" p={3} textAlign="center">
            {tab === "favorites" && !query
              ? t("palette.noFavorites")
              : tab === "recent" && !query
                ? t("palette.noRecent")
                : t("palette.empty")}
          </Text>
        ) : null}
      </Box>
      {/* One line (truncated with a tooltip in a narrow palette), readable contrast. */}
      <Text
        fontSize="xs"
        color="fg"
        opacity={0.8}
        px={1}
        pt={1}
        borderTopWidth="1px"
        whiteSpace="nowrap"
        truncate
        title={t("palette.footer")}
        data-testid="palette-footer"
      >
        {t("palette.footer")}
      </Text>
    </Stack>
  );
}
