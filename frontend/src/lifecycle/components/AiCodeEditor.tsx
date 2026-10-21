import { Spinner } from "@chakra-ui/react";
import { type ComponentProps, Suspense, lazy } from "react";

const Impl = lazy(() => import("./AiCodeEditorImpl"));

export type { AiCodeLanguage } from "./AiCodeEditorImpl";

/** Lazily-loaded CodeMirror editor/viewer of the AI dialogs. */
export function AiCodeEditor(props: ComponentProps<typeof Impl>) {
  return (
    <Suspense fallback={<Spinner size="sm" m={3} />}>
      <Impl {...props} />
    </Suspense>
  );
}
