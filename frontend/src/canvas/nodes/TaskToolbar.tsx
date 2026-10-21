import { Box, HStack, IconButton, Popover, Portal, Spinner, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { NodeToolbar, Position } from "@xyflow/react";
import { memo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  LuArrowDownToLine,
  LuArrowUpFromLine,
  LuBookOpen,
  LuCopy,
  LuEye,
  LuEyeOff,
  LuFlaskConical,
  LuTrash2,
} from "react-icons/lu";

import { getOperatorOptions } from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import { runCommand, useCommandList } from "../../lib/commands";
import type { TaskSpec } from "../../lib/types";
import { useBuilder } from "../../store/builder";

/** Id of the command the run/lifecycle feature registers to test a single task. */
const TEST_TASK_COMMAND = "runner.testTask";

function OperatorDocs({ fqn }: { fqn: string }) {
  const { t } = useTranslation("canvas");
  const [open, setOpen] = useState(false);
  const docs = useQuery({ ...getOperatorOptions({ path: { fqn } }), enabled: open, staleTime: 5 * 60_000 });
  const text = docs.data?.docstring?.trim() || docs.data?.summary || "";
  return (
    <Popover.Root open={open} onOpenChange={(e) => setOpen(e.open)} lazyMount unmountOnExit>
      <Popover.Trigger asChild>
        <IconButton size="2xs" variant="ghost" aria-label={t("toolbar.docs")} title={t("toolbar.docs")}>
          <LuBookOpen />
        </IconButton>
      </Popover.Trigger>
      <Portal>
        <Popover.Positioner>
          <Popover.Content maxW="420px" className="nowheel">
            <Popover.Arrow />
            <Popover.Body p={3}>
              <Text fontWeight="semibold" fontSize="sm" mb={1}>
                {docs.data?.class_name ?? fqn.split(".").pop()}
              </Text>
              <Text fontSize="xs" color="fg.muted" fontFamily="mono" mb={2} wordBreak="break-all">
                {fqn}
              </Text>
              {docs.isPending ? <Spinner size="sm" /> : null}
              {docs.isError ? (
                <Text fontSize="xs" color="fg.error">
                  {errorMessage(docs.error)}
                </Text>
              ) : null}
              {docs.data ? (
                <Box maxH="260px" overflowY="auto" fontSize="xs" whiteSpace="pre-wrap">
                  {text || t("toolbar.noDocs")}
                </Box>
              ) : null}
            </Popover.Body>
          </Popover.Content>
        </Popover.Positioner>
      </Portal>
    </Popover.Root>
  );
}

/** Quick actions shown above a single selected task node. */
export const TaskToolbar = memo(function TaskToolbar({ id, spec }: { id: string; spec: TaskSpec }) {
  const { t } = useTranslation("canvas");
  const canTest = useCommandList().some((c) => c.id === TEST_TASK_COMMAND);
  const store = useBuilder.getState;
  const disabled = !!spec.disabled;
  const flip = (patch: Partial<TaskSpec>) => store().updateSpec(id, patch);

  return (
    <NodeToolbar position={Position.Top} offset={8}>
      <HStack
        gap={0.5}
        p={0.5}
        bg="bg.panel"
        borderWidth="1px"
        borderColor="border.emphasized"
        rounded="md"
        shadow="md"
        role="toolbar"
        aria-label={t("toolbar.label", { name: spec.task_id })}
      >
        <IconButton
          size="2xs"
          variant="ghost"
          aria-label={t("actions.duplicate")}
          title={t("actions.duplicate")}
          onClick={() => store().duplicateNodes([id])}
        >
          <LuCopy />
        </IconButton>
        <IconButton
          size="2xs"
          variant="ghost"
          aria-pressed={disabled}
          aria-label={disabled ? t("toolbar.enable") : t("toolbar.disable")}
          title={disabled ? t("toolbar.enable") : t("toolbar.disable")}
          onClick={() => flip({ disabled: !disabled })}
        >
          {disabled ? <LuEye /> : <LuEyeOff />}
        </IconButton>
        <IconButton
          size="2xs"
          variant={spec.is_setup ? "subtle" : "ghost"}
          colorPalette={spec.is_setup ? "teal" : undefined}
          aria-pressed={!!spec.is_setup}
          aria-label={t("toolbar.setup")}
          title={t("toolbar.setup")}
          onClick={() => flip({ is_setup: !spec.is_setup, ...(spec.is_setup ? {} : { is_teardown: false }) })}
        >
          <LuArrowUpFromLine />
        </IconButton>
        <IconButton
          size="2xs"
          variant={spec.is_teardown ? "subtle" : "ghost"}
          colorPalette={spec.is_teardown ? "teal" : undefined}
          aria-pressed={!!spec.is_teardown}
          aria-label={t("toolbar.teardown")}
          title={t("toolbar.teardown")}
          onClick={() => flip({ is_teardown: !spec.is_teardown, ...(spec.is_teardown ? {} : { is_setup: false }) })}
        >
          <LuArrowDownToLine />
        </IconButton>
        <IconButton
          size="2xs"
          variant="ghost"
          disabled={!canTest}
          aria-label={t("toolbar.test")}
          title={canTest ? t("toolbar.test") : t("toolbar.testUnavailable")}
          onClick={() => {
            store().select(id);
            runCommand(TEST_TASK_COMMAND);
          }}
        >
          <LuFlaskConical />
        </IconButton>
        {spec.kind === "operator" ? <OperatorDocs fqn={spec.operator} /> : null}
        <IconButton
          size="2xs"
          variant="ghost"
          colorPalette="red"
          aria-label={t("actions.delete")}
          title={t("actions.delete")}
          onClick={() => store().removeElements([id])}
        >
          <LuTrash2 />
        </IconButton>
      </HStack>
    </NodeToolbar>
  );
});
