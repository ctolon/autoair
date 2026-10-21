import { Badge, HStack, Stack, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { LuLock, LuPackageCheck } from "react-icons/lu";

import { getTemplateOptions } from "../../api/gen/@tanstack/react-query.gen";
import type { Template, TemplateRef } from "../../api/gen/types.gen";
import { lockMap, requiredPaths } from "../../canvas/operatorTemplates";
import { isTaskNode } from "../../lib/types";
import { useBuilder } from "../../store/builder";

/** The operator template a task was inserted from (null for other tasks). */
function useTemplateRef(nodeId: string): TemplateRef | null {
  return useBuilder((s) => {
    const n = s.nodes.find((x) => x.id === nodeId);
    return n && isTaskNode(n) ? (n.data.spec.template_ref ?? null) : null;
  });
}

/** The node's operator template and its locked values (path → template value). */
export function useNodeTemplate(nodeId: string): {
  ref: TemplateRef | null;
  template: Template | undefined;
  locks: Map<string, unknown>;
} {
  const ref = useTemplateRef(nodeId);
  const query = useQuery({
    ...getTemplateOptions({ path: { template_id: ref?.id ?? "_" } }),
    enabled: !!ref,
    retry: false,
    staleTime: 60_000,
  });
  const template = ref ? query.data : undefined;
  const locks = useMemo(() => lockMap(template), [template]);
  return { ref, template, locks };
}

/** Header of a task created from an operator template: origin, version and read-only fields. */
export function TemplateBanner({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation("inspector");
  const { ref, template, locks } = useNodeTemplate(nodeId);
  if (!ref) return null;
  const required = template ? requiredPaths(template) : [];
  const meta = [template?.owner_team, ref.version ? `v${ref.version}` : null].filter(Boolean).join(" · ");
  return (
    <Stack
      gap={1}
      borderWidth="1px"
      borderColor="border.info"
      bg="bg.info"
      rounded="md"
      p={2}
      data-testid="template-banner"
      role="note"
    >
      <HStack gap={1.5} minW={0}>
        <LuPackageCheck size={14} aria-hidden />
        <Text fontSize="xs" fontWeight="semibold" truncate>
          {t("template.from", { title: template?.title ?? ref.id })}
        </Text>
        {meta ? (
          <Text fontSize="xs" color="fg.muted" flexShrink={0}>
            {meta}
          </Text>
        ) : null}
        {template?.version && ref.version && template.version !== ref.version ? (
          <Badge size="xs" colorPalette="orange" flexShrink={0}>
            {t("template.updated", { version: template.version })}
          </Badge>
        ) : null}
      </HStack>
      {locks.size ? (
        <HStack gap={1} wrap="wrap" title={t("template.lockedHelp")}>
          <LuLock size={11} aria-label={t("template.locked")} />
          {[...locks.keys()].map((path) => (
            <Badge key={path} size="xs" variant="outline" fontFamily="mono">
              {path}
            </Badge>
          ))}
        </HStack>
      ) : null}
      {required.length ? (
        <Text fontSize="xs" color="fg.muted">
          {t("template.required", { fields: required.join(", ") })}
        </Text>
      ) : null}
    </Stack>
  );
}
