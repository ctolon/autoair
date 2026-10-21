import { Alert, Button, HStack } from "@chakra-ui/react";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { LuClipboard, LuRotateCcw } from "react-icons/lu";

import { i18n } from "../i18n";
import { useAccess } from "../lib/access";
import { useBuilder } from "../store/builder";
import { toaster } from "../ui/toaster";
import { isRawDag, useCodeMode } from "./store";

const t = (key: string, opts?: Record<string, unknown>) => i18n.t(key, { ns: "lifecycle", ...opts });

/**
 * Follows the editor flags (`features.visual_editor` / `features.code_editor`), which an admin
 * can change at runtime: a single enabled editor becomes the mode, and a change while a DAG is
 * open never touches the document — the user is told what changed and that the work is kept.
 */
export function useEditorModeWatch(visual: boolean, code: boolean): void {
  const before = useRef<{ visual: boolean; code: boolean } | null>(null);
  useEffect(() => {
    if (!(visual && code)) useCodeMode.getState().setMode(visual ? "visual" : "code");
    const prev = before.current;
    before.current = { visual, code };
    if (!prev || (prev.visual === visual && prev.code === code)) return;
    const raw = isRawDag(useBuilder.getState().dag);
    const draft = useCodeMode.getState().draft !== null;
    if (prev.visual && !visual) {
      toaster.create({ type: "warning", title: t("editorModes.visualOff"), description: t("editorModes.keptHelp") });
    } else if (prev.code && !code) {
      const description = raw
        ? t("editorModes.rawReadOnly")
        : draft
          ? t("editorModes.draftKept")
          : t("editorModes.keptHelp");
      toaster.create({ type: "warning", title: t("editorModes.codeOff"), description });
    } else {
      toaster.create({ type: "info", title: t("editorModes.changed"), description: t("editorModes.keptHelp") });
    }
  }, [visual, code]);
}

/**
 * An unapplied code draft left when code editing was switched off: kept in memory until the
 * user copies or discards it (or code editing comes back).
 */
export function DraftKeptBanner() {
  const { t: tl } = useTranslation("lifecycle");
  const draft = useCodeMode((s) => s.draft);
  const codeOn = useAccess().feature("code_editor");
  if (draft === null || codeOn) return null;
  return (
    <Alert.Root status="warning" size="sm" borderRadius={0} flexShrink={0} data-testid="draft-kept">
      <Alert.Indicator />
      <Alert.Content>
        <Alert.Description fontSize="xs">{tl("editorModes.draftKept")}</Alert.Description>
      </Alert.Content>
      <HStack gap={1}>
        <Button
          size="2xs"
          variant="outline"
          onClick={() => {
            void navigator.clipboard.writeText(draft);
            toaster.create({ type: "success", title: tl("common.copied") });
          }}
        >
          <LuClipboard /> {tl("common.copy")}
        </Button>
        <Button
          size="2xs"
          variant="ghost"
          onClick={() => {
            if (window.confirm(tl("codeMode.confirmDiscard"))) useCodeMode.getState().discard();
          }}
        >
          <LuRotateCcw /> {tl("codeMode.discard")}
        </Button>
      </HStack>
    </Alert.Root>
  );
}
