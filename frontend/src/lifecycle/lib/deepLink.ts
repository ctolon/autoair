const DAG_ID = /^[A-Za-z0-9_.-]{1,250}$/;

function fromParams(raw: string): string | null {
  const value = new URLSearchParams(raw).get("dag");
  return value && DAG_ID.test(value) ? value : null;
}

/** dag_id requested by the URL: `?dag=<id>` (standalone) or `#dag=<id>` (Airflow plugin). */
export function parseDeepLink(search: string, hash: string): string | null {
  return fromParams(search.replace(/^\?/, "")) ?? fromParams(hash.replace(/^#/, ""));
}

/** Returns `href` with the deep link set to `dagId` (or removed), in the query or the hash. */
export function withDeepLink(href: string, dagId: string | null, where: "query" | "hash"): string {
  const url = new URL(href);
  if (where === "query") {
    if (dagId) url.searchParams.set("dag", dagId);
    else url.searchParams.delete("dag");
  } else {
    const params = new URLSearchParams(url.hash.replace(/^#/, ""));
    if (dagId) params.set("dag", dagId);
    else params.delete("dag");
    const h = params.toString();
    url.hash = h ? `#${h}` : "";
  }
  return url.toString();
}

/** In the Airflow plugin the host router owns the query string, so the hash is used there. */
const deepLinkLocation = (): "query" | "hash" => (import.meta.env.MODE === "plugin" ? "hash" : "query");

/** dag_id of the Airflow DAG page hosting the app (the plugin's "AutoAir" DAG tab), if any. */
export function hostDagId(): string | null {
  const id = typeof window === "undefined" ? undefined : window.__AUTOAIR_CONFIG__?.initialDagId;
  return id && DAG_ID.test(id) ? id : null;
}

/** dag_id to open on startup: the hosting DAG page first, then the URL. */
export function initialDagId(): string | null {
  return hostDagId() ?? parseDeepLink(window.location.search, window.location.hash);
}

/**
 * Keeps the address bar in sync with the open DAG (no navigation, no history entry).
 * On an Airflow DAG page the host owns the URL, so nothing is changed there.
 */
export function syncDeepLink(dagId: string | null): void {
  if (hostDagId()) return;
  try {
    const next = withDeepLink(window.location.href, dagId, deepLinkLocation());
    if (next !== window.location.href) window.history.replaceState(window.history.state, "", next);
  } catch {
    /* sandboxed / non-standard location */
  }
}
