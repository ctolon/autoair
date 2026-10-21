import { HStack, IconButton, Text } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";
import { LuArrowLeft, LuX } from "react-icons/lu";

import type { XComRef } from "../lib/types";
import { nodeKey } from "../lib/types";
import { useBuilder } from "../store/builder";

/** "⟵ output of extract[key]" chip for an XCom reference value. */
export function XComChip({ value, onRemove }: { value: XComRef; onRemove?: () => void }) {
  const { t } = useTranslation("inspector");
  const source = useBuilder((s) => s.nodes.find((n) => n.id === value.node));
  const name = source ? nodeKey(source) : value.node;
  const text = value.key ? t("xcom.outputOfKey", { task: name, key: value.key }) : t("xcom.outputOf", { task: name });
  return (
    <HStack
      gap={1}
      px={2}
      py={0.5}
      rounded="full"
      borderWidth="1px"
      borderColor={source ? "teal.500" : "red.500"}
      bg={source ? "teal.subtle" : "red.subtle"}
      maxW="100%"
      alignSelf="start"
      title={source ? undefined : t("xcom.missingSource")}
    >
      <LuArrowLeft aria-hidden size={12} />
      <Text fontSize="xs" fontFamily="mono" truncate>
        {text}
      </Text>
      {onRemove ? (
        <IconButton aria-label={t("xcom.removeRef")} size="2xs" variant="ghost" rounded="full" onClick={onRemove}>
          <LuX />
        </IconButton>
      ) : null}
    </HStack>
  );
}
