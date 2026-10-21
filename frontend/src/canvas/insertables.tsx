import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { useTranslation } from "react-i18next";
import type { IconType } from "react-icons";
import {
  LuArrowLeftRight,
  LuBlocks,
  LuBot,
  LuBox,
  LuCode,
  LuGitBranch,
  LuLayers,
  LuMessageSquare,
  LuPackage,
  LuPackageCheck,
  LuPuzzle,
  LuRadar,
  LuStickyNote,
  LuTerminal,
  LuUserCheck,
} from "react-icons/lu";
import { useAccess } from "../lib/access";

import {
  listDecoratorsOptions,
  listNodeKindsOptions,
  listOperatorsOptions,
  listTemplatesOptions,
} from "../api/gen/@tanstack/react-query.gen";
import type { BlueprintSummary, OperatorSummary } from "../api/gen/types.gen";
import { type NodeSpec, PYTHON_DECORATORS, type PythonDecorator, isPythonDecorator } from "../lib/types";
import { useMeta } from "../lifecycle/hooks";
import { decoratorIcon } from "./icons";
import { useBuilder } from "../store/builder";
import { useBlueprintSummaries } from "./blueprints";
import {
  makeBlueprintSpec,
  makeCustomSpec,
  makeGroupSpec,
  makeNoteSpec,
  makeOperatorSpec,
  makePythonSpec,
} from "./factories";

/**
 * What inserting an item produces. Template payloads (fragments and operator templates) are
 * fetched (`getTemplate`) at insert time.
 */
export type InsertPayload = { kind: "spec"; spec: NodeSpec } | { kind: "fragment"; templateId: string };

export type InsertCategory =
  "builtin" | "operator" | "sensor" | "transfer" | "fragment" | "operatorTemplate" | "blueprint";

export type InsertItem = {
  /**
   * Stable key (favorites / recent): `op:<fqn>`, `py:<decorator>`, `builtin:<x>`, `custom:<name>`,
   * `fragment:<id>`, `optemplate:<id>`, `blueprint:<name>`.
   */
  key: string;
  label: string;
  hint: string;
  category: InsertCategory;
  provider?: string;
  /** Palette group (catalog.groups / package metadata); unset: grouped by `provider`. */
  group?: string | undefined;
  /** Sort key of the palette group (lower first; provider groups default to 100). */
  groupOrder?: number | undefined;
  deprecated?: boolean;
  /** Set when the builder cannot configure the item (shown, but not insertable). */
  disabledReason?: string | null;
  icon: IconType;
  /** Logo / custom icon (`builtin:…`, `custom:…`); `icon` is the fallback. */
  iconId?: string | null;
  keywords: string[];
  /** Built lazily so generated task ids are unique at insert time. */
  payload: () => InsertPayload;
};

const nodes = () => useBuilder.getState().nodes;

export const isHitlClass = (cls: string) => /^(HITL|Approval)/.test(cls);

function operatorIcon(op: Pick<OperatorSummary, "class_name" | "category">): IconType {
  if (isHitlClass(op.class_name)) return LuUserCheck;
  if (op.category === "sensor" || /Sensor$/.test(op.class_name)) return LuRadar;
  if (op.category === "transfer") return LuArrowLeftRight;
  return LuBox;
}

const providerName = (provider: string) => provider.replace(/^apache-airflow-providers-/, "");

/** Sort key of palette groups no catalog rule orders (catalog.default_group_order). */
const DEFAULT_GROUP_ORDER = 100;

type OperatorGroup = { id: string; title: string; order: number; items: InsertItem[] };

/**
 * Catalog operators by palette group: the group the catalog names (configuration or package
 * metadata), else the provider/package. Groups sort by order, then title; items keep their
 * input order (search ranking).
 */
export function operatorGroups(items: readonly InsertItem[]): OperatorGroup[] {
  const groups = new Map<string, OperatorGroup>();
  for (const item of items) {
    const title = item.group || item.provider || "";
    const order = item.groupOrder ?? DEFAULT_GROUP_ORDER;
    let g = groups.get(title);
    if (!g) {
      g = { id: item.group ? `group:${title}` : `provider:${title}`, title, order, items: [] };
      groups.set(title, g);
    } else if (order < g.order) g.order = order;
    g.items.push(item);
  }
  return [...groups.values()].sort((a, b) => a.order - b.order || a.title.localeCompare(b.title));
}

type Translate = (key: string, options?: Record<string, unknown>) => string;

/**
 * Why an operator cannot be inserted here, or null. Operators taking Python objects
 * (callables, classes) are configured with Python references, which need the server's
 * allow_python_code permission.
 */
export function unsupportedReason(
  op: OperatorSummary,
  t: Translate,
  opts: { pythonCodeAllowed?: boolean | undefined } = {},
): string | null {
  if (op.builder_supported === false) {
    const params = op.unsupported_params ?? [];
    return params.length ? t("palette.needsCallable", { params: params.join(", ") }) : (op.unsupported_reason ?? "");
  }
  if (op.requires_python_code && opts.pythonCodeAllowed === false) {
    return t("palette.needsPythonCode", { params: (op.python_ref_params ?? []).join(", ") });
  }
  return null;
}

/** `@task` for the plain decorator, `@task.<name>` otherwise. */
export const decoratorLabel = (d: PythonDecorator) => (d === "task" ? "@task" : `@task.${d}`);

/** The TaskFlow alternative (`@task.<decorator>` node) suggested for a callable operator. */
export function taskflowEquivalent(op: OperatorSummary): PythonDecorator | null {
  const d = op.equivalent_decorator;
  return d && isPythonDecorator(d) ? d : null;
}

export function operatorItem(
  op: OperatorSummary,
  t: Translate,
  opts: { pythonCodeAllowed?: boolean | undefined } = {},
): InsertItem {
  const equivalent = taskflowEquivalent(op);
  const refs = op.python_ref_params ?? [];
  let hint = op.summary || op.module;
  if (op.requires_python_code) {
    hint = equivalent
      ? t("palette.callableWithAlternative", { params: refs.join(", "), decorator: decoratorLabel(equivalent) })
      : t("palette.callableOperator", { params: refs.join(", ") });
  }
  return {
    key: `op:${op.fqn}`,
    label: op.class_name,
    hint,
    category: op.category,
    provider: providerName(op.provider),
    group: op.group ?? undefined,
    groupOrder: op.group_order ?? undefined,
    deprecated: !!op.deprecated,
    disabledReason: unsupportedReason(op, t, opts),
    icon: operatorIcon(op),
    iconId: op.icon ?? null,
    keywords: [
      op.fqn,
      op.provider,
      op.group ?? "",
      op.integration ?? "",
      op.summary ?? "",
      ...(equivalent ? [equivalent] : []),
    ].filter(Boolean),
    payload: () => ({ kind: "spec", spec: makeOperatorSpec(op, nodes()) }),
  };
}

/** Palette item of an Astronomer Blueprint (its newest version; the inspector switches versions). */
export function blueprintItem(bp: BlueprintSummary, t: Translate): InsertItem {
  const meta = [`v${bp.version}`, bp.package].filter(Boolean).join(" · ");
  return {
    key: `blueprint:${bp.name}`,
    label: bp.name,
    hint: [meta, bp.summary || bp.class_path].filter(Boolean).join(" — ") || t("insert.blueprintHint"),
    category: "blueprint",
    icon: LuPackage,
    keywords: [bp.class_path, bp.class_name, bp.package ?? "", "blueprint", "astronomer", "airflow-blueprint"].filter(
      Boolean,
    ),
    payload: () => ({ kind: "spec", spec: makeBlueprintSpec(bp, nodes()) }),
  };
}

const DECORATOR_ICONS: Partial<Record<PythonDecorator, IconType>> = {
  branch: LuGitBranch,
  short_circuit: LuGitBranch,
  branch_virtualenv: LuGitBranch,
  branch_external_python: LuGitBranch,
  sensor: LuRadar,
  bash: LuTerminal,
  llm: LuMessageSquare,
  agent: LuBot,
  llm_branch: LuGitBranch,
  llm_sql: LuMessageSquare,
};

/** Every insertable thing: built-ins, python decorator variants, catalog operators, template fragments. */
export function useInsertables() {
  const { t } = useTranslation("canvas");
  const operators = useQuery(listOperatorsOptions());
  const decorators = useQuery(listDecoratorsOptions());
  const kinds = useQuery(listNodeKindsOptions());
  const templatesOn = useAccess().feature("templates");
  const templates = useQuery({ ...listTemplatesOptions(), enabled: templatesOn, retry: false });

  const builtins = useMemo((): InsertItem[] => {
    const available = new Set<PythonDecorator>(["task"]);
    if (decorators.isError) {
      available.add("branch");
      available.add("short_circuit");
    }
    for (const d of decorators.data ?? []) if (isPythonDecorator(d.name)) available.add(d.name);
    const pythonItems = PYTHON_DECORATORS.filter((d) => available.has(d)).map((d): InsertItem => ({
      key: `py:${d}`,
      label: d === "task" ? t("insert.pythonTask") : `@task.${d}`,
      hint: t(`insert.decorators.${d}`),
      category: "builtin",
      icon: DECORATOR_ICONS[d] ?? LuCode,
      iconId: decoratorIcon(d),
      keywords: ["python", "task", "decorator", d],
      payload: () => ({ kind: "spec", spec: makePythonSpec(d, nodes()) }),
    }));
    return [
      ...pythonItems,
      {
        key: "builtin:task_group",
        label: t("insert.taskGroup"),
        hint: t("insert.taskGroupHint"),
        category: "builtin",
        icon: LuBlocks,
        keywords: ["group", "task_group"],
        payload: () => ({ kind: "spec", spec: makeGroupSpec(nodes()) }),
      },
      {
        key: "builtin:note",
        label: t("insert.note"),
        hint: t("insert.noteHint"),
        category: "builtin",
        icon: LuStickyNote,
        keywords: ["note", "sticky", "comment", "markdown"],
        payload: () => ({ kind: "spec", spec: makeNoteSpec() }),
      },
      ...(kinds.data ?? []).map((k): InsertItem => ({
        key: `custom:${k.name}`,
        label: k.title,
        hint: k.description,
        category: "builtin",
        icon: LuPuzzle,
        keywords: [k.name],
        payload: () => ({ kind: "spec", spec: makeCustomSpec(k.name, nodes()) }),
      })),
    ];
  }, [t, decorators.data, decorators.isError, kinds.data]);

  const pythonCodeAllowed = useMeta()?.allow_python_code;
  const operatorItems = useMemo(
    () => (operators.data ?? []).map((op) => operatorItem(op, t, { pythonCodeAllowed })),
    [operators.data, t, pythonCodeAllowed],
  );

  const fragments = useMemo(
    () =>
      (templates.data ?? [])
        .filter((tpl) => tpl.kind === "fragment")
        .map((tpl): InsertItem => ({
          key: `fragment:${tpl.id}`,
          label: tpl.title,
          hint: tpl.description || t("insert.fragmentHint"),
          category: "fragment",
          icon: LuLayers,
          keywords: [...(tpl.tags ?? []), tpl.category ?? "", "template", "fragment"].filter(Boolean),
          payload: () => ({ kind: "fragment", templateId: tpl.id }),
        })),
    [templates.data, t],
  );

  const operatorTemplates = useMemo(
    () =>
      (templates.data ?? [])
        .filter((tpl) => tpl.kind === "operator")
        .map((tpl): InsertItem => {
          const type = tpl.operator_class ?? "";
          const short = type.includes(".") && !type.startsWith("@") ? (type.split(".").pop() ?? type) : type;
          const meta = [short, tpl.owner_team, tpl.version ? `v${tpl.version}` : null].filter(Boolean).join(" · ");
          return {
            key: `optemplate:${tpl.id}`,
            label: tpl.title,
            hint: [meta, tpl.description].filter(Boolean).join(" — ") || t("insert.operatorTemplateHint"),
            category: "operatorTemplate",
            icon: LuPackageCheck,
            iconId: tpl.icon ?? null,
            keywords: [
              ...(tpl.tags ?? []),
              type,
              tpl.owner_team ?? "",
              tpl.category ?? "",
              "template",
              "blueprint",
              "operator template",
            ].filter(Boolean),
            payload: () => ({ kind: "fragment", templateId: tpl.id }),
          };
        }),
    [templates.data, t],
  );

  const blueprintQuery = useBlueprintSummaries();
  const blueprints = useMemo(
    () => (blueprintQuery.data ?? []).filter((bp) => bp.latest !== false).map((bp) => blueprintItem(bp, t)),
    [blueprintQuery.data, t],
  );

  return {
    builtins,
    operators: operatorItems,
    fragments,
    operatorTemplates,
    blueprints,
    operatorsQuery: operators,
    isPending: operators.isPending,
  };
}

const operatorIndexCache = new WeakMap<OperatorSummary[], Map<string, OperatorSummary>>();
function operatorIndex(data: OperatorSummary[]): Map<string, OperatorSummary> {
  let index = operatorIndexCache.get(data);
  if (!index) {
    index = new Map(data.map((o) => [o.fqn, o]));
    operatorIndexCache.set(data, index);
  }
  return index;
}

/** Catalog summary of one operator class (shared, cached catalog query). */
export function useOperatorSummary(fqn: string): OperatorSummary | undefined {
  const select = useCallback((data: OperatorSummary[]) => operatorIndex(data).get(fqn), [fqn]);
  return useQuery({ ...listOperatorsOptions(), select }).data;
}
