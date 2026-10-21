import { Spinner } from "@chakra-ui/react";
import { Suspense, lazy } from "react";

// react-markdown + remark-gfm are only fetched when something is previewed.
const MarkdownImpl = lazy(() => import("./Markdown"));

/** Lazy GitHub-flavoured Markdown preview. */
export function MarkdownPreview({ text }: { text: string }) {
  return (
    <Suspense fallback={<Spinner size="xs" />}>
      <MarkdownImpl text={text} />
    </Suspense>
  );
}
