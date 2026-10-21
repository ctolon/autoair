import { Spinner } from "@chakra-ui/react";
import { type ComponentProps, Suspense, lazy } from "react";

const CodeViewImpl = lazy(() => import("./CodeView"));
const DiffViewImpl = lazy(() => import("./DiffView"));

/** Lazily-loaded CodeMirror viewer/editor. */
export function CodeView(props: ComponentProps<typeof CodeViewImpl>) {
  return (
    <Suspense fallback={<Spinner size="sm" m={3} />}>
      <CodeViewImpl {...props} />
    </Suspense>
  );
}

/** Lazily-loaded CodeMirror merge (diff) view. */
export function DiffView(props: ComponentProps<typeof DiffViewImpl>) {
  return (
    <Suspense fallback={<Spinner size="sm" m={3} />}>
      <DiffViewImpl {...props} />
    </Suspense>
  );
}
