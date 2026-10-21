"""Online research (``ai.research.online = true``): fetch documentation from an
allow-list of domains with SSRF checks on every hop, per-domain rate limits, a body
size limit, an optional proxy and an on-disk TTL cache.

Default domains: ``airflow.apache.org`` (docs), ``pypi.org`` (package JSON) and
``raw.githubusercontent.com`` (provider sources / docs).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx2
from pydantic import BaseModel

from autoair.ai.config import ResearchSettings
from autoair.ai.net import PinnedAsyncTransport, UrlNotAllowed, check_url, host_matches
from autoair.net import tls


class ResearchDisabled(PermissionError):
    pass


class FetchResult(BaseModel):
    url: str
    final_url: str
    status: int
    content_type: str = ""
    title: str = ""
    text: str
    truncated: bool = False
    cached: bool = False


class _TextExtractor(HTMLParser):
    """Readable text of an HTML page (drops scripts, styles, navigation chrome)."""

    SKIP = frozenset({"script", "style", "nav", "header", "footer", "noscript", "svg", "form"})
    BLOCK = frozenset(
        {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "pre", "section", "dt", "dd", "br"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(raw)
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return html.unescape(parser.title.strip()), text


class _Bucket:
    def __init__(self, per_minute: int) -> None:
        self.capacity = float(per_minute)
        self.tokens = float(per_minute)
        self.rate = per_minute / 60.0
        self.updated = time.monotonic()

    def take(self) -> float:
        """Seconds to wait before the request may go (0 = now)."""
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate


class OnlineFetcher:
    def __init__(
        self,
        settings: ResearchSettings,
        cache_dir: Path | None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        allow_private: bool = False,
    ) -> None:
        self.settings = settings
        self.cache_dir = cache_dir
        self._transport = transport
        self._allow_private = allow_private
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.settings.online

    def _cache_file(self, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()}.json"

    def _cached(self, url: str) -> FetchResult | None:
        path = self._cache_file(url)
        if path is None or not path.is_file():
            return None
        if time.time() - path.stat().st_mtime > self.settings.cache_ttl_seconds:
            return None
        try:
            result = FetchResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        result.cached = True
        return result

    def _store(self, result: FetchResult) -> None:
        path = self._cache_file(result.url)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(result.model_dump_json(), encoding="utf-8")
        except OSError:
            pass

    async def _wait_turn(self, host: str) -> None:
        async with self._lock:
            bucket = self._buckets.setdefault(
                host.lower(), _Bucket(self.settings.rate_limit_per_minute)
            )
            delay = bucket.take()
        if delay > 0:
            if delay > self.settings.timeout_seconds:
                raise ResearchDisabled(f"rate limit for {host} reached; try again later")
            await asyncio.sleep(delay)

    async def fetch(self, url: str) -> FetchResult:
        if not self.settings.online:
            raise ResearchDisabled(
                "online research is disabled (air-gapped); set AUTOAIR_AI__RESEARCH__ONLINE=true"
            )
        url = url.strip()
        cached = self._cached(url)
        if cached is not None:
            return cached
        domains = self.settings.allowed_domains
        current = url
        client_args: dict[str, Any] = {
            "timeout": self.settings.timeout_seconds,
            "follow_redirects": False,
            "headers": {"user-agent": self.settings.user_agent, "accept": "*/*"},
            "transport": self._transport,
        }
        # CA bundle / client certificate of AUTOAIR_OUTBOUND__* (autoair.net.tls)
        client_args["verify"] = tls.ssl_context("research")
        proxy = self.settings.proxy or tls.proxy_for("research", url)
        if proxy:
            # The admin's forward proxy resolves the host itself (no pinning possible).
            client_args["proxy"] = proxy
        elif not self._allow_private:
            # check_url() below is advisory against DNS rebinding: the transport resolves
            # again, validates and connects to exactly the checked IP.
            client_args["transport"] = PinnedAsyncTransport(self._transport)
        async with httpx2.AsyncClient(**client_args) as client:
            for _ in range(self.settings.max_redirects + 1):
                await check_url(current, domains=domains, allow_private=self._allow_private)
                await self._wait_turn(urlsplit(current).hostname or "")
                async with client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise UrlNotAllowed("redirect without a location")
                        current = urljoin(current, location)
                        continue
                    body = bytearray()
                    truncated = False
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.settings.max_bytes:
                            truncated = True
                            del body[self.settings.max_bytes :]
                            break
                    content_type = resp.headers.get("content-type", "")
                    result = self._decode(url, current, resp.status_code, content_type, bytes(body))
                    result.truncated = truncated
                    if resp.status_code < 400:
                        self._store(result)
                    return result
        raise UrlNotAllowed(f"too many redirects (> {self.settings.max_redirects})")

    def _decode(
        self, url: str, final: str, status: int, content_type: str, body: bytes
    ) -> FetchResult:
        text = body.decode("utf-8", errors="replace")
        title = ""
        if "html" in content_type:
            title, text = html_to_text(text)
        return FetchResult(
            url=url,
            final_url=final,
            status=status,
            content_type=content_type.split(";")[0],
            title=title,
            text=text,
        )

    async def pypi(self, package: str) -> dict[str, Any]:
        """Latest release metadata from PyPI's JSON API (compact)."""
        name = re.sub(r"[^A-Za-z0-9._-]", "", package)
        result = await self.fetch(f"https://pypi.org/pypi/{name}/json")
        if result.status >= 400:
            raise LookupError(f"PyPI has no package {package!r} (HTTP {result.status})")
        data = json.loads(result.text)
        info = data.get("info") or {}
        releases = sorted((data.get("releases") or {}).keys())
        return {
            "name": info.get("name"),
            "latest_version": info.get("version"),
            "summary": info.get("summary"),
            "requires_python": info.get("requires_python"),
            "project_urls": info.get("project_urls"),
            "requires_dist": (info.get("requires_dist") or [])[:60],
            "recent_releases": releases[-10:],
            "cached": result.cached,
        }

    def allowed(self, url: str) -> bool:
        host = urlsplit(url).hostname or ""
        return host_matches(host, self.settings.allowed_domains)


def docs_url(path_or_url: str) -> str:
    """Accept full URLs or paths relative to https://airflow.apache.org/docs/."""
    value = path_or_url.strip()
    if value.startswith(("http://", "https://")):
        return value
    return "https://airflow.apache.org/docs/" + value.lstrip("/")


__all__ = [
    "FetchResult",
    "OnlineFetcher",
    "ResearchDisabled",
    "docs_url",
    "html_to_text",
]
