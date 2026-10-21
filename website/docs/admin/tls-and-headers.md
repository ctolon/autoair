---
title: TLS and HTTP headers
description: HTTPS and mutual TLS for autoair serve, running behind a reverse proxy with trusted forwarded headers, the security headers and Content-Security-Policy, CORS, and request limits.
---

The standalone server (`autoair serve`, and `autoair local`) handles TLS, reverse proxies, security headers, CORS and request limits itself. All settings are in the [`server` section](/docs/reference/configuration#server) and, for CORS, at the [top level](/docs/reference/configuration#top-level) (`AUTOAIR_CORS_ORIGINS`, `AUTOAIR_CORS_ALLOW_METHODS`, `AUTOAIR_CORS_ALLOW_HEADERS`, `AUTOAIR_CORS_EXPOSE_HEADERS`, `AUTOAIR_CORS_MAX_AGE`, `AUTOAIR_CORS_ALLOW_CREDENTIALS`).

:::note Plugin mode
Inside Airflow (plugin mode), Airflow's API server owns TLS, proxy handling, CORS and response headers; AutoAir adds no security headers there. Configure those in Airflow. The request-body limit still applies to AutoAir's routes.
:::

TLS for **outgoing** connections (a corporate CA for Airflow, git hosts and AI providers, client certificates, egress proxies) is configured separately in the `outbound` section; see [Configuration](/docs/admin/configuration#outgoing-connections-ca-bundle-client-certificates-proxies).

## HTTPS

`autoair serve` runs uvicorn. Give it a certificate chain and a private key to serve HTTPS directly:

```bash
autoair serve --ssl-certfile /etc/autoair/tls.pem --ssl-keyfile /etc/autoair/tls.key
# or
AUTOAIR_SERVER__SSL_CERTFILE=/etc/autoair/tls.pem
AUTOAIR_SERVER__SSL_KEYFILE=/etc/autoair/tls.key
AUTOAIR_SERVER__SSL_KEYFILE_PASSWORD=...        # only for an encrypted key; no CLI option
AUTOAIR_SERVER__SSL_MIN_VERSION=TLSv1.3         # default TLSv1.2
AUTOAIR_SERVER__SSL_CIPHERS='ECDHE+AESGCM:ECDHE+CHACHA20'   # default: Python's secure set
```

| Setting | CLI option | Default | Description |
|---|---|---|---|
| `server.ssl_certfile` | `--ssl-certfile` | unset | Certificate chain (PEM); enables HTTPS |
| `server.ssl_keyfile` | `--ssl-keyfile` | unset | Private key (PEM) |
| `server.ssl_keyfile_password` | none (secret) | unset | Password of an encrypted key |
| `server.ssl_min_version` | none | `TLSv1.2` | Lowest TLS version: `TLSv1.2` or `TLSv1.3` |
| `server.ssl_ciphers` | none | Python's default | OpenSSL cipher string (TLS 1.2 suites) |
| `server.ssl_ca_certs` | `--ssl-ca-certs` | unset | CA bundle that client certificates are verified against |
| `server.ssl_cert_reqs` | `--ssl-cert-reqs` | `none` | Client certificates: `none`, `optional`, `required` |

### Mutual TLS

To require client certificates, set the CA that issues them and `required` (or `optional` to accept clients without one):

```bash
autoair serve --ssl-certfile /etc/autoair/tls.pem --ssl-keyfile /etc/autoair/tls.key \
  --ssl-ca-certs /etc/autoair/clients-ca.pem --ssl-cert-reqs required
```

Client certificates authenticate the TLS connection only. Users are still identified by their Airflow session or token ([SSO and sessions](/docs/admin/sso)).

### Configuration errors

Inconsistent TLS options stop the server before it binds (exit code `2`):

* `server.ssl_cert_reqs needs server.ssl_ca_certs (mutual TLS)`: `optional` or `required` without a CA bundle.
* `server.ssl_keyfile / ssl_cert_reqs need server.ssl_certfile`: a key or client-certificate requirement without a certificate.

Paths may use `~`. Certificates are read at start-up: renewing them needs a restart (a rolling restart with several replicas).

### TLS at a proxy instead

Terminating TLS at a load balancer or ingress is equally supported. AutoAir then listens on plain HTTP inside the network, and the proxy must be trusted for `X-Forwarded-Proto` so that AutoAir knows the original request was HTTPS (for `Secure` cookies, HSTS and links). See the next section.

## Reverse proxies and forwarded headers

| Setting | CLI option | Default | Description |
|---|---|---|---|
| `server.proxy_headers` | none | `true` | Trust `X-Forwarded-Proto` and `X-Forwarded-For` from the addresses below |
| `server.forwarded_allow_ips` | `--forwarded-allow-ips` (comma-separated) | `["127.0.0.1"]` | Proxy addresses whose `X-Forwarded-*` headers are trusted; `*` trusts every peer |
| `server.public_url` | none | unset | AutoAir's public base URL as users reach it, for example `https://autoair.example.com` or `https://airflow.example.com/autoair` |

With the defaults, only a proxy on the same host is trusted. Behind a load balancer or an ingress controller, list its address (or the pod network range when it is not stable):

```bash
AUTOAIR_SERVER__FORWARDED_ALLOW_IPS='["10.0.0.10"]'
# or
autoair serve --forwarded-allow-ips 10.0.0.10,10.0.0.11
```

What depends on it:

* the **client IP** used by the sign-in rate limiter and, for unauthenticated calls, the API rate limiter. Without a trusted proxy, every user shares the proxy's address and one user's failures lock everybody out;
* the **scheme**: `server.cookie_secure=auto` sets `Secure` and HSTS is sent only when the request is known to be HTTPS.

Use `*` only when the proxy strips incoming `X-Forwarded-*` headers from clients; otherwise clients can spoof their address.

`server.public_url` is not derived from headers. Set it whenever AutoAir is behind a proxy: it is used for the return URL of "Sign in with Airflow" and for the link back to a DAG in commits and pull requests (unset: no links).

## Security headers

With `server.security_headers=true` (the default), the standalone server adds these headers to every response that does not set them itself:

| Header | Value | Setting |
|---|---|---|
| `Content-Security-Policy` | the built-in policy below, or your own | `server.content_security_policy`, `server.frame_ancestors` |
| `X-Content-Type-Options` | `nosniff` | always |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | `server.referrer_policy` (empty: not sent) |
| `X-Frame-Options` | `SAMEORIGIN` when `frame_ancestors` is `["'self'"]`, `DENY` when it is `["'none'"]`, not sent otherwise | `server.frame_ancestors` |
| `Strict-Transport-Security` | `max-age=<hsts_seconds>` (+ `includeSubDomains`, `preload`) on HTTPS responses only | `server.hsts_seconds`, `server.hsts_include_subdomains`, `server.hsts_preload` |

uvicorn's `Server` header is not sent.

### Content-Security-Policy

The built-in policy is written for the single-page app:

```text
default-src 'self';
script-src 'self' 'sha256-<hash of the page's inline configuration script>';
style-src 'self' 'unsafe-inline';
img-src 'self' data: blob: https:;
font-src 'self' data:;
connect-src 'self';
worker-src 'self' blob:;
object-src 'none';
base-uri 'self';
form-action 'self';
frame-ancestors 'self'
```

* Scripts are limited to AutoAir's own files plus the exact inline configuration script of the UI page, allowed by its SHA-256 hash.
* Inline styles are allowed because Chakra UI (emotion) and CodeMirror inject `<style>` elements at runtime.
* `img-src https:` lets `ui.logo_url` point to any HTTPS image.
* `frame-ancestors` comes from `server.frame_ancestors`.

To allow embedding the UI in a portal, add its origin:

```bash
AUTOAIR_SERVER__FRAME_ANCESTORS="[\"'self'\", \"https://portal.example.com\"]"
```

```yaml
# the same in YAML
server:
  frame_ancestors: ["'self'", "https://portal.example.com"]
```

`["'none'"]` forbids embedding entirely.

`server.content_security_policy` replaces the whole header with your own string. `frame_ancestors` is then ignored (put `frame-ancestors` in your policy), no `X-Frame-Options` is sent, and the UI page's inline-script hash is no longer added: your policy must allow the UI's inline configuration script or the UI will not start. Test a custom policy in a browser before rolling it out.

### HSTS

`Strict-Transport-Security` is off by default (`server.hsts_seconds=0`). Enable it only when every client reaches AutoAir over HTTPS, for every host name it is served on:

```bash
AUTOAIR_SERVER__HSTS_SECONDS=31536000
AUTOAIR_SERVER__HSTS_INCLUDE_SUBDOMAINS=false
```

HSTS is sent only on responses AutoAir knows were served over HTTPS (directly, or through a trusted proxy that sets `X-Forwarded-Proto: https`).

### Disabling the headers

`AUTOAIR_SERVER__SECURITY_HEADERS=false` removes all of them, for example when a proxy in front sets its own. `/api/config` and `autoair config show` mask this setting's value as `***` because its name ends in `headers` (see [Secrets](/docs/admin/configuration#secrets)).

## CORS

Cross-origin requests are refused unless you list origins. CORS applies to the standalone server only.

| Setting | Default | Description |
|---|---|---|
| `AUTOAIR_CORS_ORIGINS` | `[]` | Allowed origins, for example `["https://portal.example.com"]` |
| `AUTOAIR_CORS_ALLOW_METHODS` | `GET, POST, PUT, PATCH, DELETE, OPTIONS` | Methods allowed cross-origin |
| `AUTOAIR_CORS_ALLOW_HEADERS` | `Authorization, Content-Type, If-Match, X-AutoAir-CSRF, X-Requested-With` | Request headers allowed cross-origin |
| `AUTOAIR_CORS_EXPOSE_HEADERS` | `ETag, Retry-After` | Response headers readable cross-origin |
| `AUTOAIR_CORS_MAX_AGE` | `600` | Seconds browsers cache a preflight |
| `AUTOAIR_CORS_ALLOW_CREDENTIALS` | `true` | Allow cookies and credentials cross-origin |

* With `["*"]`, `Access-Control-Allow-Credentials` is **never** sent, whatever `cors_allow_credentials` says: a wildcard would otherwise reflect any origin with credentials. List explicit origins to allow credentialed calls.
* Origins in `cors_origins` are also accepted by the [CSRF check](/docs/admin/sso#csrf-protection) for cookie-authenticated state-changing requests.
* `ETag` and `If-Match` are part of the defaults because spec saves use optimistic locking.

## Request limits and compression

| Setting | Default | Description |
|---|---|---|
| `server.max_body_bytes` | `16777216` (16 MiB) | Largest request body; above it `413` with `request body larger than N bytes`. Checked on `Content-Length` and while streaming. Per-feature limits (for example `interchange.max_source_chars`) still apply |
| `server.limit_concurrency` | unset | Most concurrent connections before uvicorn answers `503` |
| `server.keep_alive_timeout` | `5` | Idle seconds before a keep-alive connection closes |
| `server.graceful_shutdown_timeout` | `30` | Seconds in-flight requests get on shutdown |
| `server.gzip_min_size`, `server.gzip_level` | `2048`, `6` | Responses above this size are gzip-compressed |
| `server.static_max_age` | `31536000` | `Cache-Control` max-age of content-hashed UI assets (immutable); entry files are always revalidated |

Per-user API rate limits (`limits.*`, `429` with `Retry-After`) are described in [Configuration](/docs/admin/configuration#logging-rate-limits-and-ui).

## Cookies

Attributes of the cookies AutoAir sets (`server.cookie_secure`, `server.cookie_samesite`) and the CSRF header rule are described in [SSO and sessions](/docs/admin/sso#cookies).

## Production example

```bash
autoair serve --workers 4 \
  --ssl-certfile /etc/autoair/tls.pem --ssl-keyfile /etc/autoair/tls.key
AUTOAIR_SERVER__PUBLIC_URL=https://autoair.acme.io
AUTOAIR_SERVER__FORWARDED_ALLOW_IPS='["10.0.0.10"]'      # the load balancer
AUTOAIR_SERVER__HSTS_SECONDS=31536000                    # only when HTTPS everywhere
AUTOAIR_SERVER__FRAME_ANCESTORS="[\"'none'\"]"      # never embedded
AUTOAIR_SERVER__COOKIE_SECURE=always
```

See also [Standalone behind a proxy](/docs/deployment/standalone-proxy) and the [production checklist](/docs/admin/overview#production-checklist).
