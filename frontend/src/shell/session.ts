import { authHeaders, resolveApiBase, setToken } from "../api/setup";

/** Public sign-in configuration (`GET /auth/config`, no authentication). */
export interface AuthConfig {
  mode: string;
  auth: string;
  login_enabled: boolean;
  sso: {
    enabled: boolean;
    cookie_name: string;
    airflow_login_url: string | null;
    refresh_margin_seconds: number;
    session_check_seconds: number;
    propagate_logout: boolean;
  };
  ui: {
    title: string;
    logo_url: string | null;
    accent_color: string | null;
    banner_text: string | null;
    banner_color: string;
    default_language: string | null;
    default_timezone: string | null;
    date_locale: string | null;
  };
}

/** The current session (`GET /auth/session`). */
interface SessionInfo {
  authenticated: boolean;
  source: "cookie" | "bearer" | "none";
  user: string | null;
  display_name?: string | null;
  expires_at: string | null;
  expires_in: number | null;
  verified: boolean;
  refreshable: boolean;
}

function url(path: string): string {
  return `${resolveApiBase()}${path}`;
}

/** `null` when the server predates `/auth/config` or cannot be reached. */
export async function fetchAuthConfig(): Promise<AuthConfig | null> {
  try {
    const resp = await fetch(url("/auth/config"), { credentials: "include" });
    if (!resp.ok) return null;
    return (await resp.json()) as AuthConfig;
  } catch {
    return null;
  }
}

/** The session, or `null` when there is none (401) or the server cannot tell. */
async function checkSession(): Promise<SessionInfo | null> {
  try {
    const resp = await fetch(url("/auth/session"), { headers: authHeaders(), credentials: "include" });
    if (resp.status === 401) return null;
    if (!resp.ok) return { authenticated: true } as SessionInfo; // unknown: assume still valid
    return (await resp.json()) as SessionInfo;
  } catch {
    return { authenticated: true } as SessionInfo; // offline: do not sign out
  }
}

/** Renew a cookie session through Airflow; `null` when it has ended. */
async function refreshSession(): Promise<SessionInfo | null> {
  try {
    const resp = await fetch(url("/auth/refresh"), {
      method: "POST",
      headers: authHeaders(),
      credentials: "include",
    });
    if (resp.status === 401) return null;
    if (!resp.ok) return checkSession();
    return (await resp.json()) as SessionInfo;
  } catch {
    return checkSession();
  }
}

/**
 * "Sign in with Airflow": opens Airflow's login page (through AutoAir's redirect) and polls
 * the session until the Airflow cookie reaches AutoAir. Resolves `true` once signed in,
 * `false` on timeout or when the window was blocked/closed early.
 */
export async function signInWithAirflow(
  config: AuthConfig,
  { timeoutMs = 300_000, intervalMs = 1500 }: { timeoutMs?: number; intervalMs?: number } = {},
): Promise<"signed-in" | "blocked" | "timeout"> {
  const target = config.sso.airflow_login_url;
  if (!target) return "blocked";
  const next = window.location.pathname + window.location.search;
  const popup = window.open(
    `${target}?next=${encodeURIComponent(next)}`,
    "autoair-airflow-login",
    "width=520,height=720",
  );
  if (!popup) return "blocked";
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, intervalMs));
    const session = await checkSession();
    if (session?.authenticated && session.source === "cookie") {
      try {
        popup.close();
      } catch {
        /* cross-origin window */
      }
      return "signed-in";
    }
  }
  return "timeout";
}

interface WatcherHandlers {
  /** The session ended (Airflow sign-out, revoked or expired token). */
  onEnded: () => void;
  /** A non-refreshable session (pasted/password token) expires soon. */
  onExpiring?: (minutes: number) => void;
}

/**
 * Keeps the session alive and in sync with Airflow: refreshes cookie sessions shortly before
 * they expire, re-checks periodically (and when the tab becomes visible) so signing out of
 * Airflow signs AutoAir out too. Returns a stop function.
 */
export function startSessionWatcher(config: AuthConfig, handlers: WatcherHandlers): () => void {
  let stopped = false;
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  let warned = false;
  const margin = Math.max(0, config.sso.refresh_margin_seconds) * 1000;

  const schedule = (session: SessionInfo | null) => {
    if (stopped || !session) return;
    if (refreshTimer) clearTimeout(refreshTimer);
    const left = session.expires_in != null ? session.expires_in * 1000 : null;
    if (left == null) return;
    if (session.refreshable) {
      refreshTimer = setTimeout(() => void refresh(), Math.max(5_000, left - margin));
    } else if (handlers.onExpiring && !warned) {
      refreshTimer = setTimeout(
        () => {
          warned = true;
          handlers.onExpiring?.(Math.max(1, Math.round(Math.min(left, margin) / 60_000)));
        },
        Math.max(0, left - margin),
      );
    }
  };
  const ended = () => {
    if (stopped) return;
    stopped = true;
    handlers.onEnded();
  };
  const refresh = async () => {
    const session = await refreshSession();
    if (!session) ended();
    else schedule(session);
  };
  const check = async () => {
    if (stopped) return;
    const session = await checkSession();
    if (!session) ended();
    else if (session.expires_in != null) schedule(session);
  };

  void check();
  const every = config.sso.session_check_seconds * 1000;
  const interval = every > 0 ? setInterval(() => void check(), every) : undefined;
  const onVisible = () => {
    if (document.visibilityState === "visible") void check();
  };
  document.addEventListener("visibilitychange", onVisible);
  return () => {
    stopped = true;
    if (refreshTimer) clearTimeout(refreshTimer);
    if (interval) clearInterval(interval);
    document.removeEventListener("visibilitychange", onVisible);
  };
}

/** Forget the token here and sign out on the server (revokes/clears the Airflow session). */
export async function signOutEverywhere(): Promise<void> {
  const headers = authHeaders();
  setToken(null);
  try {
    await fetch(url("/auth/logout"), { method: "POST", headers, credentials: "include" });
  } catch {
    /* offline: the token is gone from this browser anyway */
  }
}

/** True when this tab holds a pasted/password token (bearer) rather than a cookie session. */
