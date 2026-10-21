"""Retrieval ranking, embedding cache/hybrid fusion and the memory store."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from autoair.ai.memory import MemoryError, MemoryStore
from autoair.ai.rag import (
    BM25Index,
    Document,
    EmbeddingCache,
    Retriever,
    doc_documents,
    markdown_documents,
    tokenize,
)

DOCS = [
    Document(
        id="operator:s3",
        kind="operator",
        title="S3ToRedshiftOperator (airflow.providers.amazon.S3ToRedshiftOperator)",
        text="Copy data from an S3 bucket into a Redshift table with COPY.",
    ),
    Document(
        id="operator:bash",
        kind="operator",
        title="BashOperator",
        text="Run a bash command or script.",
    ),
    Document(
        id="operator:sensor",
        kind="operator",
        title="S3KeySensor",
        text="Wait for a key (file) to appear in an S3 bucket.",
    ),
    Document(
        id="doc:retries",
        kind="doc",
        title="Retries and timeouts",
        text="Default to two retries with a delay; always set execution timeouts.",
    ),
]


def test_tokenize_splits_identifiers() -> None:
    assert tokenize("S3ToRedshiftOperator") == ["s3", "redshift", "operator"]
    assert tokenize("execution_timeout_seconds") == ["execution", "timeout", "second"]
    assert "the" not in tokenize("the bucket")


def test_bm25_ranks_relevant_documents_first() -> None:
    index = BM25Index(DOCS)
    top = [DOCS[i].id for i, _ in index.search("load files from s3 into redshift", 3)]
    assert top[0] == "operator:s3"
    wait = [DOCS[i].id for i, _ in index.search("wait until a file lands in s3", 2)]
    assert wait[0] == "operator:sensor"
    docs_only = index.search("timeouts", 5, kinds=["doc"])
    assert [DOCS[i].id for i, _ in docs_only] == ["doc:retries"]
    assert index.search("zzzz-unknown", 5) == []


class FakeEmbedder:
    """Bag-of-letters embeddings: deterministic and good enough to rank."""

    embedding_model = "fake-emb"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [
            [float(t.lower().count(c)) for c in "abcdefghijklmnopqrstuvwxyz0123456789"]
            for t in texts
        ]


def test_hybrid_retriever_caches_embeddings_on_disk(tmp_path: Path) -> None:
    cache = tmp_path / "emb.sqlite3"
    embedder = FakeEmbedder()
    retriever = Retriever([lambda: list(DOCS)], embedder=embedder, cache_path=cache)
    assert retriever.hybrid
    hits = retriever.search("redshift copy", 2)
    assert hits[0].id == "operator:s3"
    assert hits[0].uri is None
    assert embedder.calls == 2  # one batch for the corpus, one for the query
    # A new retriever reuses the disk cache: only the query is embedded.
    embedder2 = FakeEmbedder()
    again = Retriever([lambda: list(DOCS)], embedder=embedder2, cache_path=cache)
    again.search("bash", 1)
    assert embedder2.calls == 1
    stored = EmbeddingCache(cache).get_many("fake-emb", [EmbeddingCache.key("x")])
    assert stored == {}


def test_retriever_survives_failing_sources_and_embedder() -> None:
    class Broken(FakeEmbedder):
        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            raise RuntimeError("embedding service down")

    def bad_source() -> list[Document]:
        raise RuntimeError("catalog unavailable")

    retriever = Retriever([bad_source, lambda: list(DOCS)], embedder=Broken())
    assert retriever.search("bash command", 1)[0].id == "operator:bash"
    context = retriever.context("retries", k=2, max_chars=500)
    assert "Retries and timeouts" in context
    assert len(context) <= 600
    assert retriever.search("   ") == []


def test_markdown_chunking_and_shipped_knowledge(tmp_path: Path) -> None:
    md = tmp_path / "guide.md"
    md.write_text("# Guide\nIntro text.\n\n## First\nAlpha.\n\n## Second\nBeta.\n")
    docs = markdown_documents(md)
    assert [d.title for d in docs] == ["Guide", "Guide - First", "Guide - Second"]
    assert docs[1].uri == "autoair://docs/guide"
    shipped = doc_documents()
    assert any("Airflow 3" in d.title for d in shipped)


# --------------------------------------------------------------------------- memory


def test_memory_store_scopes_dedupe_and_secrets(tmp_path: Path) -> None:
    store = MemoryStore(str(tmp_path / "mem"))
    g = store.add("Owner is always data-eng", scope="global", created_by="alice")
    assert store.add("Owner is always data-eng", scope="global") == g
    store.add("Use snowflake_prod for loads", scope="team", owner="analytics")
    store.add("I prefer deferrable sensors", scope="user", owner="bob", kind="preference")
    store.add("Team of others", scope="team", owner="other")

    visible, total = store.list(visible_to=("analytics", "bob"))
    assert total == 3
    assert {m.text for m in visible} == {
        "Owner is always data-eng",
        "Use snowflake_prod for loads",
        "I prefer deferrable sensors",
    }
    found, _ = store.list(query="snowflake connection")
    assert [m.text for m in found] == ["Use snowflake_prod for loads"]
    page, total = store.list(limit=1, offset=1)
    assert len(page) == 1
    assert total == 4

    conventions = store.conventions(team="analytics", user="bob")
    assert conventions[0] == "[global] Owner is always data-eng"
    assert conventions[-1] == "[user:bob] I prefer deferrable sensors"

    with pytest.raises(MemoryError, match="secret"):
        store.add("the api key is AKIA" + "ABCDEFGHIJKLMNOP")
    with pytest.raises(MemoryError, match="owner"):
        store.add("x y z", scope="team")
    with pytest.raises(MemoryError, match="empty"):
        store.add("   ")

    assert store.delete(g.id) is True
    assert store.delete(g.id) is False
    reopened = MemoryStore(str(tmp_path / "mem"))
    assert len(reopened.list()[0]) == 3
