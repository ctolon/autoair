import { Badge, HStack, Link, Text } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";
import { LuExternalLink } from "react-icons/lu";

type Parsed = { tool: string | null; rule: string; url: string | null; palette: string };

/**
 * Problem codes as they come from the server: `lint:B018` (ruff), `SC2086` (ShellCheck),
 * `schema`, `param`, `graph`… → tool, rule and documentation link.
 */
export function parseProblemCode(code: string): Parsed {
  const lint = /^lint:([A-Z]+\d+)$/.exec(code);
  if (lint?.[1]) {
    const rule = lint[1];
    // Ruff's rule pages are named (…/rules/useless-expression); the server sends the link.
    return { tool: "ruff", rule, url: null, palette: "purple" };
  }
  const shell = /^(SC\d{4})$/.exec(code);
  if (shell?.[1])
    return { tool: "ShellCheck", rule: shell[1], url: `https://www.shellcheck.net/wiki/${shell[1]}`, palette: "teal" };
  if (code === "schema") return { tool: null, rule: "schema", url: null, palette: "red" };
  if (code === "secret") return { tool: null, rule: "secret", url: null, palette: "orange" };
  if (code === "ai" || code.startsWith("ai:"))
    return { tool: "AI", rule: code.replace(/^ai:?/, "") || "review", url: null, palette: "pink" };
  return { tool: null, rule: code, url: null, palette: "gray" };
}

/** `line 2: message` → [2, "message"] (lint messages carry their line in the text). */
export function splitLine(message: string, line?: number | null): [number | null, string] {
  const m = /^line (\d+):\s*/.exec(message);
  if (m?.[1]) return [Number(m[1]), message.slice(m[0].length)];
  return [line ?? null, message];
}

/** Compact, linkable chip for a problem's rule code (e.g. "ruff · B018 ↗"). */
export function ProblemCode({
  code,
  line,
  docUrl,
}: {
  code: string;
  line?: number | null;
  docUrl?: string | null | undefined;
}) {
  const { t } = useTranslation("lifecycle");
  const parsed = parseProblemCode(code);
  const p = docUrl?.startsWith("https://") ? { ...parsed, url: docUrl } : parsed;
  const chip = (
    <Badge
      size="xs"
      variant="surface"
      colorPalette={p.palette}
      fontFamily="mono"
      gap={1}
      title={p.url ? t("problems.ruleDocs", { tool: p.tool ?? "", rule: p.rule }) : undefined}
      data-problem-code={code}
    >
      {p.tool ? (
        <Text as="span" fontFamily="body" fontWeight="semibold">
          {p.tool}
        </Text>
      ) : null}
      {p.rule}
      {p.url ? <LuExternalLink aria-hidden /> : null}
    </Badge>
  );
  return (
    <HStack gap={1} flexShrink={0}>
      {line != null ? (
        <Badge size="xs" variant="outline" fontFamily="mono" title={t("problems.line", { line })}>
          L{line}
        </Badge>
      ) : null}
      {p.url ? (
        <Link href={p.url} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>
          {chip}
        </Link>
      ) : (
        chip
      )}
    </HStack>
  );
}
