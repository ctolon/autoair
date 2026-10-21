import { Skeleton, Stack } from "@chakra-ui/react";
import { Suspense, lazy } from "react";

import type { SchemaFormProps } from "./SchemaFormImpl";

// RJSF + ajv are heavy: loaded on first use.
const SchemaFormImpl = lazy(() => import("./SchemaFormImpl"));

function FormSkeleton() {
  return (
    <Stack gap={2} aria-busy="true">
      <Skeleton h="8" />
      <Skeleton h="8" />
    </Stack>
  );
}

/** Lazy JSON-Schema form (RJSF, Chakra theme). */
export function SchemaForm(props: SchemaFormProps) {
  return (
    <Suspense fallback={<FormSkeleton />}>
      <SchemaFormImpl {...props} />
    </Suspense>
  );
}
