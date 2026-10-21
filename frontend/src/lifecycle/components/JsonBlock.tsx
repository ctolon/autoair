import { Code } from "@chakra-ui/react";

export function pretty(value: unknown): string {
  if (value === undefined) return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** Small pretty-printed JSON value (no editor). */
export function JsonBlock({ value, maxH = "240px" }: { value: unknown; maxH?: string }) {
  return (
    <Code
      as="pre"
      display="block"
      p={2}
      fontSize="xs"
      whiteSpace="pre-wrap"
      wordBreak="break-word"
      overflow="auto"
      maxH={maxH}
      w="100%"
    >
      {pretty(value)}
    </Code>
  );
}
