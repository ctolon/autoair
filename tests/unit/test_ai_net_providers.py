"""Regression tests: SSRF pinning, bounded model caches, proxy cleanup, request-bound
signed identities and lazy connection model variants."""

from __future__ import annotations

import asyncio
import gc
import socket
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from autoair.ai import net
from autoair.ai.access import ReplayCache, sign_identity, verify_identity
from autoair.ai.llm import LLM, MAX_MODEL_VARIANTS, LLMError, ModelConfig, llm_from_config
from autoair.ai.user_llm import UserLlmCache


def _infos(*ips: str) -> list[Any]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


# --------------------------------------------------------------------------- SSRF pinning


def test_pinned_transport_connects_to_the_validated_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(host: str, port: int | None) -> list[Any]:
        return _infos("93.184.216.34")

    monkeypatch.setattr(net, "_agetaddrinfo", resolve)
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, text="ok")

    async def go() -> None:
        transport = net.PinnedAsyncTransport(httpx2.MockTransport(handler))
        async with httpx2.AsyncClient(transport=transport) as client:
            resp = await client.get("https://llm.example.com:8443/v1/x?q=1")
        assert resp.text == "ok"

    asyncio.run(go())
    (req,) = seen
    assert req.url.host == "93.184.216.34"
    assert req.url.port == 8443
    assert req.url.raw_path == b"/v1/x?q=1"
    assert req.headers["host"] == "llm.example.com:8443"
    assert req.extensions["sni_hostname"] == "llm.example.com"


def test_pinned_transport_blocks_dns_rebinding(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check sees a public address, the connection-time resolution a private one."""

    async def public(host: str) -> list[str]:
        return ["93.184.216.34"]

    async def rebound(host: str, port: int | None) -> list[Any]:
        return _infos("127.0.0.1")

    monkeypatch.setattr(net, "resolve", public)
    monkeypatch.setattr(net, "_agetaddrinfo", rebound)
    called: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        called.append(request)
        return httpx2.Response(200)

    async def go() -> None:
        await net.check_url("https://rebind.example.com/")  # passes (first answer)
        transport = net.PinnedAsyncTransport(httpx2.MockTransport(handler))
        async with httpx2.AsyncClient(transport=transport) as client:
            await client.get("https://rebind.example.com/")

    with pytest.raises(net.BlockedAddress, match="non-public"):
        asyncio.run(go())
    assert called == []


def test_sync_pinned_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(net, "_getaddrinfo", lambda host, port: _infos("10.0.0.7"))
    transport = net.PinnedTransport(httpx2.MockTransport(lambda r: httpx2.Response(200)))
    with httpx2.Client(transport=transport) as client, pytest.raises(net.BlockedAddress):
        client.get("http://internal.example.com/")


def test_user_llm_endpoint_is_revalidated_on_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [_infos("93.184.216.34")]

    async def resolve(host: str, port: int | None) -> list[Any]:
        return answers.pop(0) if answers else _infos("169.254.169.254")

    monkeypatch.setattr(net, "_agetaddrinfo", resolve)
    hosts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        hosts.append(request.url.host)
        return httpx2.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 1,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    config = ModelConfig(
        provider="openai",
        model="m",
        api_key=SecretStr("sk-test-123456789"),
        base_url="https://llm.example.com/v1",
        max_retries=0,
        pin_public_ip=True,
    )
    llm = LLM(config, transport=httpx2.MockTransport(handler))

    async def go() -> None:
        assert await llm.acomplete_text("s", "hi") == "hi"
        with pytest.raises(LLMError, match="endpoint refused"):
            await llm.acomplete_text("s", "hi")

    asyncio.run(go())
    assert hosts == ["93.184.216.34"]  # the second call never connected


def test_online_fetcher_pins_connections(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from autoair.ai.config import ResearchSettings
    from autoair.ai.research.online import OnlineFetcher

    async def public(host: str) -> list[str]:
        return ["151.101.0.223"]

    async def rebound(host: str, port: int | None) -> list[Any]:
        return _infos("10.1.2.3")

    monkeypatch.setattr(net, "resolve", public)
    monkeypatch.setattr(net, "_agetaddrinfo", rebound)
    fetcher = OnlineFetcher(
        ResearchSettings(online=True, allowed_domains=["pypi.org"]),
        tmp_path,
        transport=httpx2.MockTransport(lambda r: httpx2.Response(200, text="secret")),
    )
    with pytest.raises(net.BlockedAddress):
        asyncio.run(fetcher.fetch("https://pypi.org/simple/"))


# --------------------------------------------------------------------------- bounded caches


def test_model_variants_are_an_lru() -> None:
    config = ModelConfig(provider="openai", model="base", api_key=SecretStr("sk-x-12345678"))
    llm = llm_from_config(config)
    first = llm.with_model("m-0")
    for i in range(1, MAX_MODEL_VARIANTS + 5):
        llm.with_model(f"m-{i}")
        llm.with_model("m-0")  # kept hot
    variants = llm._shared.variants
    assert len(variants) == MAX_MODEL_VARIANTS
    assert llm.with_model("m-0") is first
    assert "m-1" not in variants
    assert first.model == "m-0"


class _Closable:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


def test_user_llm_cache_closes_evicted_clients() -> None:
    async def go() -> None:
        cache = UserLlmCache(max_entries=2)
        a, b, c, a2 = _Closable(), _Closable(), _Closable(), _Closable()
        await cache.put(("alice", "v1"), a)
        await cache.put(("bob", "v1"), b)
        assert cache.get(("alice", "v1")) is a  # alice is now most recent
        await cache.put(("carol", "v1"), c)  # evicts bob
        assert b.closed == 1
        assert a.closed == 0
        assert cache.get(("bob", "v1")) is None
        await cache.put(("alice", "v2"), a2)  # alice's old version is closed
        assert a.closed == 1
        assert cache.get(("alice", "v2")) is a2
        assert len(cache) == 2
        await cache.aclose()
        assert a2.closed == 1
        assert c.closed == 1
        assert len(cache) == 0

    asyncio.run(go())


def test_models_are_keyed_by_live_loops() -> None:
    config = ModelConfig(provider="openai", model="m", api_key=SecretStr("sk-x-12345678"))
    llm = LLM(config, transport=httpx2.MockTransport(lambda r: httpx2.Response(200)))

    async def touch() -> Any:
        return await llm.pydantic_model()

    first = asyncio.run(touch())
    second = asyncio.run(touch())
    assert first is not second  # HTTP clients are bound to their event loop
    gc.collect()
    assert len(llm._shared.models) <= 1  # the first loop is gone

    async def reuse() -> None:
        assert await llm.pydantic_model() is await llm.pydantic_model()
        await llm.aclose()
        assert len(llm._shared.models) == 0

    asyncio.run(reuse())


# --------------------------------------------------------------------------- proxy cleanup


class _FailingStream(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> Any:
        yield b"data: one\n\n"
        raise httpx2.ReadError("upstream died")

    async def aclose(self) -> None:
        self.closed = True


def test_proxy_closes_upstream_when_it_fails_mid_stream() -> None:
    from starlette.requests import Request

    from autoair.ai.config import AiSettings
    from autoair.ai.proxy import _Upstream

    stream = _FailingStream()
    services = SimpleNamespace(
        settings=SimpleNamespace(
            ai=AiSettings(service_url="http://ai.internal", service={"shared_secret": "s3cret"})
        ),
        ai_proxy_transport=httpx2.MockTransport(
            lambda r: httpx2.Response(200, stream=stream, headers={"content-type": "text/plain"})
        ),
    )
    upstream = _Upstream(services)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/ai/status",
            "query_string": b"",
            "headers": [],
            "client": ("1.2.3.4", 1),
        },
        receive,
    )

    async def go() -> None:
        response = await upstream.forward(request, "http://ai.internal/api/ai/status")
        chunks: list[Any] = []

        async def drain() -> None:
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                chunks.append(chunk)

        with pytest.raises(httpx2.ReadError):
            await drain()
        assert chunks == [b"data: one\n\n"]

    asyncio.run(go())
    assert stream.closed


# --------------------------------------------------------------------------- signed identity


def test_signed_identity_is_bound_to_the_request_and_single_use() -> None:
    secret = b"k"
    headers = sign_identity(
        secret, "alice", "admin", method="POST", path="/api/ai/ask", query="a=1", body=b"{}"
    )
    payload, signature = headers["x-autoair-ai-identity"], headers["x-autoair-ai-signature"]

    def verify(**kw: Any) -> Any:
        args: dict[str, Any] = {
            "method": "POST",
            "path": "/api/ai/ask",
            "query": "a=1",
            "body": b"{}",
            "replay": ReplayCache(),
        }
        args.update(kw)
        return verify_identity(secret, payload, signature, **args)

    assert verify(method="DELETE") is None
    assert verify(path="/api/ai/knowledge/doc") is None
    assert verify(query="a=2") is None
    assert verify(body=b'{"x":1}') is None
    assert verify(path="/ai/ask")["user"] == "alice"  # a stripped path prefix is fine
    replay = ReplayCache()
    assert verify(replay=replay)["role"] == "admin"
    assert verify(replay=replay) is None  # replayed
    assert verify_identity(b"other", payload, signature, method="POST", path="/api/ai/ask") is None


def test_replay_cache_expires_and_is_bounded() -> None:
    cache = ReplayCache(ttl=10, max_entries=3)
    assert cache.add("a", now=0)
    assert not cache.add("a", now=5)
    assert cache.add("a", now=11)  # expired
    for n in "bc":
        assert cache.add(n, now=12)
    assert not cache.add("d", now=12)
    assert not cache.add("a", now=12)  # capacity pressure must not make replay possible
    assert cache.add("d", now=23)
    assert len(cache._seen) <= 3


# --------------------------------------------------------------------------- connection LLM


def test_connection_llm_with_model_does_not_read_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[str | None] = []

    def read(self: LLM) -> ModelConfig:
        reads.append(self.conn_id)
        return ModelConfig(
            provider="openai", model=self._model_override or "base", api_key=SecretStr("sk-x-1")
        )

    monkeypatch.setattr(LLM, "_read_connection", read)
    llm = LLM(conn_id="llm_conn")
    variant = llm.with_model("other")
    assert reads == []  # no DB / secrets-backend access on the event loop
    assert variant.model == "other"
    assert variant.conn_id == "llm_conn"

    async def go() -> None:
        resolved = await variant.aresolve()
        assert resolved is not None
        assert resolved.model == "other"

    asyncio.run(go())
    assert reads == ["llm_conn"]
    assert llm.with_model("other") is variant  # cached


def test_user_llm_store_preview_does_not_write(tmp_path: Any) -> None:
    from cryptography.fernet import Fernet

    from autoair.ai.user_llm import UserLlmStore, UserLlmUpdate

    store = UserLlmStore(str(tmp_path / "users"), Fernet.generate_key())
    store.put("u", UserLlmUpdate(provider="openai", api_key=SecretStr("sk-abcdefgh1234")))
    preview = store.preview("u", UserLlmUpdate(provider="openai", model="gpt-x"))
    assert preview.model == "gpt-x"
    assert store.api_key(preview) == "sk-abcdefgh1234"  # stored key kept
    assert store.view("u").model is None  # nothing written
    cleared = store.preview("u", UserLlmUpdate(provider="openai", clear_api_key=True))
    assert cleared.api_key_enc is None
    assert store.view("u").api_key_set
