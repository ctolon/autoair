"""Retrieval mechanics: hybrid fusion, metadata filters, incremental indexing, the query
cache, rerankers (incl. hosted rerank APIs over httpx2), chunking and query expansion."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import httpx2

from autoair.ai.chunking import chunk_markdown, chunk_python, chunk_text, docstring_summary
from autoair.ai.config import RetrievalSettings
from autoair.ai.query import expand_query, rewrite_queries
from autoair.ai.rag import Document, Retriever
from autoair.ai.rerank import HttpReranker, LLMReranker, build_reranker
from autoair.ai.testing import FakeLLM


def docs() -> list[Document]:
    return [
        Document(
            id="a",
            kind="operator",
            title="S3KeySensor",
            text="wait for s3 key",
            meta={"provider": "amazon"},
        ),
        Document(
            id="b",
            kind="operator",
            title="GCSObjectExistenceSensor",
            text="wait for gcs object",
            meta={"provider": "google"},
        ),
        Document(
            id="c",
            kind="doc",
            title="Sensors",
            text="use deferrable sensors to wait",
            meta={"provider": "core"},
        ),
    ]


class CountingEmbedder:
    embedding_model = "count"

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[float(len(t)), float(t.count("s")), 1.0] for t in texts]

    async def aembed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed(texts)


def test_filters_and_kinds() -> None:
    r = Retriever([docs])
    assert [h.id for h in r.search("wait sensor", 5, filters={"provider": "google"})] == ["b"]
    either = {h.id for h in r.search("wait", 5, filters={"provider": ["amazon", "core"]})}
    assert either == {"a", "c"}
    assert {h.id for h in r.search("wait", 5, kinds=["doc"])} == {"c"}
    assert asyncio.run(r.asearch("wait", 5, filters={"kind": "doc"}))[0].id == "c"


def test_incremental_embedding_and_rebuild_only_on_change() -> None:
    corpus = docs()
    embedder = CountingEmbedder()
    r = Retriever([lambda: list(corpus)], embedder=embedder, ttl_seconds=0)
    r.search("wait", 3)
    first = len(embedder.texts)
    assert r.rebuilds == 1
    r.search("wait again", 3)
    assert r.rebuilds == 1  # sources re-read (ttl 0) but unchanged: no rebuild
    corpus.append(Document(id="d", kind="doc", title="New", text="brand new doc"))
    r.search("new", 3)
    assert r.rebuilds == 2
    corpus_texts = [t for t in embedder.texts[first:] if "\n" in t]
    assert corpus_texts == ["New\nbrand new doc"]  # only the new chunk was embedded


def test_query_cache_and_invalidation() -> None:
    calls = {"n": 0}

    def source() -> list[Document]:
        calls["n"] += 1
        return docs()

    r = Retriever([source], settings=RetrievalSettings(index_ttl_seconds=60))
    one = r.search("wait", 2)
    two = r.search("wait", 2)
    assert [h.id for h in one] == [h.id for h in two]
    assert calls["n"] == 1
    r.invalidate()
    r.search("wait", 2)
    assert calls["n"] == 2


def test_variants_are_fused_and_reranker_reorders() -> None:
    class Reverse:
        name = "reverse"

        async def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
            return [float(i) for i in range(len(texts))]  # last candidate wins

    r = Retriever([docs], reranker=Reverse())
    plain = asyncio.run(r.asearch("wait", 3, rerank=False))
    reranked = asyncio.run(r.asearch("wait", 3))
    assert [h.id for h in reranked] == [h.id for h in reversed(plain)]
    fused = asyncio.run(r.asearch("s3 key", 3, variants=["gcs object"], rerank=False))
    assert {h.id for h in fused[:2]} == {"a", "b"}

    class Broken:
        name = "broken"

        async def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
            raise RuntimeError("down")

    fallback = Retriever([docs], reranker=Broken())
    assert [h.id for h in asyncio.run(fallback.asearch("wait", 3))] == [h.id for h in plain]


def test_http_and_llm_rerankers() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer k"
        return httpx2.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                ]
            },
        )

    cohere = HttpReranker("cohere", api_key="k", transport=httpx2.MockTransport(handler))
    assert asyncio.run(cohere.rerank("q", ["x", "y"])) == [0.1, 0.9]
    assert seen[0]["model"] == "rerank-v3.5"
    assert seen[0]["top_n"] == 2
    llm = FakeLLM([{"scores": [1, 9]}])
    assert asyncio.run(LLMReranker(llm).rerank("q", ["x", "y"])) == [1, 9]
    assert build_reranker(RetrievalSettings(reranker="cohere")) is None  # no key
    assert build_reranker(RetrievalSettings(reranker="llm"), llm) is not None
    assert build_reranker(RetrievalSettings(reranker="cross_encoder")) is None  # extra missing


def test_chunkers() -> None:
    long = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(10))
    pieces = chunk_text(long, size=500, overlap=50)
    assert len(pieces) > 3
    assert all(len(p) <= 560 for p in pieces)
    md = "# Title\nintro\n\n## A\nalpha\n```\n# not a heading\n```\n### A1\ndeep\n## B\nbeta"
    chunks = chunk_markdown(md)
    assert [c.title for c in chunks] == ["Title", "Title - A", "Title - A - A1", "Title - B"]
    assert "# not a heading" in chunks[1].text
    source = (
        '"""Mod doc."""\n\nclass Op(Base):\n    """Op doc."""\n\n    def run(self):\n'
        '        return 1\n\n\ndef helper(x):\n    """Help."""\n    return x\n'
    )
    code = chunk_python(source, "pkg.mod")
    assert [c.anchor for c in code] == ["pkg.mod", "pkg.mod.Op", "pkg.mod.helper"]
    summary = docstring_summary(source, "pkg.mod")
    assert any("class Op(Base)" in c.text for c in summary)
    assert chunk_python("def broken(:", "m")[0].text.startswith("def broken")


def test_query_expansion_and_llm_rewrite() -> None:
    assert "kubernetes" in expand_query("run on k8s")
    assert expand_query("nothing special here") == "nothing special here"
    llm = FakeLLM([{"queries": ["S3KeySensor deferrable", "wait s3"]}])
    assert asyncio.run(rewrite_queries(llm, "wait s3")) == [
        "wait s3",
        "S3KeySensor deferrable",
    ]
