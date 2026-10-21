import { Box, Code, Heading, Link, Text } from "@chakra-ui/react";
import { Fragment, type ReactNode } from "react";

const INLINE = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\(https?:\/\/[^)\s]+\)|\*[^*\n]+\*|_[^_\n]+_)/g;
const LINK = /^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/;

/** Inline markdown subset: **bold**, *italic* / _italic_, `code`, [links](https://…). */
function renderInline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(INLINE)) {
    const tok = m[0];
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const key = `i${i++}`;
    const link = LINK.exec(tok);
    if (link) {
      out.push(
        <Link key={key} href={link[2]} target="_blank" rel="noopener noreferrer" colorPalette="blue" className="nodrag">
          {link[1]}
        </Link>,
      );
    } else if (tok.startsWith("**")) {
      out.push(<strong key={key}>{tok.slice(2, -2)}</strong>);
    } else if (tok.startsWith("`")) {
      out.push(
        <Code key={key} size="sm">
          {tok.slice(1, -1)}
        </Code>,
      );
    } else {
      out.push(<em key={key}>{tok.slice(1, -1)}</em>);
    }
    last = at + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

type Block =
  | { kind: "h"; level: 1 | 2 | 3; text: string }
  | { kind: "ul" | "ol"; items: string[] }
  | { kind: "quote"; text: string }
  | { kind: "p"; text: string }
  | { kind: "gap" };

function parseBlocks(src: string): Block[] {
  const blocks: Block[] = [];
  for (const raw of src.replace(/\r\n/g, "\n").split("\n")) {
    const line = raw.trimEnd();
    const prev = blocks[blocks.length - 1];
    let m: RegExpExecArray | null;
    if (!line.trim()) {
      if (prev && prev.kind !== "gap") blocks.push({ kind: "gap" });
    } else if ((m = /^(#{1,3})\s+(.*)$/.exec(line))) {
      blocks.push({ kind: "h", level: (m[1]?.length ?? 1) as 1 | 2 | 3, text: m[2] ?? "" });
    } else if ((m = /^\s*[-*]\s+(.*)$/.exec(line))) {
      if (prev?.kind === "ul") prev.items.push(m[1] ?? "");
      else blocks.push({ kind: "ul", items: [m[1] ?? ""] });
    } else if ((m = /^\s*\d+[.)]\s+(.*)$/.exec(line))) {
      if (prev?.kind === "ol") prev.items.push(m[1] ?? "");
      else blocks.push({ kind: "ol", items: [m[1] ?? ""] });
    } else if ((m = /^>\s?(.*)$/.exec(line))) {
      blocks.push({ kind: "quote", text: m[1] ?? "" });
    } else if (prev?.kind === "p") {
      prev.text += `\n${line}`;
    } else {
      blocks.push({ kind: "p", text: line });
    }
  }
  return blocks;
}

const HEADING_SIZE = { 1: "md", 2: "sm", 3: "xs" } as const;

/** Small, safe (no HTML) markdown renderer for sticky notes. */
export function MiniMarkdown({ source }: { source: string }) {
  return (
    <>
      {parseBlocks(source).map((b, i) => {
        switch (b.kind) {
          case "h":
            return (
              <Heading key={i} size={HEADING_SIZE[b.level]} mb={1}>
                {renderInline(b.text)}
              </Heading>
            );
          case "ul":
          case "ol":
            return (
              <Box key={i} as={b.kind} ps={4} mb={1} listStyleType={b.kind === "ul" ? "disc" : "decimal"}>
                {b.items.map((it, j) => (
                  <li key={j}>{renderInline(it)}</li>
                ))}
              </Box>
            );
          case "quote":
            return (
              <Text key={i} borderLeftWidth="3px" borderColor="border.emphasized" ps={2} color="fg.muted" mb={1}>
                {renderInline(b.text)}
              </Text>
            );
          case "p":
            return (
              <Text key={i} whiteSpace="pre-wrap" mb={1}>
                {b.text.split("\n").map((l, j) => (
                  <Fragment key={j}>
                    {j ? <br /> : null}
                    {renderInline(l)}
                  </Fragment>
                ))}
              </Text>
            );
          case "gap":
            return <Box key={i} h={1} />;
        }
      })}
    </>
  );
}
