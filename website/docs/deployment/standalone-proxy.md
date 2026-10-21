---
title: Standalone behind a reverse proxy
description: Run autoair serve behind nginx, Traefik or another reverse proxy with correct forwarded headers, WebSockets, unbuffered AI streaming and public URLs.
sidebar_label: Standalone behind a proxy
---

`autoair serve` is a uvicorn server. In production put it behind a reverse proxy or ingress that terminates TLS. Three kinds of traffic must pass through intact:

| Traffic | Paths | Proxy requirement |
|---|---|---|
| Regular HTTP | `/ui/...`, `/api/...` | Forward `X-Forwarded-Proto` and `X-Forwarded-For`; allow bodies up to `server.max_body_bytes` (16 MiB) |
| WebSockets | `/collab/<dag_id>` (live co-editing), `/api/lsp/ws/<language>` (IDE language servers, when `features.ide_language_server=true`) | Pass `Upgrade` / `Connection` headers; long idle timeout |
| Server-sent events | `/api/ai/assistant/chat/stream` (AI chat), `/api/ai/ask/stream` (questions), `/api/ai/write/stream` (writers), `/api/ai/explain-code/stream` (explanations), `/api/ai/troubleshoot/stream` (troubleshooting) | No response buffering, no compression of `text/event-stream`, read timeout above the longest agent run |

## AutoAir settings

```bash
autoair serve --host 0.0.0.0 --port 8765 --workers 2
```

```bash
AUTOAIR_SERVER__PUBLIC_URL=https://autoair.example.com        # links in commits/PRs, SSO return URL
AUTOAIR_SERVER__PROXY_HEADERS=true                            # default
AUTOAIR_SERVER__FORWARDED_ALLOW_IPS='["10.0.0.10","10.0.0.11"]'  # the proxy addresses
AUTOAIR_SERVER__HSTS_SECONDS=31536000                         # only when every client uses HTTPS
AUTOAIR_SERVER__COOKIE_SECURE=auto                            # Secure when X-Forwarded-Proto is https
AUTOAIR_SSO__AIRFLOW_PUBLIC_URL=https://airflow.example.com   # Airflow as browsers reach it
AUTOAIR_AIRFLOW_URL=http://airflow-api-server:8080            # Airflow as AutoAir reaches it
AUTOAIR_COLLAB_SECRET=...                                     # several workers or replicas
```

| Setting | Default | Why it matters behind a proxy |
|---|---|---|
| `server.proxy_headers` | `true` | Uvicorn takes the scheme and client address from `X-Forwarded-Proto` / `X-Forwarded-For` |
| `server.forwarded_allow_ips` | `["127.0.0.1"]` | Only these peers are trusted to set forwarded headers. Set the proxy's addresses. `*` trusts every peer and is only safe when nothing but the proxy can reach the port |
| `server.public_url` | unset | Without it commits and pull requests carry no link back, and "Sign in with Airflow" cannot compute a return URL |
| `server.cookie_secure` | `auto` | `auto` marks cookies `Secure` when the request came over HTTPS; this depends on trusted forwarded headers |
| `server.hsts_seconds` | `0` | AutoAir sends `Strict-Transport-Security` only on HTTPS responses and only when > 0 |
| `server.max_body_bytes` | `16777216` | Bodies above it get 413; set the proxy's limit to at least the same |
| `server.keep_alive_timeout` | `5` | Keep the proxy's upstream keep-alive below this, or raise it, to avoid races on reused connections |
| `server.frame_ancestors` | `["'self'"]` | Add your portal if it embeds AutoAir in a frame |

The proxy-related options are also flags: `--forwarded-allow-ips`, `--workers`, `--ssl-certfile`, `--ssl-keyfile`, `--ssl-ca-certs`, `--ssl-cert-reqs`. TLS between proxy and AutoAir (or mutual TLS) is configured with `server.ssl_certfile`, `server.ssl_keyfile`, `server.ssl_keyfile_password`, `server.ssl_ca_certs`, `server.ssl_cert_reqs`, `server.ssl_ciphers` and `server.ssl_min_version`; see [TLS and HTTP headers](/docs/admin/tls-and-headers).

### Host name, not a sub-path

Serve the standalone UI on its own host name (`https://autoair.example.com/`). The UI computes its API base from the ASGI root path, and `autoair serve` does not set one, so a proxy that strips a prefix such as `/autoair` produces a UI that calls `/api` at the host root. The plugin is the supported way to run under Airflow's host and path (`/autoair/...`).

`autoair serve` has no `root_path` flag or `server.*` setting; the API base is derived from the ASGI `root_path` only when a host server mounts AutoAir under a prefix, which is what the Airflow plugin does.

### Signing in through the proxy

Standalone AutoAir accepts Airflow's `_token` session cookie only when the browser sends it, which depends on host names:

| Layout | Result |
|---|---|
| AutoAir and Airflow on the same host (different ports, or paths behind one proxy) | The host-only cookie reaches AutoAir; users are signed in automatically |
| Sibling sub-domains (`autoair.example.com`, `airflow.example.com`) | Only if the proxy in front of Airflow widens the cookie domain, for example nginx `proxy_cookie_domain airflow.example.com .example.com;`. This sends Airflow's session to every sub-domain: do it only when all of them are trusted |
| Unrelated hosts | No cookie: users sign in with their Airflow username and password (`iam.login_enabled`, proxied once to Airflow's `/auth/token`) or paste a token |

Details and the CSRF rules for cookie sessions are on [SSO](/docs/admin/sso).

## nginx

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

upstream autoair {
    server 10.0.1.21:8765;
    server 10.0.1.22:8765;
    keepalive 32;
}

server {
    listen 443 ssl;
    http2 on;
    server_name autoair.example.com;
    ssl_certificate     /etc/nginx/tls/autoair.pem;
    ssl_certificate_key /etc/nginx/tls/autoair.key;

    client_max_body_size 16m;          # server.max_body_bytes

    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection        "";

    location / {
        proxy_pass http://autoair;
        proxy_read_timeout 60s;
    }

    # Live co-editing and language servers (WebSockets).
    location /collab/ {
        proxy_pass http://autoair;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }
    location /api/lsp/ws/ {
        proxy_pass http://autoair;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 1h;
    }

    # AI streaming (text/event-stream): never buffer, long reads.
    location /api/ai/ {
        proxy_pass http://autoair;
        proxy_buffering off;
        proxy_cache off;
        gzip off;
        proxy_read_timeout 900s;      # above ai.agent.timeout_seconds / ai.troubleshoot.timeout_seconds
    }
}
```

Notes:

- The `/api/ai/` block applies to all AI routes; unbuffered proxying is harmless for the non-streaming ones. Some streaming responses also carry `X-Accel-Buffering: no`, but do not rely on the header alone.
- AutoAir sends an SSE comment every `ai.stream_heartbeat_seconds` (15 s), so the read timeout only has to exceed the gap between events; the long timeout protects slow model calls.
- With the file state backend and several replicas, add sticky routing for `/collab/` (for example `hash $request_uri consistent;` in a separate upstream) so all editors of a DAG reach one replica. With PostgreSQL or Redis collaboration fan-out this is not needed.

## Traefik

Traefik passes WebSockets without extra configuration and does not buffer responses unless the buffering middleware is enabled. Use a router per host and keep compression off for event streams:

```yaml
# dynamic configuration (file provider)
http:
  routers:
    autoair:
      rule: Host(`autoair.example.com`)
      entryPoints: [websecure]
      service: autoair
      tls: {}
  services:
    autoair:
      loadBalancer:
        servers:
          - url: http://10.0.1.21:8765
          - url: http://10.0.1.22:8765
        passHostHeader: true
```

```yaml
# static configuration: long-lived streams
entryPoints:
  websecure:
    address: ":443"
    transport:
      respondingTimeouts:
        readTimeout: 0       # 0 = no limit on reading requests (Traefik default in v3)
        idleTimeout: 3600s
```

If you add the `compress` middleware, exclude `text/event-stream` (`excludedContentTypes`). For sticky collaboration with the file backend, enable `loadBalancer.sticky.cookie` on the service.

Set `AUTOAIR_SERVER__FORWARDED_ALLOW_IPS` to the Traefik pods' addresses (or the pod CIDR when only Traefik can reach AutoAir).

## Kubernetes ingress-nginx

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: autoair
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "16m"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "900"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "900"
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
spec:
  ingressClassName: nginx
  tls: [{ hosts: [autoair.example.com], secretName: autoair-tls }]
  rules:
    - host: autoair.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: { service: { name: autoair, port: { number: 8765 } } }
```

ingress-nginx upgrades WebSockets automatically. `proxy-buffering: "off"` applies to the whole host; that is acceptable for AutoAir.

## The plugin behind a proxy

In plugin mode AutoAir is part of Airflow's API server, so the proxy in front of Airflow carries its traffic. Apply the same three rules to Airflow's ingress for the `/autoair/collab/`, `/autoair/api/lsp/ws/` and `/autoair/api/ai/` paths. Airflow's own `[api] base_url` and proxy settings decide forwarded-header handling; the `server.*` settings of `autoair serve` do not apply.

## Verify

```bash
curl -s https://autoair.example.com/api/health          # {"status":"ok","version":"..."}
curl -s https://autoair.example.com/api/ready           # 200 with checks, or 503
curl -sI https://autoair.example.com/ui/ | grep -i -E 'strict-transport|content-security'
```

Then open a DAG in two browsers and click **Collaborate**; both should show each other's presence. Ask the AI assistant a question and confirm the answer streams in progressively rather than arriving at once (buffering).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Redirects or links use `http://` | Forwarded headers not trusted: `forwarded_allow_ips` does not contain the proxy |
| Cookies not `Secure` behind HTTPS | Same as above (`cookie_secure=auto` sees plain HTTP) |
| Collaboration stuck on "connecting" | WebSocket upgrade headers not forwarded, or the proxy closes idle connections |
| Collaborators on different replicas do not see each other | File backend without sticky `/collab/` routing; use PostgreSQL or sticky sessions |
| AI answers appear only at the end | Proxy buffering or compression on `text/event-stream` |
| AI streams cut off after 60 s | Proxy read timeout too short |
| 413 on import or deploy | Proxy body limit below `server.max_body_bytes` |

## Related pages

- [Standalone installation](/docs/installation/standalone)
- [TLS and HTTP headers](/docs/admin/tls-and-headers)
- [SSO](/docs/admin/sso)
- [Kubernetes reference architecture](/docs/deployment/kubernetes)
