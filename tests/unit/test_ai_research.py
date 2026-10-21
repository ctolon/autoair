"""Agentic research: air-gapped (installed packages, provider metadata, source access with
path confinement) and online (allow-list, redirects, size limits, cache, rate limits)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest

from autoair.ai.config import AiSettings, ResearchSettings
from autoair.ai.net import UrlNotAllowed
from autoair.ai.research.local import (
    InstalledIndex,
    ResearchError,
    allowed_roots,
    installed_packages,
    module_file,
    package_info,
    read_source,
    top_level_modules,
)
from autoair.ai.research.online import OnlineFetcher, ResearchDisabled, docs_url, html_to_text
from autoair.ai.research.tools import research_tools
from autoair.settings import Settings

pytestmark = pytest.mark.airflow  # reads the installed Airflow providers


def services(tmp_path: Path, **ai: Any) -> Any:
    settings = Settings(dags_folder=tmp_path, subfolder="", ai=AiSettings(**ai))
    return SimpleNamespace(settings=settings, llm=None)


def test_installed_packages_and_provider_metadata() -> None:
    providers = installed_packages(providers_only=True)
    names = {p.name for p in providers}
    assert "apache-airflow-providers-standard" in names
    info = package_info("apache_airflow_providers_standard")
    assert info is not None
    assert info.is_provider
    assert info.provider is not None
    assert any(m.endswith("operators.bash") for m in info.provider["operators"])
    assert package_info("definitely-not-installed-xyz") is None
    assert "airflow" in top_level_modules()
    assert "httpx2" in {p.name for p in installed_packages("httpx2")}


def test_read_source_is_confined_to_allowed_roots(tmp_path: Path) -> None:
    roots = allowed_roots()
    bash = read_source("airflow.providers.standard.operators.bash.BashOperator", roots=roots)
    assert bash.symbol == "BashOperator"
    assert "class BashOperator" in bash.text
    method = read_source(
        "airflow.providers.standard.operators.bash", symbol="BashOperator.execute", roots=roots
    )
    assert method.text.lstrip().startswith(("def execute", "@"))
    assert "BashOperator" in bash.symbols
    secret = tmp_path / "secret.py"
    secret.write_text("TOKEN = 'x'\n")
    for target in ("/etc/passwd", "../../../../etc/passwd", str(secret), "os;rm -rf /"):
        with pytest.raises(ResearchError):
            read_source(target, roots=roots)
    # an explicitly allowed folder is readable, a symlink out of it is not
    extra = tmp_path / "include"
    extra.mkdir()
    (extra / "my_ops.py").write_text('class MyOperator:\n    """Custom."""\n')
    (extra / "escape.py").symlink_to(secret)
    custom = allowed_roots([extra])
    assert "Custom" in read_source(str(extra / "my_ops.py"), roots=custom).text
    with pytest.raises(ResearchError):
        read_source(str(extra / "escape.py"), roots=custom)
    with pytest.raises(ResearchError, match="not found"):
        read_source("airflow.providers.standard.operators.bash", symbol="Nope", roots=roots)
    assert module_file("no.such.module", roots) is None


def test_installed_index_documents() -> None:
    index = InstalledIndex(["apache-airflow-providers-standard"], max_modules=200)
    docs = index.documents()
    assert any(d.kind == "package" for d in docs)
    assert any(d.meta.get("example") for d in docs)  # example_dags are indexed
    assert any("BashOperator" in d.title for d in docs if d.kind == "source")
    assert index.documents() is docs  # cached until the installed set changes


def test_research_tools_air_gapped(tmp_path: Path) -> None:
    svc = services(
        tmp_path, research=ResearchSettings(package_patterns=["apache-airflow-providers-standard"])
    )
    tools = {t.name: t for t in research_tools(svc)}
    assert "fetch_doc" not in tools  # offline by default
    hits = asyncio.run(
        tools["search_docs"].handler(
            tools["search_docs"].input_model(query="bash command operator", scopes=["installed"])
        )
    )
    assert hits
    assert all(h["kind"] in ("source", "package") for h in hits)
    info = asyncio.run(
        tools["get_package_info"].handler(
            tools["get_package_info"].input_model(
                name="apache-airflow-providers-standard", online=True
            )
        )
    )
    assert info["installed"]["is_provider"] is True
    assert "disabled" in info["pypi"]
    listing = tools["list_installed_packages"].handler(
        tools["list_installed_packages"].input_model(pattern="standard", providers_only=True)
    )
    assert listing[0]["name"] == "apache-airflow-providers-standard"


# --------------------------------------------------------------------------- online


PAGE = (
    "<html><head><title>S3 operators</title><script>evil()</script></head><body>"
    "<nav>menu</nav><h1>Amazon S3</h1><p>Use S3KeySensor with deferrable=True.</p></body></html>"
)


def fetcher(tmp_path: Path, handler: Any, **kw: Any) -> OnlineFetcher:
    settings = ResearchSettings(online=True, **kw)
    return OnlineFetcher(
        settings,
        tmp_path / "cache",
        transport=httpx2.MockTransport(handler),
        allow_private=True,  # no DNS in tests; the allow-list still applies
    )


def test_online_fetch_allowlist_cache_and_html(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        assert request.headers["user-agent"].startswith("AutoAir-Research")
        return httpx2.Response(200, text=PAGE, headers={"content-type": "text/html"})

    f = fetcher(tmp_path, handler)
    url = docs_url("apache-airflow-providers-amazon/stable/operators/s3/s3.html")
    first = asyncio.run(f.fetch(url))
    assert first.title == "S3 operators"
    assert "deferrable=True" in first.text
    assert "evil()" not in first.text
    assert "menu" not in first.text
    again = asyncio.run(f.fetch(url))
    assert again.cached
    assert len(calls) == 1
    with pytest.raises(UrlNotAllowed, match="allow-list"):
        asyncio.run(f.fetch("https://evil.example.com/x"))
    with pytest.raises(ResearchDisabled):
        asyncio.run(OnlineFetcher(ResearchSettings(online=False), None).fetch("https://pypi.org/x"))
    title, text = html_to_text("<p>a</p><p>b</p>")
    assert text == "a\n\nb"
    assert title == ""


def test_online_redirects_are_rechecked_and_size_limited(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/hop":
            return httpx2.Response(302, headers={"location": "https://169.254.169.254/latest"})
        if request.url.path == "/big":
            return httpx2.Response(200, content=b"x" * 5000, headers={"content-type": "text/plain"})
        return httpx2.Response(302, headers={"location": "/hop"})

    f = fetcher(tmp_path, handler, max_bytes=1024)
    with pytest.raises(UrlNotAllowed, match="allow-list"):
        asyncio.run(f.fetch("https://pypi.org/start"))
    big = asyncio.run(f.fetch("https://pypi.org/big"))
    assert big.truncated
    assert len(big.text) == 1024
    loop = fetcher(
        tmp_path, lambda r: httpx2.Response(302, headers={"location": "/again"}), max_redirects=2
    )
    with pytest.raises(UrlNotAllowed, match="too many redirects"):
        asyncio.run(loop.fetch("https://pypi.org/again"))


def test_pypi_and_rate_limit(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "info": {"name": "apache-airflow", "version": "3.9.0", "summary": "s"},
                "releases": {"3.8.0": [], "3.9.0": []},
            },
            headers={"content-type": "application/json"},
        )

    f = fetcher(tmp_path, handler, rate_limit_per_minute=1, timeout_seconds=0.5)
    meta = asyncio.run(f.pypi("apache-airflow"))
    assert meta["latest_version"] == "3.9.0"
    assert meta["recent_releases"][-1] == "3.9.0"
    with pytest.raises(ResearchDisabled, match="rate limit"):
        asyncio.run(f.fetch("https://pypi.org/pypi/other/json"))


def test_fetch_doc_tool_only_when_online(tmp_path: Path) -> None:
    svc = services(tmp_path, research=ResearchSettings(online=True, index_installed=False))
    tools = {t.name: t for t in research_tools(svc)}
    assert "fetch_doc" in tools
    from autoair.ai.research.tools import research_for

    ctx = research_for(svc)
    ctx.fetcher = fetcher(
        tmp_path,
        lambda r: httpx2.Response(200, text=PAGE, headers={"content-type": "text/html"}),
    )
    page = asyncio.run(
        tools["fetch_doc"].handler(
            tools["fetch_doc"].input_model(url="apache-airflow/stable/x.html")
        )
    )
    assert page["title"] == "S3 operators"
    hits = asyncio.run(
        tools["search_docs"].handler(
            tools["search_docs"].input_model(query="S3KeySensor deferrable", scopes=["web"])
        )
    )
    assert hits
    assert hits[0]["kind"] == "web"
