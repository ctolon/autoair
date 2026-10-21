import { Box, Text } from "@chakra-ui/react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";

/** Lines rendered at most (the server already caps the diff; this keeps the DOM small). */
const MAX_LINES = 4000;

type Tone = { color: string; bg?: string; fontWeight?: string };

function toneOf(line: string): Tone {
  if (line.startsWith("+++") || line.startsWith("---")) return { color: "fg.muted", fontWeight: "semibold" };
  if (line.startsWith("@@")) return { color: "fg.info" };
  if (line.startsWith("+")) return { color: "fg.success", bg: "bg.success" };
  if (line.startsWith("-")) return { color: "fg.error", bg: "bg.error" };
  if (line.startsWith("\\")) return { color: "fg.subtle" };
  return { color: "fg" };
}

/**
 * A read-only unified diff (``--- a/… +++ b/… @@ … @@``) with added and removed lines
 * coloured by semantic tokens, so it reads in light and dark mode. Anything that is not a
 * string renders as "no changes" instead of failing.
 */
export function UnifiedDiff({
  diff,
  truncated,
  label,
  maxH = "320px",
}: {
  diff: unknown;
  truncated?: boolean | null;
  label: string;
  maxH?: string;
}) {
  const { t } = useTranslation("lifecycle");
  const lines = useMemo(
    () => (typeof diff === "string" && diff.trim() ? diff.replace(/\n$/, "").split("\n") : []),
    [diff],
  );
  if (!lines.length) {
    return (
      <Text fontSize="xs" color="fg.muted" data-testid="source-diff-empty">
        {t("source.noDiff")}
      </Text>
    );
  }
  const shown = lines.slice(0, MAX_LINES);
  const cut = truncated === true || lines.length > MAX_LINES;
  return (
    <Box>
      <Box
        as="pre"
        role="region"
        aria-label={label}
        data-testid="source-diff"
        maxH={maxH}
        overflow="auto"
        fontFamily="mono"
        fontSize="xs"
        lineHeight="1.45"
        borderWidth="1px"
        borderColor="border"
        rounded="md"
        bg="bg.subtle"
        py={1}
        m={0}
      >
        {shown.map((line, i) => {
          const tone = toneOf(line);
          return (
            <Box
              // Lines of a diff have no identity besides their position.
              key={i}
              as="span"
              display="block"
              px={2}
              whiteSpace="pre"
              color={tone.color}
              bg={tone.bg}
              fontWeight={tone.fontWeight}
            >
              {line || " "}
            </Box>
          );
        })}
      </Box>
      {cut ? (
        <Text fontSize="xs" color="fg.muted" mt={1}>
          {t("source.diffTruncated")}
        </Text>
      ) : null}
    </Box>
  );
}
