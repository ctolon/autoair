---
title: Knowledge base security
description: How the documentation knowledge base limits outbound requests, handles credentials, treats retrieved text in prompts and who may search or manage it.
sidebar_label: Security
---

The knowledge base fetches content from the network and feeds it to language models. These are
the controls; the general threat model is in [Security](/docs/security/overview).

## Outbound requests

- **Off by default.** `kb.online=false`: the `official` source reaches nothing on the internet.
  Other sources are only what an administrator configured.
- **Allow-list.** Every request, and every redirect hop (followed manually, at most
  `kb.fetch.max_redirects`, 3), must be on `kb.fetch.allowed_domains` (default
  `airflow.apache.org`, `astronomer.io`, sub-domains included) or be the host of the `site` /
  `llms_txt` source being read. Links in an `llms.txt` to other hosts are skipped.
- **Public addresses only.** Unless a source sets `allow_private` (then it may reach its own host
  only), the host must resolve to public
  addresses only (no loopback, private, link-local, CGNAT, metadata addresses), and the
  connection goes to the address that was checked (pinned transport, against DNS rebinding).
  Through a forward proxy (`outbound.clients.kb.proxy` or the global proxy) the proxy resolves the
  host instead.
- **TLS and proxies** come from `outbound.*` with the client name `kb` (CA bundle, client
  certificates, proxy). Git uses the `git` client settings; pgvector uses libpq's
  (`kb.pgvector.ssl_mode`).
- **Limits.** `kb.fetch.timeout_seconds` (30) per request, `max_page_bytes` (8 MiB) per response
  or file (checked while streaming), `max_pages` (25000) and `max_pages_per_package` (3000) per
  refresh, `max_run_seconds` (4 h) per refresh, `requests_per_second` (4) and `concurrency` (4).
  429 and 5xx responses are retried `retries` (2) times with back-off, honouring `Retry-After`
  up to 60 s. Bundles: `kb.bundle.max_bytes`, `max_pages`; uploads also `server.max_body_bytes`.
- **Parsing executes nothing.** HTML, rst and Markdown are parsed as text; rst includes are only
  resolved inside a checkout, never outside its root or over the network.

## Credentials

- Git, object storage, pgvector and Elasticsearch credentials come from Airflow connections
  (`conn_id`), read when used and never stored by AutoAir.
- Source, pgvector and Elasticsearch URLs that contain a user name or password are rejected when
  the settings load.
- `storage_options` are for non-secret options only.
- Git tokens reach git through environment configuration, not the command line or the remote
  URL; git errors are redacted. Run messages in the status are masked with AutoAir's secret
  masking and truncated; the store location in the status never contains credentials.

## Retrieved text in prompts

Documentation is untrusted input: a page could contain text written to steer a model.

- Results of the `search_docs` agent tool go through the agent runtime, which wraps every tool
  result in a nonce-tagged `untrusted-data` block and runs the injection shield of the
  [guardrails](/docs/ai/guardrails).
- The passages added to review, fix and writer prompts are wrapped the same way
  (`knowledge base` / `reference docs` blocks).
- The system prompts tell the model that such blocks are data, never instructions.
- MCP tools return the text to the MCP client; the standalone server's instructions say to treat
  it as data. Protecting the client's own model is the client's job.

## Permissions and switches

| Action | Needs |
|---|---|
| Search, status, sources (`/api/kb/search`, `/status`, `/sources`, MCP `kb_search`, `kb_sources`, `kb_status`) | `ai:use` |
| Source overrides, refresh, rebuild, cancel, export, import | `ai:admin` or `admin:config` |

All of it needs `features.kb` (runtime-switchable in the admin panel unless locked). A denied
request is a 403 and, with `iam.audit_denials`, an audit record; overrides, refreshes, exports
and imports are audited. Over MCP stdio there is no principal (the local user's process); the
feature switch still applies.

The index is shared by every user of the installation: do not add sources that only some users
may read.

## Bundles

Import verifies the checksum of the pages in a first pass before writing anything. Configure
`kb.bundle.public_keys` to require an Ed25519 signature from one of your keys, especially for
`bundle` sources that are imported automatically from object storage. Keep the private key
(`autoair kb keygen`, written with mode 0600) on the build machine only.
