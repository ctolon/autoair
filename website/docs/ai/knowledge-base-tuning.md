---
title: Tuning the knowledge base
description: Chunking, retrieval, embeddings and reranking settings of the documentation knowledge base, with their defaults and trade-offs.
sidebar_label: Tuning
---

The defaults suit the Airflow documentation; change them when answers miss context or return
too much. Every field is in the [`kb` settings reference](/docs/reference/configuration#kb).

## Chunking (`kb.chunk`)

| Setting | Default | Effect |
|---|---|---|
| `size` | 1200 | Target characters per chunk. Larger chunks carry more context per hit and fewer hits fit a prompt |
| `overlap` | 150 | Characters shared by adjacent chunks of a long section; must be below half of `size` |
| `min_chars` | 200 | Sections shorter than this are merged with the next section of the page (0: never) |
| `max_chunks_per_page` | 400 | Chunks kept per page; the rest is dropped |

Chunks never span two sections (after merging short ones), so each hit has one heading path
and one anchor. Changing any of these re-chunks the stored pages on the next refresh, without
fetching.

## Retrieval (`kb.search`)

| Setting | Default | Effect |
|---|---|---|
| `top_k` | 6 | Results when the caller does not say |
| `max_results` | 20 | Hard cap for every caller (API, MCP, agents) |
| `candidate_pool` | 50 | Candidates per ranker before fusion (at least 5 x the requested results) |
| `rrf_k` | 60 | Reciprocal Rank Fusion constant; higher flattens the influence of the top ranks |
| `max_per_page` | 2 | Results from one page, so one long page does not fill the list |
| `snippet_chars` | 320 | Snippet length around the first query term |
| `max_text_chars` | 1500 | Chunk text returned per result |
| `context_max_chars` | 3000 | Documentation text added to one review, fix or writer prompt |

Query expansion uses AutoAir's Airflow vocabulary plus `ai.retrieval.synonyms`, the same as the
other retrieval in AutoAir ([Retrieval](/docs/ai/research-and-knowledge#retrieval)).

## Embeddings

Hybrid search needs `autoair[ai]` and an embeddings provider in `ai.embedding`
([Embeddings](/docs/ai/providers#embeddings)); the KB reuses it rather than having its own.
`kb.search.embeddings=false` keeps the KB keyword-only even when embeddings are configured for
the rest of AutoAir.

- Chunks are embedded during refreshes in batches of `ai.embedding.batch_size`, as title +
  heading + text. Vectors are cached by model and text hash in `<kb cache>/embeddings.sqlite3`.
- Configuring an embedder for an existing index embeds its chunks on the next refresh; changing
  the model clears the vectors and re-embeds. Until then the index stays searchable by keyword.
- The query vector is used only when the index holds vectors of the configured model; otherwise
  the search is lexical. `GET /api/kb/status` reports `mode` (`hybrid` or `lexical`) and the
  embedding model; each search result reports its own `mode`.
- After three consecutive embedding failures a refresh stops embedding, keeps indexing keywords
  and records an error; the next refresh fills the missing vectors.

Embedding sends documentation text to the embeddings provider; with Ollama or another local
endpoint nothing leaves your network.

## Reranking

With `kb.search.rerank` (on) and `ai.retrieval.reranker` set (`cross_encoder`, `llm`, `cohere`,
`voyage`, `jina`), the top `kb.search.rerank_top_n` (20) fused candidates are reranked
([Rerankers](/docs/ai/research-and-knowledge#retrieval)). A failing reranker keeps the fused
order. In `ai.llm_mode=per_user` the central model is not used as an `llm` reranker on a user's
behalf. `autoair kb search` and the passages added to review, fix and writer prompts are not
reranked.

## Size of the index

With the default selection (core, Task SDK, installed providers, Helm chart, airflowctl,
Astronomer guides) the size depends mostly on how many providers are installed;
`autoair kb status` shows pages per component. To keep it small:

- leave `kb.providers=installed` and narrow `kb.provider_include`;
- keep `kb.fetch.include_api_reference=false`;
- index one version (`kb.versions=["installed"]`);
- exclude page families with a source's `exclude` patterns, for example `["changelog", "*/changelog"]`.
