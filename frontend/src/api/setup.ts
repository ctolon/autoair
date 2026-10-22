import { client } from "./gen/client.gen";

declare global {
  interface Window {
    __AUTOAIR_CONFIG__?: { apiBase?: string; initialDagId?: string };
  }
}

const TOKEN_KEY = "autoair.airflowToken";

/**
 * Where the AutoAir API lives:
 *  - standalone / Airflow 3.0 full page: injected by the server into index.html
 *  - dev server: proxied `/api`
 *  - Airflow >= 3.1 react_app: the plugin's FastAPI app, relative to Airflow's base URL
 */
export function resolveApiBase(): string {
  const injected = window.__AUTOAIR_CONFIG__?.apiBase;
  if (injected) return injected.replace(/\/$/, "");
  if (import.meta.env.DEV) return "/api";
  return new URL("autoair/api", document.baseURI).pathname.replace(/\/$/, "");
}

function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: token only lives for this page */
  }
}

/** Prefix of `autoair local`'s per-run tokens (`LOCAL_TOKEN_PREFIX` in src/autoair/server.py). */
const LOCAL_TOKEN_PREFIX = "autoair-local-";

/**
 * `autoair local --open` opens the UI with `#token=<per-run token>`: keep it for this tab and
 * drop it from the address bar (a fragment never reaches a server or its logs). Only tokens of
 * that shape are taken, so a crafted link cannot sign the user in with someone else's token.
 */
export function adoptFragmentToken(): void {
  try {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const token = params.get("token");
    if (token === null) return;
    if (token.startsWith(LOCAL_TOKEN_PREFIX)) setToken(token);
    params.delete("token");
    const url = new URL(window.location.href);
    const rest = params.toString();
    url.hash = rest ? `#${rest}` : "";
    window.history.replaceState(window.history.state, "", url.toString());
  } catch {
    /* sandboxed / non-standard location */
  }
}

let configured = false;

export function configureClient(): void {
  if (configured) return;
  configured = true;
  adoptFragmentToken();
  // Inside Airflow the `_token` cookie authenticates us; standalone uses a bearer token.
  client.setConfig({ baseUrl: resolveApiBase(), credentials: "include", throwOnError: true });
  client.interceptors.request.use((request) => {
    const token = getToken();
    if (token && !request.headers.has("Authorization")) {
      request.headers.set("Authorization", `Bearer ${token}`);
    }
    // Cookie sessions (the Airflow UI's `_token`): state-changing calls must prove they come
    // from this app (docs/sso.md); harmless with a bearer token.
    if (!request.headers.has(CSRF_HEADER)) request.headers.set(CSRF_HEADER, "1");
    return request;
  });
  // The API never answers with an HTML page: one means a wrong API address, a sign-in page or
  // a proxy error page in between. Turn it into an error so queries fail visibly instead of
  // handing a string to components that expect JSON (which would break the whole view).
  client.interceptors.response.use((response) => {
    if (response.ok && (response.headers.get("Content-Type") ?? "").startsWith("text/html")) {
      return new Response(JSON.stringify({ detail: HTML_ANSWER }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
    return response;
  });
}

const HTML_ANSWER =
  "The AutoAir API answered with an HTML page (wrong API address, or a sign-in / proxy page in between).";

/** Header the server requires on state-changing requests authenticated by the session cookie. */
const CSRF_HEADER = "X-AutoAir-CSRF";

/** Headers for hand-written `fetch` calls (streams, sign-in): bearer token + CSRF marker. */
export function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const headers: Record<string, string> = { ...extra, [CSRF_HEADER]: "1" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

/** Human readable message from an API error (FastAPI `detail` or a thrown Error). */
export function errorMessage(err: unknown): string {
  if (err && typeof err === "object" && "detail" in err) {
    const detail = (err as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg ?? ""}`)
        .join("; ");
    }
    // Structured details (e.g. a refused promotion's {status, message, report}) carry a message.
    if (detail && typeof detail === "object" && typeof (detail as { message?: unknown }).message === "string") {
      return (detail as { message: string }).message;
    }
    return JSON.stringify(detail);
  }
  if (err instanceof Error) return err.message;
  return String(err);
}
