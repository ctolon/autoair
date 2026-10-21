"""Signed identities stay single-use across independently constructed service replicas."""

from concurrent.futures import ThreadPoolExecutor

from autoair.ai.access import SharedReplayCache
from autoair.persistence import document_store
from autoair.settings import Settings


def test_replicas_atomically_consume_nonce_and_expire_it(tmp_path):
    settings = Settings(database={"backend": "sqlite", "url": f"sqlite:///{tmp_path}/state.db"})
    a, b = [SharedReplayCache(document_store(settings, "nonces", file_url="")) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda cache: cache.add("same", now=10), [a, b]))
    assert sorted(results) == [False, True]
    assert not b.add("same", now=11)
    assert a.add("same", now=10 + a.ttl + 1)


def test_full_bucket_keeps_live_nonces(tmp_path):
    from autoair.persistence.documents import FileDocumentStore

    cache = SharedReplayCache(FileDocumentStore(str(tmp_path)), max_entries=256)
    import hashlib

    nonce = "first"
    bucket = hashlib.sha256(nonce.encode()).hexdigest()[:2]
    collision = next(
        str(i)
        for i in range(10000)
        if hashlib.sha256(str(i).encode()).hexdigest().startswith(bucket)
    )
    assert cache.add(nonce, now=10)
    assert not cache.add(collision, now=11)
    assert not cache.add(nonce, now=12)
    assert cache.add(collision, now=10 + cache.ttl + 1)


def test_concurrent_chat_writes_do_not_overwrite_each_other(tmp_path):
    import pytest

    from autoair.ai.assistants.threads import ChatThread, ThreadStore
    from autoair.persistence.documents import DocumentConflict

    settings = Settings(database={"backend": "sqlite", "url": f"sqlite:///{tmp_path}/chat.db"})
    stores = [ThreadStore(document_store(settings, "chat", file_url="")) for _ in range(2)]
    thread = stores[0].save(ChatThread(user="alice", mode="developer"))
    first, second = [store.get("alice", thread.id) for store in stores]
    first.title = "first change"
    stores[0].save(first)
    second.title = "stale change"
    with pytest.raises(DocumentConflict):
        stores[1].save(second)
    assert stores[1].get("alice", thread.id).title == "first change"
