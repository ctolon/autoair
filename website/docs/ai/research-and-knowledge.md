---
title: Retrieval, research and knowledge
description: How AutoAir's AI finds reference material (hybrid retrieval, reranking, air-gapped and online research) and how administrators steer it with the knowledge base, prompt overrides, project rules and memory.
sidebar_label: Retrieval and knowledge
---

AI answers are only as good as the material the model sees. AutoAir grounds its prompts in what is
installed: the operator catalog, the installed provider packages, the knowledge base and your saved
DAGs.

## Retrieval

Retrieval feeds the agent's search tools, the writers' reference docs, the developer assistant and
`GET /api/ai/search`.

**Corpus.** Catalog operators (summary, parameters, docstring), the enabled knowledge-base
documents, saved DAG specs (at most `ai.retrieval.max_specs`, 300) and templates
(`ai.retrieval.max_templates`, 300). Research adds installed provider docstrings and metadata,
example DAGs and fetched pages.

**Chunking.** Markdown is split by heading path with code fences kept intact; Python by syntax tree
(module docstring, top-level functions, classes split per method when large); plain text in paragraph
windows. Target size `ai.retrieval.chunk_size` (1200 characters), overlap `chunk_overlap` (150).

**Hybrid search.**

1. **BM25** with identifier-aware tokens and a title boost (`bm25_k1` 1.4, `bm25_b` 0.7,
   `title_boost` 3).
2. **Dense embeddings** when `ai.embedding.provider` is set ([Embeddings](/docs/ai/providers#embeddings)).
3. **Reciprocal Rank Fusion** of both rankings (`rrf_k`, 60). Each ranker contributes
   `candidate_pool` (40) candidates. Several query variants are fused the same way.
4. **Reranking** of the top `rerank_top_n` (20) fused candidates, when a reranker is configured.
5. The top `top_k` (8) hits are returned; at most `context_max_chars` (4000) of retrieved text is
   injected into one prompt.

**Query expansion.** Deterministic Airflow and cloud vocabulary (for example `k8s` → `kubernetes`, `bq` →
`bigquery`, `s3` → `aws`; the built-in table is `SYNONYMS` in `autoair/ai/query.py`) is added at reduced BM25 weight (`query_expansion`, on). Add site terms
with `synonyms`, for example `{"dwh": ["snowflake", "warehouse"]}`. `llm_query_rewrite` (off) lets the
model rewrite research queries at the cost of one extra call.

**Rerankers** (`ai.retrieval.reranker`):

| Value | How | Data leaves the server |
|---|---|---|
| `none` (default) | Fused order | no |
| `cross_encoder` | Local sentence-transformers model (`pip install 'autoair[ai-rerank]'`, pulls PyTorch) | no |
| `llm` | The configured chat model scores the candidates | to the chat provider |
| `cohere`, `voyage`, `jina` | Hosted rerank API over httpx2 (`rerank_api_key`, optional `rerank_base_url`, `rerank_model`) | to that API |

A reranker failure keeps the fused order; it never fails the request.

**Metadata filters.** Searches can be restricted by `kind` and any metadata key (exact or any-of), for
example research limited to one provider package.

**Indexing and caching.** Sources are re-read after `index_ttl_seconds` (120); the index is rebuilt
only when a source's fingerprint changed, and vectors are kept per text hash, so only new or changed
chunks are embedded. Query results are cached (`query_cache_size` 256, `query_cache_ttl_seconds` 300),
and vectors in an on-disk SQLite cache (`embedding_cache_max_entries` 100000).

Ranking quality is measured by a labelled evaluation set in the test suite (recall@k and MRR for
BM25, hybrid and reranked retrieval), so ranking changes are checked rather than guessed.

```bash
# Hybrid retrieval with OpenAI embeddings and a hosted reranker
AUTOAIR_AI__EMBEDDING__PROVIDER=openai
AUTOAIR_AI__RETRIEVAL__RERANKER=cohere
AUTOAIR_AI__RETRIEVAL__RERANK_API_KEY=...
AUTOAIR_AI__RETRIEVAL__SYNONYMS='{"dwh": ["snowflake", "warehouse"]}'
```

## Research

Research answers questions about the packages AutoAir and your workers actually have installed. It is
exposed as agent tools (`search_docs`, `read_source`, `list_installed_packages`, `get_package_info`,
`fetch_doc`), as MCP tools (`search_installed_docs`, `read_source`, `list_installed_packages`,
`get_package_info`, `fetch_doc`) and as `GET /api/ai/research/search`.

### Air-gapped (default)

With `ai.research.online=false` nothing is fetched from the internet. The index covers:

- installed distributions and versions, and Airflow provider metadata from each provider's
  `get_provider_info` entry point (operators, hooks, sensors, triggers, connection types);
- docstrings and signatures of the modules of distributions matching
  `ai.research.package_patterns` (default `apache-airflow*`, `airflow*`), parsed with `ast` without
  importing anything, at most `max_index_modules` (4000) modules (`index_installed` turns this off);
- full example DAGs (`example_dags/`) of those distributions;
- AutoAir's docs and the knowledge base.

`read_source` returns a module, `module.Class`, `Class.method` or a file, at most `max_source_chars`
(60000). It is confined to site-packages, AutoAir's custom operator folders and
`ai.research.extra_source_dirs`; paths are resolved, and symlink escapes and other file types are
refused.

### Online

`ai.research.online=true` enables `fetch_doc` and PyPI data in `get_package_info`:

| Setting | Default | Meaning |
|---|---|---|
| `allowed_domains` | `airflow.apache.org`, `pypi.org`, `raw.githubusercontent.com` | Hosts that may be fetched (sub-domains included) |
| `proxy` | – | HTTP(S) proxy |
| `timeout_seconds` | 15 | One fetch |
| `max_bytes` | 2097152 | Largest body read |
| `max_redirects` | 3 | Redirects followed; every hop is checked again |
| `rate_limit_per_minute` | 30 | Per domain (token bucket) |
| `cache_ttl_seconds` / `cache_dir` | 86400 / `<state>/ai-research-cache` | On-disk fetch cache |
| `user_agent` | `AutoAir-Research/1.0 (+https://github.com/ctolon/autoair)` | User-Agent header |

Private, loopback, link-local and cloud-metadata addresses are refused, with the same pinned-IP check
as [user endpoints](/docs/ai/providers#private-network-restrictions). HTML is reduced to readable
text, fetched pages become searchable, and fetched text is treated as untrusted data like every tool
output.

The prompts tell the model to search installed docs first, confirm parameters with `read_source`, and
use online docs only for what the installed packages do not explain.

## Knowledge base

:::tip Official documentation
The official Apache Airflow, provider and Astronomer documentation (online or air-gapped, with
citations) is indexed by the separate [documentation knowledge base](knowledge-base.md); the
`search_docs` research tool searches both.
:::

The knowledge base is a set of Markdown guides the AI reads through retrieval and that MCP clients
read as `autoair://docs/{name}` resources. AutoAir ships `dagspec-authoring` and
`airflow3-best-practices`; when running from a source checkout, AutoAir's own `docs/` folder is indexed
too (`ai.knowledge.include_repo_docs`).

Administrators (`ai:admin`) manage it under **AI → AI knowledge base…**:

- **switch off** a document (**Use …** toggle), or list built-in ids in `ai.knowledge.disabled`;
- **replace** a document: **Save as override** in the UI, **Upload .md**, or a file of the same name
  in one of `ai.knowledge.extra_dirs` (paths or fsspec URLs);
- **reset** a document (**Reset to default**): deletes the override and UI toggles;
- add **New document**s (ids: lower-case letters, digits, `.`, `-`, `_`).

Each document shows its origin: *built-in*, *site folder*, *overridden* or *custom*. Uploads are limited
to `ai.knowledge.max_upload_bytes` (512 KB). Overrides live under `ai.knowledge.store_url` (default
`<state>/ai-knowledge`). Changes invalidate the retrievers immediately and are recorded in the audit
trail.

API (all need `ai:admin`): `GET /api/ai/knowledge`, `GET|PUT|DELETE /api/ai/knowledge/{id}`,
`POST /api/ai/knowledge/{id}/enabled`.

## Prompts

Every system prompt can be replaced or extended without a code change. For a prompt `NAME`:

1. `ai.prompts.override_dir`: the file `<NAME>.md` replaces the prompt. This wins over everything
   else and is re-read when the file changes.
2. `ai.prompts.replace`: `{NAME: text}` replaces it.
3. `ai.prompts.append`: `{NAME: text}` is appended to it.

Prompt names: `ADMIN_ASSISTANT_SYSTEM`, `AGENT_SYSTEM`, `COMPACT_SYSTEM`, `DEPLOY_MESSAGE_SYSTEM`,
`DESCRIBE_SYSTEM`, `DEVELOPER_ASSISTANT_SYSTEM`, `EXPLAIN_CODE_SYSTEM`, `EXPLAIN_DAG_SYSTEM`,
`EXPLAIN_PROBLEMS_SYSTEM`, `FIX_SYSTEM`, `LEAKS_SYSTEM`, `OPTIMIZE_SYSTEM`, `REVIEW_SYSTEM`,
`TASK_CODE_SYSTEM`, `TROUBLESHOOT_SYSTEM`, `UPGRADE_SYSTEM`, `WRITER_BASH_SYSTEM`,
`WRITER_CUSTOM_OPERATOR_SYSTEM`, `WRITER_INLINE_FUNCTION_SYSTEM`, `WRITER_OPERATOR_SYSTEM`,
`WRITER_SQL_SYSTEM`, `WRITER_TASK_BODY_SYSTEM`. `GET /api/ai/prompts` (`ai:admin`) lists them with
whether each is overridden.

```yaml
ai:
  prompts:
    organization: Acme Data Platform
    append:
      DEVELOPER_ASSISTANT_SYSTEM: "Always answer in German."
    variables:
      oncall: "#data-oncall"
```

### Prompt variables

`${name}` placeholders are replaced in every prompt. Unknown names are left untouched, and Jinja such
as `{{ ds }}` is never affected.

| Variable | Value |
|---|---|
| `${organization}` | `ai.prompts.organization` (default "your organisation") |
| `${user}` | The caller |
| `${team}` | The server's team (`team` setting) |
| `${assistant}` | `developer`, `admin` or `troubleshoot` |
| `${airflow_version}`, `${autoair_version}` | Installed versions |
| `${today}` | ISO date |
| `${online_research}` | `enabled` or `disabled (air-gapped)` |
| any key of `ai.prompts.variables` | Your own values |

## Project rules

Project rules are Markdown written by administrators and appended to every authoring and writing
prompt: generation, review, optimise and fix, task code and the writers, explain-DAG, the agent, the
developer assistant, troubleshooting and upgrade. The model is told they override general best
practice but never safety rules.

Manage them in the admin panel's **AI rules** tab (**Add rules**, **Scope**, **Rules (Markdown)**,
**Preview**) or through the API:

| Endpoint | Permission | |
|---|---|---|
| `GET /api/ai/rules` | `ai:use` | All rules |
| `GET /api/ai/rules/effective` | `ai:use` | What a prompt on this server receives |
| `PUT /api/ai/rules/{scope}` | `ai:admin` | Create or replace; `scope` is `global` or `team:<name>` |
| `DELETE /api/ai/rules/{scope}` | `ai:admin` | Delete |

Global rules apply to everyone; `team:<name>` rules apply to the server whose `team` setting matches.
Settings: `ai.rules.enabled` (on), `max_chars` (8000 characters injected per prompt),
`store_url` (default `<state>/ai-rules`, rows with a SQL database backend), `cache_seconds` (10).
Changes are audited (`ai_rules_put`, `ai_rules_delete`).

## Memory

A small persistent store of conventions and preferences (for example "warehouse loads use
`snowflake_prod`") is injected, bounded, into the prompts of AutoAir's AI features and is shared with
MCP clients. Scopes: `global`, `team` and `user`. The developer assistant uses `list_conventions` and
`remember`; MCP clients use `list_memories`, `remember` and `forget`. Text that looks like a credential
is rejected. Location: `ai.memory_url` (default `<state>/ai-memory`); size:
`ai.assist.memory_max_items` (2000).

## Related pages

- [`ai.retrieval`](/docs/reference/configuration#airetrieval), [`ai.research`](/docs/reference/configuration#airesearch), [`ai.knowledge`](/docs/reference/configuration#aiknowledge), [`ai.prompts`](/docs/reference/configuration#aiprompts), [`ai.rules`](/docs/reference/configuration#airules)
- [Guardrails](/docs/ai/guardrails): how retrieved text is treated as untrusted data
- [MCP](/docs/ai/mcp): research tools and knowledge resources for external agents
