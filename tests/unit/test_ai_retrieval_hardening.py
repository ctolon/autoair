"""Regression tests: chunking termination, lazy cross-encoder loading, per-user rerank,
bounded caches / event channel, per-services singletons and snapshot-consistent search."""

from __future__ import annotations

import asyncio
import dataclasses
import gc
import logging
import threading
import time
import weakref
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from autoair.ai import rerank as rerank_mod
from autoair.ai.chunking import chunk_text
from autoair.ai.config import RetrievalSettings
from autoair.ai.events import EventChannel, StatusEvent, TokenEvent
from autoair.ai.rag import Document, EmbeddingCache, Retriever, VectorIndex


class Embedder:
    embedding_model = "fake-emb"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(t.lower().count(c)) + 0.01 for c in "abcdefghijklmnop"] for t in texts]


def _docs(prefix: str, n: int) -> list[Document]:
    return [
        Document(id=f"{prefix}{i}", kind="doc", title=f"{prefix} title {i}", text=f"text {i}")
        for i in range(n)
    ]


# --------------------------------------------------------------------------- #6 chunking


def test_chunk_text_terminates_with_overlap_larger_than_size() -> None:
    text = " ".join(["word"] * 2000)
    result: list[list[str]] = []
    worker = threading.Thread(target=lambda: result.append(chunk_text(text, 200, 500)))
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "chunk_text did not terminate"
    assert result[0]
    assert all(len(c) <= 200 for c in result[0])
    # Pathological inputs: no whitespace, tiny windows.
    assert chunk_text("x" * 50, 1, 10)
    assert chunk_text("y" * 999, 3, 3)


def test_retrieval_settings_reject_overlap_of_half_a_chunk_or_more() -> None:
    with pytest.raises(ValidationError, match="chunk_overlap"):
        RetrievalSettings(chunk_size=200, chunk_overlap=500)
    with pytest.raises(ValidationError, match="chunk_overlap"):
        RetrievalSettings(chunk_size=400, chunk_overlap=200)
    assert RetrievalSettings(chunk_size=400, chunk_overlap=199).chunk_overlap == 199


# --------------------------------------------------------------------------- #8 reranker


def test_cross_encoder_loads_once_in_a_thread_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []
    loop_thread = threading.current_thread()

    def import_module(name: str) -> Any:
        calls.append(threading.current_thread().name)
        assert threading.current_thread() is not loop_thread
        raise OSError("model download failed")

    monkeypatch.setattr(rerank_mod.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(rerank_mod.importlib, "import_module", import_module)
    monkeypatch.setattr(rerank_mod, "_CROSS_ENCODERS", {})
    reranker = rerank_mod.build_reranker(RetrievalSettings(reranker="cross_encoder"))
    assert isinstance(reranker, rerank_mod.CrossEncoderReranker)
    assert calls == []  # building never loads the model

    async def run() -> list[list[float]]:
        return [await reranker.rerank("q", ["a", "b", "c"]) for _ in range(3)]

    with caplog.at_level(logging.WARNING, logger="autoair.ai.rerank"):
        scores = asyncio.run(run())
    assert scores == [[3.0, 2.0, 1.0]] * 3  # incoming order kept
    assert len(calls) == 1
    assert sum("unavailable" in r.getMessage() for r in caplog.records) == 1


def test_cross_encoder_model_is_shared_and_used(monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[str] = []

    class FakeCE:
        def __init__(self, model: str) -> None:
            loads.append(model)

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [float(len(t)) for _, t in pairs]

    monkeypatch.setattr(rerank_mod, "_CROSS_ENCODERS", {})
    monkeypatch.setattr(
        rerank_mod.importlib, "import_module", lambda name: SimpleNamespace(CrossEncoder=FakeCE)
    )

    async def run() -> list[float]:
        a, b = rerank_mod.CrossEncoderReranker("m"), rerank_mod.CrossEncoderReranker("m")
        await asyncio.gather(a.rerank("q", ["x"]), b.rerank("q", ["x"]))
        return await a.rerank("q", ["x", "xyz"])

    assert asyncio.run(run()) == [1.0, 3.0]
    assert loads == ["m"]


def test_llm_reranker_not_built_on_the_central_model_in_per_user_mode() -> None:
    settings = RetrievalSettings(reranker="llm")
    assert rerank_mod.build_reranker(settings, object(), llm_mode="central") is not None
    assert rerank_mod.build_reranker(settings, object(), llm_mode="per_user") is None


def test_research_uses_the_callers_llm_or_none_in_per_user_mode() -> None:
    from autoair.ai.config import AiSettings
    from autoair.ai.research.tools import _caller_llm

    central, mine = object(), object()

    def services(mode: str) -> Any:
        settings = SimpleNamespace(ai=AiSettings(llm_mode=mode))  # type: ignore[arg-type]
        return SimpleNamespace(settings=settings, llm=central)

    assert _caller_llm(services("per_user"), None) is None
    assert _caller_llm(services("per_user"), mine) is mine
    assert _caller_llm(services("central"), None) is central


def test_asearch_reranker_override_is_used() -> None:
    class Reverse:
        name = "reverse"

        async def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
            return [float(i) for i in range(len(texts))]

    retriever = Retriever([lambda: _docs("d", 3)])

    async def run() -> tuple[list[str], list[str]]:
        plain = [h.id for h in await retriever.asearch("title", 3)]
        over = [h.id for h in await retriever.asearch("title", 3, reranker=Reverse())]
        return plain, over

    plain, over = asyncio.run(run())
    assert over == list(reversed(plain))


def test_aconventions_for_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoair.ai import memory

    seen: list[threading.Thread] = []

    def fake(services: Any, *, user: str | None = None) -> list[str]:
        seen.append(threading.current_thread())
        return [f"rule for {user}"]

    monkeypatch.setattr(memory, "conventions_for", fake)

    async def run() -> list[str]:
        return await memory.aconventions_for(object(), user="ann")

    assert asyncio.run(run()) == ["rule for ann"]
    assert seen
    assert seen[0] is not threading.main_thread()


def test_rebuild_does_not_block_concurrent_searches() -> None:
    """While one thread embeds a changed corpus, searches keep using the old snapshot."""
    gate = threading.Event()
    entered = threading.Event()

    class Slow(Embedder):
        block = False

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            if self.block and len(texts) > 1:
                entered.set()
                gate.wait(5)
            return super().embed(texts)

    docs = {"items": _docs("old", 3)}
    embedder = Slow()
    retriever = Retriever([lambda: list(docs["items"])], embedder=embedder, ttl_seconds=0)
    assert retriever.search("old title", 1)
    embedder.block = True
    docs["items"] = _docs("new", 5)
    rebuild = threading.Thread(target=retriever.search, args=("new title", 1))
    rebuild.start()
    assert entered.wait(5)
    started = time.monotonic()
    stale = retriever.search("old title", 1)
    assert time.monotonic() - started < 2
    assert stale[0].id.startswith("old")
    gate.set()
    rebuild.join(5)
    assert retriever.search("new title", 1)[0].id.startswith("new")


# --------------------------------------------------------------------------- #9 bounded


def test_vector_index_forgets_vectors_that_left_the_corpus() -> None:
    index = VectorIndex(_docs("a", 4), Embedder(), None)
    assert len(index._known) == 4
    index.update(_docs("b", 2))
    assert len(index._known) == 2
    assert len(index.vectors) == 2


def test_embedding_cache_is_capped(tmp_path: Path) -> None:
    cache = EmbeddingCache(tmp_path / "e.sqlite3", max_entries=10)
    for batch in range(5):
        cache.put_many("m", {f"h{batch}-{i}": [1.0, 2.0] for i in range(4)})
    assert len(cache) == 10
    # The newest writes survive.
    assert set(cache.get_many("m", ["h4-0", "h4-3", "h0-0"])) == {"h4-0", "h4-3"}


def test_event_channel_is_bounded_and_coalesces_tokens() -> None:
    async def run() -> tuple[int, str, int, int]:
        channel = EventChannel(maxsize=10)
        for i in range(5000):
            channel.emit(TokenEvent(text=str(i % 10)))
            if i % 100 == 0:
                channel.emit(StatusEvent(stage="s"))
        pending = channel.pending()
        channel.close()
        text = ""
        statuses = 0
        async for event in channel:
            if isinstance(event, TokenEvent):
                text += event.text
            else:
                statuses += 1
        return pending, text, statuses, channel.dropped

    pending, text, statuses, dropped = asyncio.run(run())
    assert pending <= 21  # queue + overflow, not 5050
    # Every token arrives (merged); only surplus progress events are dropped.
    assert text == "".join(str(i % 10) for i in range(5000))
    assert statuses >= 1
    assert statuses + dropped == 50


def test_event_channel_applies_backpressure_to_worker_threads() -> None:
    async def run() -> tuple[int, int]:
        channel = EventChannel(maxsize=5)
        peak = 0

        def produce() -> None:
            for i in range(200):
                channel.emit(StatusEvent(stage=str(i)))
            channel.close()

        worker = asyncio.create_task(asyncio.to_thread(produce))
        got = 0
        async for _ in channel:
            peak = max(peak, channel.pending())
            got += 1
            await asyncio.sleep(0)
        await worker
        return got, peak

    got, peak = asyncio.run(run())
    assert got == 200
    assert peak <= 6


def test_event_channel_closed_consumer_does_not_block_workers() -> None:
    async def run() -> float:
        channel = EventChannel(maxsize=1, block_timeout=30)
        channel.emit(StatusEvent(stage="a"))
        iterator = channel.__aiter__()
        await iterator.__anext__()
        await iterator.aclose()
        started = time.monotonic()
        await asyncio.to_thread(lambda: [channel.emit(StatusEvent(stage="x")) for _ in range(5)])
        return time.monotonic() - started

    assert asyncio.run(run()) < 2


@dataclasses.dataclass
class _Services:  # like autoair.api.app.Services: an unhashable dataclass
    settings: Any
    catalog: Any = None
    llm: Any = None


def test_retriever_and_research_state_live_on_the_services_object(tmp_path: Path) -> None:
    from autoair.ai.corpus import retriever_for
    from autoair.ai.research.tools import research_for
    from autoair.settings import Settings

    services = _Services(settings=Settings(state_dir=tmp_path))
    first = retriever_for(services)
    assert retriever_for(services) is first
    ctx = research_for(services)
    assert research_for(services) is ctx
    ref = weakref.ref(services)
    del services, first, ctx
    gc.collect()
    assert ref() is None  # no module-level map keeps the services alive


# --------------------------------------------------------------------------- #11 snapshot


def test_asearch_reads_documents_and_vectors_from_one_snapshot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rebuild during the query-embedding await must not misalign docs and vectors."""
    docs = {"items": _docs("old", 2)}

    class Racing(Embedder):
        retriever: Retriever | None = None

        async def aembed(self, texts: Sequence[str]) -> list[list[float]]:
            assert self.retriever is not None
            docs["items"] = _docs("grown", 8)
            self.retriever.invalidate()
            await asyncio.to_thread(self.retriever.search, "grown", 1)  # rebuild now
            return self.embed(texts)

    embedder = Racing()
    retriever = Retriever([lambda: list(docs["items"])], embedder=embedder)
    embedder.retriever = retriever

    async def run() -> list[str]:
        return [h.id for h in await retriever.asearch("old title", 8, rerank=False)]

    with caplog.at_level(logging.WARNING, logger="autoair.ai.rag"):
        ids = asyncio.run(run())
    assert sorted(ids) == ["old0", "old1"]
    assert not [r for r in caplog.records if "embedding query failed" in r.getMessage()]
    assert retriever.search("grown title", 2)[0].id.startswith("grown")


# --------------------------------------------------------------------------- #14 web pages


def test_remember_page_replaces_chunks_of_an_earlier_fetch() -> None:
    from autoair.ai.research.tools import ResearchContext

    web: dict[str, Document] = {}
    ctx = ResearchContext.__new__(ResearchContext)
    ctx._web = web
    ctx._lock = threading.Lock()
    ctx.retriever = Retriever([lambda: list(web.values())])
    long_page = "\n\n".join(f"## Part {i}\n\nold body {i}" for i in range(6))
    ctx.remember_page("https://x/doc", "Doc", long_page)
    assert len(web) == 6
    ctx.remember_page("https://x/doc", "Doc", "## Only\n\nnew body")
    assert len(web) == 1
    assert "new body" in next(iter(web.values())).text
    assert not any("old body" in d.text for d in ctx.retriever.documents())
