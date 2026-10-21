import { chakra } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { type ReactNode, memo, useState } from "react";

import { listIconsOptions } from "../api/gen/@tanstack/react-query.gen";
import { resolveApiBase } from "../api/setup";
import type { PythonDecorator } from "../lib/types";

/**
 * Canvas icons: bundled integration logos (`builtin:<logo>`) and uploaded custom icons
 * (`custom:<name>`), served as small images by the API (public, cacheable).
 */
const iconUrl = (id: string) => `${resolveApiBase()}/icons/${encodeURIComponent(id)}`;

/** Logos for TaskFlow nodes (the decorator says which runtime runs the function). */
const DECORATOR_ICONS: Partial<Record<PythonDecorator, string>> = {
  docker: "builtin:docker",
  kubernetes: "builtin:kubernetes",
  bash: "builtin:bash",
};

export function decoratorIcon(decorator: PythonDecorator | undefined): string | null {
  const d = decorator ?? "task";
  const icon = DECORATOR_ICONS[d];
  return icon === "" ? null : (icon ?? "builtin:python");
}

/** An icon image with a fallback (generic React icon) when there is none or it fails. */
export const IconImage = memo(function IconImage({
  id,
  size = 16,
  fallback = null,
  title,
}: {
  id: string | null | undefined;
  size?: number;
  fallback?: ReactNode;
  title?: string | undefined;
}) {
  const [failed, setFailed] = useState<string | null>(null);
  if (!id || failed === id) return <>{fallback}</>;
  return (
    // Many vendor logos are dark (Spark, Jira, GitHub…): on dark backgrounds they sit on a light tile.
    <chakra.img
      src={iconUrl(id)}
      width={size}
      height={size}
      alt=""
      title={title}
      aria-hidden={title ? undefined : true}
      draggable={false}
      loading="lazy"
      decoding="async"
      style={{ width: size, height: size, objectFit: "contain", display: "block", flexShrink: 0 }}
      boxSizing="border-box"
      _dark={{ bg: "rgba(255,255,255,0.92)", rounded: "3px", p: "1px" }}
      onError={() => setFailed(id)}
    />
  );
});

/** Every icon (bundled + custom) for pickers. */
export function useIcons() {
  return useQuery({ ...listIconsOptions(), staleTime: 60_000 });
}
