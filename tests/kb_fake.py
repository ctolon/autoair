"""A fake airflow.apache.org / astronomer.io for knowledge-base tests (no network).

``FakeDocs`` serves Sphinx search indexes, HTML pages and llms.txt indexes through an
``httpx2.MockTransport`` with ETag / conditional request support, and records requests.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx2

from autoair.kb.components import installed_providers, installed_version
from autoair.kb.service import KnowledgeBase
from autoair.kb.stores.local import LocalStore
from autoair.persistence.documents import FileDocumentStore

DOCS = "https://airflow.apache.org/docs/"
ASTRO = "https://www.astronomer.io/docs/"


def sphinx_index(pages: dict[str, str]) -> str:
    """searchindex.js for docname -> title."""
    names = list(pages)
    return (
        "Search.setIndex("
        + json.dumps(
            {
                "docnames": names,
                "filenames": [f"{n}.rst" for n in names],
                "titles": [pages[n] for n in names],
            }
        )
        + ")"
    )


def sphinx_page(title: str, sections: Sequence[tuple[str, str, str]]) -> str:
    """HTML in the Sphinx theme layout: (id, heading, body html) per section."""
    body = "".join(
        f'<section id="{sid}"><h2>{head}<a class="headerlink" href="#{sid}">¶</a></h2>'
        f"{html}</section>"
        for sid, head, html in sections
    )
    return (
        f"<html><head><title>{title} — Airflow Documentation</title></head><body>"
        "<nav>Navigation junk</nav><script>var x = 1;</script>"
        f'<div class="body" role="main"><section id="top"><h1>{title}'
        f'<a class="headerlink" href="#top">¶</a></h1>{body}</section></div>'
        "<footer>Footer junk</footer></body></html>"
    )


class FakeDocs:
    def __init__(self) -> None:
        self.files: dict[str, tuple[int, bytes, str]] = {}
        self.requests: list[str] = []
        self.conditional_hits = 0
        self.airflow = installed_version("apache-airflow") or "3.0.0"
        self.standard = installed_providers().get("apache-airflow-providers-standard", "1.0.0")
        self._defaults()

    def put(self, url: str, body: str | bytes, ctype: str = "text/html", status: int = 200) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.files[url] = (status, data, ctype)

    def remove(self, url: str) -> None:
        self.files.pop(url, None)

    def _defaults(self) -> None:
        core = f"{DOCS}apache-airflow/{self.airflow}/"
        self.put(
            core + "searchindex.js",
            sphinx_index(
                {
                    "index": "Apache Airflow",
                    "howto/operator/bash": "BashOperator",
                    "genindex": "Index",
                    "_api/airflow/models/index": "airflow.models",
                }
            ),
            "text/javascript",
        )
        self.put(
            core + "index.html",
            sphinx_page(
                "Apache Airflow",
                [
                    (
                        "what-is-airflow",
                        "What is Airflow",
                        "<p>Airflow is a platform to author, schedule and monitor workflows.</p>",
                    ),
                ],
            ),
        )
        self.put(
            core + "howto/operator/bash.html",
            sphinx_page(
                "BashOperator",
                [
                    (
                        "templating",
                        "Templating",
                        "<p>Use the BashOperator to execute commands in a "
                        "Bash shell. Jinja templating of bash_command is supported.</p>"
                        '<pre>run_this = BashOperator(task_id="run", bash_command="echo 1")</pre>',
                    ),
                    (
                        "troubleshooting",
                        "Troubleshooting",
                        "<p>Jinja template not found errors "
                        "happen when bash_command ends with .sh.</p>",
                    ),
                ],
            ),
        )
        self.put(core + "_api/airflow/models/index.html", sphinx_page("airflow.models", []))
        # task-sdk: the installed version is not published -> stable
        sdk = f"{DOCS}task-sdk/stable/"
        self.put(sdk + "searchindex.js", sphinx_index({"index": "Task SDK"}), "text/javascript")
        self.put(
            sdk + "index.html",
            sphinx_page(
                "Task SDK",
                [
                    (
                        "dag-decorator",
                        "The dag decorator",
                        "<p>Use @dag from airflow.sdk to define DAGs with the TaskFlow API.</p>",
                    ),
                ],
            ),
        )
        prov = f"{DOCS}apache-airflow-providers/stable/"
        self.put(prov + "searchindex.js", sphinx_index({"index": "Providers"}), "text/javascript")
        self.put(
            prov + "index.html",
            sphinx_page(
                "Providers",
                [
                    ("providers", "Provider packages", "<p>Providers add operators and hooks.</p>"),
                ],
            ),
        )
        std = f"{DOCS}apache-airflow-providers-standard/{self.standard}/"
        self.put(
            std + "searchindex.js",
            sphinx_index(
                {
                    "operators/python": "PythonOperator",
                    "sensors/datetime": "DateTimeSensor",
                }
            ),
            "text/javascript",
        )
        self.put(
            std + "operators/python.html",
            sphinx_page(
                "PythonOperator",
                [
                    (
                        "pythonoperator",
                        "PythonOperator",
                        "<p>Use the PythonOperator to execute Python "
                        "callables. Prefer the @task decorator.</p>",
                    ),
                ],
            ),
        )
        self.put(
            std + "sensors/datetime.html",
            sphinx_page(
                "DateTimeSensor",
                [
                    (
                        "deferrable",
                        "Deferrable mode",
                        "<p>DateTimeSensorAsync waits in the triggerer "
                        "without occupying a worker slot.</p>",
                    ),
                ],
            ),
        )
        helm = f"{DOCS}helm-chart/stable/"
        self.put(helm + "searchindex.js", sphinx_index({"index": "Helm Chart"}), "text/javascript")
        self.put(
            helm + "index.html",
            sphinx_page(
                "Helm Chart for Apache Airflow",
                [
                    (
                        "install",
                        "Installing the chart",
                        "<p>helm install airflow apache-airflow/airflow --namespace airflow</p>",
                    ),
                ],
            ),
        )
        ctl = f"{DOCS}apache-airflow-ctl/stable/"
        self.put(ctl + "searchindex.js", sphinx_index({"index": "airflowctl"}), "text/javascript")
        self.put(
            ctl + "index.html",
            sphinx_page(
                "airflowctl",
                [
                    ("usage", "Usage", "<p>airflowctl talks to the Airflow REST API remotely.</p>"),
                ],
            ),
        )
        self.put(
            f"{ASTRO}airflow/llms.txt",
            "# Airflow documentation\n\n## Versions\n\n- [Airflow 2](https://www.astronomer.io/"
            "docs/airflow/2.x/llms.txt): 118 pages.\n\n## Airflow 3 (current)\n\n"
            "- [Airflow XComs](https://www.astronomer.io/docs/airflow/airflow-passing-data-"
            "between-tasks.md): Pass data.\n"
            "- [Evil](https://evil.example.com/page.md): not allowed.\n",
            "text/plain",
        )
        self.put(
            f"{ASTRO}airflow/airflow-passing-data-between-tasks.md",
            "---\ntitle: Pass data between tasks\n---\n\n## XCom basics\n\nXComs pass small "
            "amounts of data between Airflow tasks. Use an object storage backend for large "
            "data.\n",
            "text/markdown",
        )

    @staticmethod
    def etag(data: bytes) -> str:
        return '"' + hashlib.sha256(data).hexdigest()[:16] + '"'

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        self.requests.append(url)
        found = self.files.get(url)
        if found is None:
            return httpx2.Response(404, text="not found")
        status, data, ctype = found
        etag = self.etag(data)
        if request.headers.get("if-none-match") == etag:
            self.conditional_hits += 1
            return httpx2.Response(304, headers={"etag": etag})
        return httpx2.Response(status, content=data, headers={"content-type": ctype, "etag": etag})

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handler)


class FakeEmbedder:
    """Deterministic bag-of-words vectors (hashing trick)."""

    embedding_model = "fake-embedding-1"

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    async def aembed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed(texts)


def kb_settings(tmp: Path, **kb: Any) -> Any:
    from autoair.settings import Settings

    config: dict[str, Any] = {
        "online": True,
        # Only the fake official sites: the packaged AutoAir documentation (the default
        # autoair source) has its own tests in test_kb_autoair_source.py.
        "sources": [{"id": "official", "kind": "official"}],
        "provider_include": ["standard"],
        "refresh_interval_hours": 0,
        "refresh_on_start": False,
        "fetch": {"requests_per_second": 100, "retries": 0},
    }
    for key, value in kb.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key] = {**config[key], **value}
        else:
            config[key] = value
    return Settings(dags_folder=tmp / "dags", kb=config)


def make_kb(
    tmp: Path,
    fake: FakeDocs | None = None,
    *,
    store: Any = None,
    embedder: Any = None,
    settings: Any = None,
    **kb: Any,
) -> KnowledgeBase:
    settings = settings or kb_settings(tmp, **kb)
    return KnowledgeBase(
        settings,
        store=store or LocalStore(tmp / "kb.sqlite3"),
        state_store=FileDocumentStore(str(tmp / "kb-state")),
        transport=fake.transport() if fake is not None else None,
        resolve_dns=False,
        embedder=embedder,
    )


__all__ = [
    "ASTRO",
    "DOCS",
    "FakeDocs",
    "FakeEmbedder",
    "kb_settings",
    "make_kb",
    "sphinx_index",
    "sphinx_page",
]
