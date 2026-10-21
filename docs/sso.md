# Sign-in with the Airflow UI session (SSO)

Reference for contributors and operators. Task-oriented guide on the documentation site: [SSO](https://ctolon.github.io/autoair/docs/admin/sso).

A user signed in to the Airflow UI should not have to sign in to AutoAir again. Airflow 3
allows this only within limits; AutoAir builds on what it allows, with the security
trade-offs noted below. Code: `autoair/iam/sso.py`, `autoair/auth.py`,
`frontend/src/shell/{LoginGate.tsx,session.ts,PlatformShell.tsx}`. Settings: the `sso` section ([Settings](#settings-sso-section)).

## What Airflow 3 gives us

Airflow 3 (checked against 3.3) keeps the UI session in a cookie:

| Property | Value | Consequence |
|---|---|---|
| Name | `_token` (`COOKIE_NAME_JWT_TOKEN`) | AutoAir's `sso.cookie_name` defaults to it |
| Content | the Airflow JWT (the same token `POST /auth/token` returns) | AutoAir can verify it like a bearer token |
| `HttpOnly` | yes | JavaScript (Airflow's or AutoAir's) can never read it: only a server can |
| `SameSite` | `Lax` | sent on same-site navigations and requests; not on cross-site sub-requests |
| `Domain` | none (host-only) | sent only to Airflow's **host** — but to **every port** of it |
| `Path` | `[api] base_url`'s path, else `/` | AutoAir must be under that path to receive it |
| `Secure` | when Airflow is reached over HTTPS | |
| Refresh | `JWTRefreshMiddleware` re-issues the cookie on any Airflow request when the auth manager's `refresh_user` returns a user (such as Keycloak, or FAB with refresh tokens); SimpleAuthManager never refreshes | AutoAir relays this refresh |
| Logout | `GET /api/v2/auth/logout` revokes the cookie's and the bearer token (Airflow ≥ 3.2) and deletes the cookie | AutoAir calls it and clears the cookie too |
| Login return URL | `/auth/login?next=…` only accepts URLs on Airflow's own host:port and under its base path (`is_safe_url`) | Airflow cannot hand a token back to another origin |

There is **no** endpoint that turns the UI session into a token for another origin (no OAuth
authorization-code flow, no CORS on `/auth/token`). Everything below follows from that.

## Modes

| Deployment | How the user is signed in | Refresh | Logout propagation |
|---|---|---|---|
| **Plugin** (inside Airflow's API server) | The browser sends `_token` to `/autoair/api` (same origin); Airflow's `get_user` validates it | Airflow's own middleware refreshes the cookie on AutoAir's requests too (they go through Airflow's app) | Signing out of Airflow deletes the cookie; the next AutoAir check fails and the sign-in panel links back to Airflow's login |
| **Standalone, same host** (`https://data.example.com:8765` next to Airflow on `:8080`, or `/autoair` behind the same proxy) | Automatic: the host-only cookie reaches AutoAir; AutoAir verifies it against Airflow (`TokenVerifier`) on every request (cached `auth_cache_ttl` s, never past `exp`) | `POST /api/auth/refresh`: AutoAir calls Airflow with the cookie and relays a re-issued cookie (same host → it is the same cookie jar) | `POST /api/auth/logout` revokes the token at Airflow and clears the cookie, which signs the Airflow UI out as well (`sso.propagate_logout`); signing out of Airflow is noticed by the UI's periodic `GET /api/auth/session` (`sso.session_check_seconds`) |
| **Standalone, sibling sub-domain** (`autoair.example.com` + `airflow.example.com`) | Only when the proxy in front of Airflow widens the cookie: nginx `proxy_cookie_domain airflow.example.com .example.com;` (or Traefik/Envoy equivalents). Then as above | as above, but AutoAir cannot rewrite the widened cookie's domain: the re-issued cookie lands on AutoAir's host. Prefer short-lived tokens with refresh handled in Airflow | as above |
| **Standalone, another site** | No cookie can be shared. Users sign in with their Airflow **username/password** (proxied once to Airflow's `/auth/token`, never stored) or paste a token | Not possible without the password: the UI warns `sso.refresh_margin_seconds` before expiry | `POST /api/auth/logout` revokes the token |

"Sign in with Airflow" (`GET /api/auth/sso/login`) opens Airflow's login page in a small window.
When AutoAir is reachable under Airflow's host (`server.public_url` on the same host:port as
`sso.airflow_public_url`, under its base path) the window comes back to AutoAir through
Airflow's `next=`; otherwise the user signs in to Airflow and the AutoAir tab picks the session
up as soon as the cookie is visible to it (it polls `GET /api/auth/session`).

## Endpoints

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/auth/config` | public | what the sign-in page offers: password form, SSO, branding (`ui.*`) |
| `GET /api/auth/session` | public (verifies what it gets) | the current session: `source` (`cookie`/`bearer`), user, `expires_in`, `refreshable`; `401` when there is none — this is how an Airflow sign-out reaches AutoAir |
| `POST /api/auth/refresh` | cookie | renews a cookie session through Airflow; `409` for bearer tokens, `401` (cookie cleared) when Airflow ended the session |
| `GET /api/auth/sso/login?next=/ui/…` | public | redirect to Airflow's login (with a return URL only when Airflow will accept it; `next` must be a same-site path) |
| `POST /api/auth/login`, `POST /api/auth/logout` | public / token | password sign-in (§5 of [iam.md](iam.md)); logout also revokes and clears the cookie session |

## The UI

* `LoginGate` asks `/auth/config`: plugin mode shows "Sign in to Airflow" (Airflow's login with
  the current page as `next`); standalone shows "Sign in with Airflow" (when `sso.enabled`), the
  password form (when `iam.login_enabled`) and the token field; `autoair local --token` shows
  only the token field.
* `PlatformShell` runs the session watcher (`startSessionWatcher`): it refreshes cookie sessions
  `refresh_margin_seconds` before `exp`, warns before a bearer token expires, re-checks every
  `session_check_seconds` and when the tab becomes visible. When the session ends it shows the
  sign-in panel *over* the builder, so unsaved work stays in place (drafts are also kept
  locally).

## Security considerations

* **The token never reaches JavaScript.** AutoAir does not copy the cookie into
  `sessionStorage`; the browser keeps sending the `HttpOnly` cookie and AutoAir forwards it to
  Airflow as `Authorization: Bearer`. Pasted/password tokens live in `sessionStorage` of one tab.
* **Verification.** A cookie is accepted only after Airflow answers `GET /api/v2/dags?limit=1`
  with it (standalone) or Airflow's `get_user` validated it (plugin). `exp` is checked locally
  first; verified tokens are cached for at most `auth_cache_ttl` s and never past `exp`.
  Signing out through AutoAir drops the cache entry immediately. **Audience:** Airflow's JWT
  carries Airflow's audience (`[api_auth] jwt_audience`); AutoAir is a relying party that
  forwards it to the same Airflow, so it neither re-scopes nor re-signs tokens.
* **CSRF.** A cookie is sent automatically, so in standalone mode every state-changing request
  (`POST/PUT/PATCH/DELETE`) authenticated by the cookie must carry `X-AutoAir-CSRF` (a custom
  header a foreign page cannot send without a CORS preflight AutoAir refuses) and, when the
  browser sends `Origin`, a same-origin or `cors_origins` origin (`sso.require_csrf_header`).
  `SameSite=Lax` alone is not enough: other ports of the same host and sibling sites of the
  registrable domain are *same-site*. Bearer-token requests are exempt (browsers never attach
  them on their own). Plugin mode keeps Airflow's model (Airflow's own API relies on
  `SameSite=Lax`).
* **Cookie scope.** Widening Airflow's cookie to a parent domain (proxy rewrite) sends the
  Airflow session to *every* sub-domain: only do it when all of them are trusted. Cookies AutoAir
  sets itself (a relayed refresh, the logout deletion) are `HttpOnly`, `SameSite=`
  `server.cookie_samesite`, `Secure` per `server.cookie_secure` (`auto`: when the request came
  over HTTPS — set `server.proxy_headers`/`forwarded_allow_ips` behind a TLS-terminating proxy).
* **Open redirects.** `/auth/sso/login` only accepts same-site paths as `next` (no scheme, no
  `//host`, no backslashes) and only forwards a return URL Airflow itself would accept.
* **Expiry.** Airflow's `[api_auth] jwt_expiration_time` (default 24 h) bounds every session;
  without an auth manager that refreshes, users sign in again when it ends.
* **Logout propagation is best effort**: revocation needs Airflow ≥ 3.2; with
  `sso.propagate_logout=false` AutoAir signs out locally and leaves Airflow's session alone.

## Settings (`sso` section)

| Setting | Default | |
|---|---|---|
| `sso.enabled` | `true` | Standalone: accept the Airflow cookie and offer "Sign in with Airflow" |
| `sso.cookie_name` | `_token` | Airflow's session cookie |
| `sso.airflow_public_url` | `airflow_url` | Airflow as browsers reach it (login redirect) |
| `sso.require_csrf_header` | `true` | CSRF rule above |
| `sso.refresh_margin_seconds` | `300` | Refresh/warn this long before `exp` |
| `sso.session_check_seconds` | `60` | Periodic session check (0: only on API errors) |
| `sso.propagate_logout` | `true` | AutoAir logout revokes/clears the Airflow session |
| `server.public_url` | unset | AutoAir's public URL (return URL, links in commits) |
| `server.cookie_secure`, `server.cookie_samesite` | `auto`, `lax` | Attributes of cookies AutoAir sets |

## Testing

`tests/integration/test_sso.py` runs every flow against a mocked Airflow (cookie sign-in,
custom cookie name, expiry, refresh relay, logout revocation, CSRF, redirect safety, security
headers); `frontend/src/shell/session.test.ts` covers the watcher; the Playwright spec
`frontend/e2e/sso.spec.ts` signs in with a real Airflow cookie on the same host.
