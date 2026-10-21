import { Button, HStack, IconButton, Spinner, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCircleAlert, LuCircleCheck, LuShieldCheck } from "react-icons/lu";

import { validateCode } from "../../api/gen/sdk.gen";
import type { ValidateRequest, ValidateResult } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import type { CodeDiagnostic, CodeLanguage, PythonLspContext } from "./types";

type ValidateLanguage = ValidateRequest["language"];

const LANGUAGE: Partial<Record<CodeLanguage, ValidateLanguage>> = {
  python: "python",
  bash: "bash",
  sql: "sql",
  yaml: "yaml",
  json: "json",
  markdown: "markdown",
  go: "go",
  java: "java",
  javascript: "javascript",
  typescript: "typescript",
};

/** The server-side validator language of an editor (null: nothing to validate, e.g. Jinja). */
export function validateLanguageOf(language: CodeLanguage): ValidateLanguage | null {
  return LANGUAGE[language] ?? null;
}

/** Validator diagnostics → editor diagnostics (the tool and rule are appended). */
export function toCodeDiagnostics(result: ValidateResult): CodeDiagnostic[] {
  return (result.diagnostics ?? []).map((d) => ({
    line: d.line,
    column: d.column ?? null,
    severity: d.severity,
    message: `${d.message} (${d.source}${d.code ? ` ${d.code}` : ""})`,
  }));
}

type ValidationSummary = {
  status: "ok" | "warnings" | "errors" | "unavailable";
  errors: number;
  warnings: number;
  /** Tools that checked the code. */
  ran: string[];
  /** Tools that are not installed (or failed) on the server, with the reason. */
  missing: { name: string; note: string | null }[];
};

export function summarize(result: ValidateResult): ValidationSummary {
  const errors = (result.diagnostics ?? []).filter((d) => d.severity === "error").length;
  const warnings = (result.diagnostics ?? []).length - errors;
  const ran = (result.tools ?? []).filter((t) => t.ran).map((t) => t.name);
  const missing = (result.tools ?? []).filter((t) => !t.ran).map((t) => ({ name: t.name, note: t.note ?? null }));
  const status = !result.validated ? "unavailable" : errors ? "errors" : warnings ? "warnings" : "ok";
  return { status, errors, warnings, ran, missing };
}

type Check = { value: string; result: ValidateResult } | { value: string; error: string };

/**
 * On-demand validation of a code field (`POST /api/lsp/validate`). The result belongs to the
 * text it was computed for: once the text changes it is hidden (stale) until validated again.
 */
export function useCodeValidation(value: string, language: CodeLanguage, pythonLsp?: PythonLspContext | null) {
  const target = validateLanguageOf(language);
  const [check, setCheck] = useState<Check | null>(null);
  const [running, setRunning] = useState(false);
  const fresh = check && check.value === value ? check : null;
  const run = async () => {
    if (!target) return;
    const code = value;
    setRunning(true);
    try {
      const { data } = await validateCode({
        body: {
          language: target,
          code,
          // Task bodies are wrapped in their function; other Python is a whole module.
          python_mode: pythonLsp && (pythonLsp.mode ?? "task_body") === "task_body" ? "task_body" : "module",
        },
        throwOnError: true,
      });
      setCheck({ value: code, result: data });
    } catch (err) {
      setCheck({ value: code, error: errorMessage(err) });
    } finally {
      setRunning(false);
    }
  };
  return {
    available: target !== null,
    running,
    run,
    result: fresh && "result" in fresh ? fresh.result : null,
    error: fresh && "error" in fresh ? fresh.error : null,
    diagnostics: fresh && "result" in fresh ? toCodeDiagnostics(fresh.result) : undefined,
  };
}

type CodeValidation = ReturnType<typeof useCodeValidation>;

/** Icon button shown over the editor (next to "open in a large editor"). */
export function ValidateIconButton({ validation, name }: { validation: CodeValidation; name: string }) {
  const { t } = useTranslation("common");
  if (!validation.available) return null;
  const label = t("codeEditor.validate", { name });
  return (
    <IconButton
      aria-label={label}
      title={label}
      size="2xs"
      variant="subtle"
      position="absolute"
      top="4px"
      right="30px"
      zIndex={1}
      opacity={0.55}
      _groupHover={{ opacity: 1 }}
      _groupFocusWithin={{ opacity: 1 }}
      _focusVisible={{ opacity: 1 }}
      loading={validation.running}
      onClick={() => void validation.run()}
    >
      <LuShieldCheck />
    </IconButton>
  );
}

/** Labelled button for toolbars (the large editor dialog). */
export function ValidateButton({ validation, compact }: { validation: CodeValidation; compact?: boolean }) {
  const { t } = useTranslation("common");
  if (!validation.available) return null;
  return (
    <Button
      size={compact ? "2xs" : "xs"}
      variant={compact ? "ghost" : "outline"}
      flexShrink={0}
      loading={validation.running}
      onClick={() => void validation.run()}
    >
      <LuShieldCheck /> {t("codeEditor.validateShort")}
    </Button>
  );
}

/** One line under the editor: valid / N errors · M warnings, which tools ran, which are missing. */
export function ValidationResultLine({ validation }: { validation: CodeValidation }) {
  const { t } = useTranslation("common");
  if (validation.running && !validation.result)
    return (
      <HStack gap={1} fontSize="xs" color="fg.muted" mt={1}>
        <Spinner size="xs" /> {t("codeEditor.validating")}
      </HStack>
    );
  if (validation.error)
    return (
      <Text fontSize="xs" color="fg.error" mt={1} role="status">
        {t("codeEditor.validateFailed", { message: validation.error })}
      </Text>
    );
  if (!validation.result) return null;
  const s = summarize(validation.result);
  const color = { ok: "fg.success", warnings: "fg.warning", errors: "fg.error", unavailable: "fg.muted" }[s.status];
  const headline =
    s.status === "ok"
      ? t("codeEditor.valid")
      : s.status === "unavailable"
        ? t("codeEditor.noValidator")
        : [
            s.errors ? t("codeEditor.errors", { count: s.errors }) : null,
            s.warnings ? t("codeEditor.warnings", { count: s.warnings }) : null,
          ]
            .filter(Boolean)
            .join(" · ");
  return (
    <HStack gap={1.5} fontSize="xs" mt={1} role="status" data-validation={s.status} flexWrap="wrap">
      <HStack gap={1} color={color} fontWeight="medium">
        {s.status === "ok" ? <LuCircleCheck /> : <LuCircleAlert />}
        <Text>{headline}</Text>
      </HStack>
      {s.ran.length ? <Text color="fg.muted">{t("codeEditor.checkedWith", { tools: s.ran.join(", ") })}</Text> : null}
      {s.missing.length ? (
        <Text color="fg.muted" title={s.missing.map((m) => m.note ?? m.name).join("\n")}>
          {t("codeEditor.notAvailable", { tools: s.missing.map((m) => m.name).join(", ") })}
        </Text>
      ) : null}
    </HStack>
  );
}
