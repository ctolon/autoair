import { useMeta } from "../lifecycle/hooks";

export type Permission =
  | "builder:use"
  | "spec:read"
  | "audit:read"
  | "airflow:read"
  | "spec:write"
  | "spec:delete"
  | "code:execute"
  | "code:raw"
  | "run:test"
  | "connection:write"
  | "catalog:refresh"
  | "ai:use"
  | "ai:admin"
  | "review"
  | "deploy"
  | "deploy:direct_push"
  | "promote"
  | "run:trigger"
  | "run:manage"
  | "template:write"
  | "icon:write"
  | "admin:deploy_target"
  | "admin:config"
  | "admin:panel";

export type Feature =
  | "ai"
  | "mcp"
  | "collab"
  | "marketplace"
  | "templates"
  | "imports"
  | "test_runs"
  | "runs"
  | "deploy"
  | "custom_icons"
  | "python_code"
  | "visual_editor"
  | "code_editor"
  | "code_edit_inline"
  | "ide_language_server"
  | "airflow";

/** Opt-in flags: off unless the server reports them on. */
const OFF_BY_DEFAULT: ReadonlySet<Feature> = new Set(["code_edit_inline", "ide_language_server"]);

export type Access = {
  /** The signed-in user holds this AutoAir permission (servers without IAM allow all). */
  can: (permission: Permission) => boolean;
  /** The feature is switched on (AUTOAIR_FEATURES__<NAME>; on when the server doesn't say). */
  feature: (name: Feature) => boolean;
  /** Both: the feature is on and the user may use it. */
  allows: (name: Feature, permission: Permission) => boolean;
  roles: string[];
  user: string | null;
  loaded: boolean;
};

/**
 * What the current user may do, from `/api/meta` (`iam` = the `/api/me` payload, `features`).
 * The UI hides or disables actions accordingly; the server enforces the same rules (403).
 */
export function useAccess(): Access {
  return accessOf(useMeta());
}

/** {@link useAccess} for code outside React (commands): from a `/api/meta` payload. */
export function accessOf(meta: ReturnType<typeof useMeta>): Access {
  const iam = meta?.iam ?? null;
  const permissions = new Set(iam?.permissions ?? []);
  const features = (meta?.features ?? {}) as Partial<Record<Feature, boolean>>;
  const can = (p: Permission) => !iam || permissions.has(p);
  const feature = (f: Feature) => (OFF_BY_DEFAULT.has(f) ? features[f] === true : features[f] !== false);
  return {
    can,
    feature,
    allows: (f, p) => feature(f) && can(p),
    roles: iam?.roles ?? [],
    user: iam?.user ?? null,
    loaded: !!meta,
  };
}
