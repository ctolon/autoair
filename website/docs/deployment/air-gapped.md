---
title: Air-gapped installation
description: Install and run AutoAir without internet access - offline wheels, bundled UI assets, a prebuilt operator catalog, local logos, an internal marketplace, self-hosted LLMs and outbound controls.
sidebar_label: Air-gapped
---

AutoAir can run with no outbound internet access. Nothing in the server or UI requires an external service at runtime: the UI is bundled in the wheel, the operator catalog is built from installed packages without network access, logos ship with the package, and every outgoing client (Airflow, git hosts, marketplaces, LLMs, research, external MCP servers) is explicit and configurable.

Steps 1 to 6 below prepare the Python packages, frontend assets, operator catalog, logos, a marketplace mirror and LLM endpoints, mostly on a connected build machine; [Outbound traffic](#outbound-traffic) configures the isolated environment.

## What talks to the network

| Client (`outbound.clients` key) | Used for | Needed offline? | Offline setup |
|---|---|---|---|
| `airflow` | Airflow REST API, token checks | Yes (internal) | `AUTOAIR_AIRFLOW_URL` to the internal API server |
| `login` | Standalone password sign-in (Airflow `/auth/token`) | Yes (internal) | Same Airflow |
| `git` | Git deploy target (REST APIs or `git` CLI) | If `storage.mode=git` | Internal GitHub Enterprise / GitLab / Azure DevOps Server via `storage.git.api_url`, a self-hosted Gitea or Forgejo (`provider=gitea`), or `provider=local` with an internal remote |
| `marketplace` | Marketplace index and payload downloads | Optional | Internal `https://`, `file://` or `s3://` index, or `features.marketplace=false` |
| `ai` | LLM and embedding providers, API rerankers | Optional | Self-hosted Ollama / vLLM / LiteLLM gateway, or `features.ai=false` |
| `research` | Online documentation fetch and PyPI lookups | No | Off by default (`ai.research.online=false`) |
| `mcp` | External MCP servers used by AI agents | Optional | Internal servers only (`ai.mcp_client.servers`) |
| `opa` | Open Policy Agent decisions during validation (`policy.opa_url`) | If `policy.opa_url` is set | An internal OPA server |

No telemetry is sent. OpenTelemetry tracing is off unless `AUTOAIR_OTEL_ENABLED=true`, and then exports only to the OTLP endpoint you configure.

## 1. Python packages

On a connected machine with the same OS, CPU architecture and Python version as the target:

```bash
make build                                   # dist/autoair-<version>-py3-none-any.whl with the UI inside
mkdir wheelhouse
pip download -d wheelhouse \
  "dist/autoair-0.1.0-py3-none-any.whl[git,mcp,format,postgres,collab,shell]" \
  apache-airflow-providers-git apache-airflow-providers-amazon \
  -c "https://raw.githubusercontent.com/apache/airflow/constraints-3.3.2/constraints-3.12.txt"
cp dist/autoair-*.whl wheelhouse/
```

Transfer `wheelhouse/` and install without an index:

```bash
pip install --no-index --find-links wheelhouse \
  "autoair[git,mcp,format,postgres,collab,shell]" apache-airflow-providers-git apache-airflow-providers-amazon
```

For container builds, run the same two steps in a multi-stage Dockerfile or push the image built from `deploy/docker/Dockerfile` to your internal registry; the image needs no network at runtime.

Extras that bring their own binaries or downloads:

| Extra / feature | Offline note |
|---|---|
| `shell` | `shellcheck-py` ships the ShellCheck binary inside the wheel |
| `lsp-pyright` | `basedpyright` bundles Node.js through `nodejs-wheel`; nothing is downloaded at runtime |
| Bash language server | `npm i -g bash-language-server` from an internal npm mirror, or leave it out (the editor keeps its built-in completion and ShellCheck) |
| `ai-rerank` | `sentence-transformers` downloads models from the Hugging Face hub on first use. Pre-download the model, point `ai.retrieval.rerank_model` at a local path and set `HF_HUB_OFFLINE=1` |

The model id is passed to `sentence_transformers.CrossEncoder`, which accepts a local directory.

## 2. Frontend assets

The UI is compiled into the wheel (`autoair/static/plugin` for the plugin, `autoair/static/standalone` for `autoair serve`). It loads no scripts, fonts or styles from external hosts; the only external URLs in the UI are documentation links (for example to airflow.apache.org), which users simply cannot follow offline.

The built-in Content-Security-Policy of the standalone server allows scripts from `'self'` only and images from `https:` and `data:`. If you set `ui.logo_url`, serve the logo from an internal host or a path on your proxy.

## 3. Operator catalog

The catalog is built by parsing installed packages statically; it never contacts PyPI. Precompute it at build time so replicas start warm and identical:

```dockerfile
RUN autoair catalog build --output /opt/airflow/autoair-catalog.json
ENV AUTOAIR_CATALOG_FILE=/opt/airflow/autoair-catalog.json
```

The cache key covers installed distributions, AutoAir's version and the catalog code, so an upgraded image rebuilds rather than serving a stale catalog. To add an operator library offline, install its wheel from the wheelhouse and rebuild the catalog; see [Custom operators](/docs/extending/custom-operators).

## 4. Logos and icons

About 180 integration logos ship inside the package (`autoair/resources/logos`), together with generated concept glyphs for operators without a logo. Nothing is fetched at runtime. `scripts/fetch_logos.py` refreshes the bundled set and is a development-time tool, not part of an installation.

Custom icons uploaded in the UI are stored in AutoAir's state (`AUTOAIR_ICONS_URL`, or the database), so they stay inside the environment.

## 5. Marketplace mirror

A marketplace source is a signed `index.json` plus payload files reachable over `https://`, `file://` or any fsspec URL. In an isolated network:

1. On the connected side, build (or copy) the index: `autoair marketplace build ./market --name acme --key market.pem`.
2. Copy the folder to an internal web server, bucket or a mounted volume.
3. Pin the public key:

   ```bash
   AUTOAIR_MARKETPLACE_SOURCES='[{"name": "acme", "url": "file:///srv/autoair-market/index.json", "public_key": "<base64 ed25519>"}]'
   ```

Signature and payload hashes are verified exactly as online. `extension` entries only display a `pip install` line; install those packages from your wheelhouse. To switch the feature off instead: `AUTOAIR_FEATURES__MARKETPLACE=false`. Publishing is covered in [Publishing a marketplace](/docs/extending/marketplace).

## 6. LLM endpoints

AI features work with self-hosted models through Ollama or any OpenAI-compatible server (vLLM, LM Studio, LiteLLM, an internal gateway):

```bash
# Ollama for chat and embeddings
AUTOAIR_AI__MODEL=ollama:qwen3:14b
AUTOAIR_AI__BASE_URL=http://ollama.internal:11434
AUTOAIR_AI__EMBEDDING__PROVIDER=ollama
AUTOAIR_AI__EMBEDDING__MODEL=nomic-embed-text

# or an OpenAI-compatible server
AUTOAIR_AI__PROVIDER=openai
AUTOAIR_AI__BASE_URL=http://vllm.internal:8000/v1
AUTOAIR_AI__MODEL=qwen3-32b
```

Keep the rest of the AI stack local:

| Setting | Offline value |
|---|---|
| `ai.research.online` | `false` (default): `fetch_doc` and PyPI lookups do not exist |
| `ai.retrieval.reranker` | `none`, `llm` or `cross_encoder` (local model); not `cohere`, `voyage` or `jina` |
| `ai.user_llm.allowed_base_urls` | Internal gateway prefixes only, if users may bring their own model |
| `ai.mcp_client.servers` | Internal MCP servers only |

The AI knowledge base and the installed-environment research index are built from files in the package and installed providers. Without any model, set `AUTOAIR_FEATURES__AI=false`, or `AUTOAIR_AI__ENABLED=false` to keep the rules-only checks (leak scan, review, optimise). See [AI providers](/docs/ai/providers).

## Outbound traffic

All outgoing clients share one TLS and proxy configuration, the `outbound` settings in the table below. In an isolated network this is usually an internal CA and, if there is a controlled egress, a proxy:

```bash
AUTOAIR_OUTBOUND__CA_BUNDLE=/etc/ssl/certs/corp-root.pem
AUTOAIR_OUTBOUND__HTTPS_PROXY=http://egress.internal:3128
AUTOAIR_OUTBOUND__NO_PROXY='["localhost", "127.0.0.1", ".internal", ".svc.cluster.local"]'
AUTOAIR_OUTBOUND__TRUST_ENV=false        # ignore HTTP(S)_PROXY / SSL_CERT_FILE from the environment
AUTOAIR_OUTBOUND__CLIENTS='{"ai": {"proxy": "http://llm-egress.internal:3128"}, "git": {"ca_bundle": "/etc/ssl/gitlab.pem"}}'
```

| Setting | Default | Purpose |
|---|---|---|
| `outbound.ca_bundle` | certifi store | PEM file or hashed folder replacing the default trust store |
| `outbound.client_cert`, `outbound.client_key`, `outbound.client_key_password` | none | Mutual TLS towards servers |
| `outbound.verify` | `true` | `false` disables verification and logs a warning per client; test setups only |
| `outbound.http_proxy`, `outbound.https_proxy`, `outbound.no_proxy` | none, none, `[]` | Explicit proxies win over the environment |
| `outbound.trust_env` | `true` | Also honour `HTTP(S)_PROXY`, `NO_PROXY`, `SSL_CERT_FILE` |
| `outbound.clients` | `{}` | Per-client overrides for `airflow`, `git`, `marketplace`, `ai`, `research`, `login`, `mcp`, `opa` |

The `git` CLI (`storage.git.provider=local`) receives the same CA, verification, client certificate and proxy through the `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_<n>` and `GIT_CONFIG_VALUE_<n>` environment variables, never on the command line. A blocked destination fails the corresponding feature with a connection error; it never blocks the editor itself.

## Verification checklist

1. Start the server with egress blocked (NetworkPolicy, firewall) and open the builder: palette, validation, code preview and deploy work.
2. `GET /api/ready` returns 200 with `"catalog": "ok (<n> operators)"`.
3. The **Templates** dialog's marketplace tab lists the internal source as verified (or the tab is absent with the feature off).
4. An AI request reaches only the internal model endpoint (check the proxy or model server logs).
5. Logs contain no connection attempts to public hosts.

## Related pages

- [Installation extras](/docs/installation/extras)
- [AI providers](/docs/ai/providers)
- [Guardrails](/docs/ai/guardrails)
- [`outbound` settings](/docs/reference/configuration#outbound)
