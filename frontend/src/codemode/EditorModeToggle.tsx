import { Box, Button, ButtonGroup } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";
import { LuCode, LuWorkflow } from "react-icons/lu";

import { useAccess } from "../lib/access";
import { Tip } from "../shell/Tip";
import { editorAvailability, effectiveMode, useCodeMode } from "./store";

/** "Visual | Code" switch at the top, shown when both editors are enabled. */
export function EditorModeToggle({ iconOnly = false }: { iconOnly?: boolean }) {
  const { t } = useTranslation("lifecycle");
  const availability = editorAvailability(useAccess());
  const stored = useCodeMode((s) => s.mode);
  const hasDraft = useCodeMode((s) => s.draft !== null);
  if (!availability.both) return null;
  const mode = effectiveMode(stored, availability);
  return (
    <ButtonGroup size="xs" variant="outline" attached role="group" aria-label={t("codeMode.toggle")} flexShrink={0}>
      {(["visual", "code"] as const).map((m) => (
        <Tip key={m} content={iconOnly ? `${t(`codeMode.${m}`)}: ${t(`codeMode.${m}Help`)}` : t(`codeMode.${m}Help`)}>
          <Button
            aria-pressed={mode === m}
            aria-label={t(`codeMode.${m}`)}
            data-testid={`mode-${m}`}
            variant={mode === m ? "subtle" : "outline"}
            colorPalette={mode === m ? "blue" : undefined}
            onClick={() => useCodeMode.getState().setMode(m)}
            px={2}
          >
            {m === "visual" ? <LuWorkflow /> : <LuCode />}
            {iconOnly ? null : t(`codeMode.${m}`)}
            {m === "code" && hasDraft ? (
              <Box as="span" w="6px" h="6px" rounded="full" bg="orange.solid" aria-label={t("codeMode.edited")} />
            ) : null}
          </Button>
        </Tip>
      ))}
    </ButtonGroup>
  );
}
