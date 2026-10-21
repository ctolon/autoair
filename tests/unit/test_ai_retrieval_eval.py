"""Retrieval quality on a small labelled eval set (tests/unit/data/ai_retrieval_eval.json):
operator docs + the shipped knowledge base, 20 realistic queries.

Recall@k = share of queries with at least one relevant document in the top k. The
thresholds guard against ranking regressions (tokenisation, BM25 weights, expansion,
fusion); they are not a benchmark of absolute quality.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path

import pytest

from autoair.ai.config import KnowledgeSettings, RetrievalSettings
from autoair.ai.knowledge import KnowledgeBase
from autoair.ai.rag import Document, Retriever, tokenize

DATA = json.loads((Path(__file__).parent / "data" / "ai_retrieval_eval.json").read_text())


def corpus(tmp: Path) -> list[Document]:
    kb = KnowledgeBase(KnowledgeSettings(include_repo_docs=False), str(tmp / "kb"))
    return [Document.model_validate(d) for d in DATA["corpus"]] + kb.documents()


class HashEmbedder:
    """Deterministic "semantic" stand-in: hashed bag of stemmed tokens + character trigrams."""

    embedding_model = "hash-emb"
    dims = 256

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dims
            tokens = tokenize(text)
            grams = [t[i : i + 3] for t in tokens for i in range(max(1, len(t) - 2))]
            for feature in [*tokens, *grams]:
                h = int(hashlib.md5(feature.encode(), usedforsecurity=False).hexdigest(), 16)
                vec[h % self.dims] += 1.0
            out.append(vec)
        return out


def relevant(hit_id: str, wanted: Sequence[str]) -> bool:
    return any(hit_id == w or hit_id.startswith(w + ".") for w in wanted)


def evaluate(retriever: Retriever, k: int, *, use_async: bool = False) -> tuple[float, float]:
    found = 0
    rr = 0.0
    for item in DATA["queries"]:
        if use_async:
            hits = asyncio.run(retriever.asearch(item["q"], k))
        else:
            hits = retriever.search(item["q"], k)
        ranks = [i for i, h in enumerate(hits) if relevant(h.id, item["relevant"])]
        if ranks:
            found += 1
            rr += 1 / (ranks[0] + 1)
    n = len(DATA["queries"])
    return found / n, rr / n


@pytest.fixture
def docs(tmp_path: Path) -> list[Document]:
    return corpus(tmp_path)


def test_bm25_recall_at_k(docs: list[Document]) -> None:
    retriever = Retriever([lambda: docs])
    recall5, mrr = evaluate(retriever, 5)
    recall1, _ = evaluate(retriever, 1)
    assert recall5 >= 0.95, recall5
    assert recall1 >= 0.85, recall1
    assert mrr >= 0.85, mrr


def test_query_expansion_helps(docs: list[Document]) -> None:
    plain = Retriever([lambda: docs], settings=RetrievalSettings(query_expansion=False))
    expanded = Retriever([lambda: docs])
    # Expansion (s3 -> aws, k8s -> kubernetes, bq -> bigquery ...) lifts the right hit up.
    assert evaluate(expanded, 5)[1] > evaluate(plain, 5)[1]


def test_hybrid_rrf_recall_at_k(docs: list[Document], tmp_path: Path) -> None:
    retriever = Retriever(
        [lambda: docs], embedder=HashEmbedder(), cache_path=tmp_path / "emb.sqlite3"
    )
    assert retriever.hybrid
    recall5, mrr = evaluate(retriever, 5, use_async=True)
    assert recall5 >= 0.9, recall5
    assert mrr >= 0.75, mrr


def test_reranked_recall_at_k(docs: list[Document]) -> None:
    class OverlapReranker:
        """Scores by query-token coverage of the title (a cheap cross-encoder stand-in)."""

        name = "overlap"

        async def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
            q = set(tokenize(query))
            out = []
            for text in texts:
                title = set(tokenize(text.split("\n", 1)[0]))
                body = set(tokenize(text))
                out.append(len(q & title) * 2 + len(q & body) / math.sqrt(len(body) + 1))
            return out

    retriever = Retriever([lambda: docs], reranker=OverlapReranker())
    recall5, _ = evaluate(retriever, 5, use_async=True)
    assert recall5 >= 0.85, recall5
