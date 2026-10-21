"""Query rewriting for retrieval.

* :func:`expand_query` - deterministic expansion with Airflow / cloud vocabulary
  (``k8s`` -> ``kubernetes``, ``gcs`` -> ``google cloud storage``, ``s3`` -> ``aws``),
  always on unless ``ai.retrieval.query_expansion`` is false. Expansion terms are
  appended (BM25 rewards documents that use any of the names).
* :func:`rewrite_queries` - optional LLM rewrite into a few precise search queries
  (``ai.retrieval.llm_query_rewrite``), used by research; results of all variants are
  fused with Reciprocal Rank Fusion.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

SYNONYMS: dict[str, tuple[str, ...]] = {
    "k8s": ("kubernetes", "pod"),
    "kubernetes": ("k8s", "pod"),
    "gcs": ("google", "cloud", "storage"),
    "gcp": ("google", "cloud"),
    "bq": ("bigquery",),
    "bigquery": ("google",),
    "s3": ("aws", "amazon"),
    "aws": ("amazon",),
    "redshift": ("aws", "amazon"),
    "emr": ("aws", "amazon"),
    "lambda": ("aws", "amazon"),
    "sqs": ("aws", "amazon"),
    "sns": ("aws", "amazon"),
    "athena": ("aws", "amazon"),
    "glue": ("aws", "amazon"),
    "adls": ("azure", "data", "lake"),
    "blob": ("azure", "wasb"),
    "adf": ("azure", "data", "factory"),
    "postgres": ("postgresql", "sql"),
    "postgresql": ("postgres",),
    "mysql": ("sql",),
    "mssql": ("sql", "server"),
    "snowflake": ("sql",),
    "shell": ("bash", "command"),
    "bash": ("shell", "command"),
    "cron": ("schedule", "timetable"),
    "schedule": ("timetable", "cron"),
    "wait": ("sensor", "deferrable"),
    "sensor": ("wait", "poke", "reschedule"),
    "email": ("smtp", "notifier"),
    "slack": ("notifier", "webhook"),
    "http": ("api", "rest", "request"),
    "api": ("http", "rest"),
    "sftp": ("ssh", "file", "transfer"),
    "ftp": ("file", "transfer"),
    "dbt": ("cosmos", "transform"),
    "spark": ("pyspark", "submit"),
    "docker": ("container", "image"),
    "xcom": ("return", "value", "task"),
    "dataset": ("asset",),
    "asset": ("dataset", "outlet", "inlet"),
    "sla": ("deadline",),
    "retry": ("retries", "retry_delay"),
    "timeout": ("execution_timeout",),
}

_WORD = re.compile(r"[A-Za-z0-9_]+")


def expand_query(query: str, extra: dict[str, tuple[str, ...]] | None = None) -> str:
    vocab = {**SYNONYMS, **(extra or {})}
    words = [w.lower() for w in _WORD.findall(query)]
    added: list[str] = []
    seen = set(words)
    for word in words:
        for syn in vocab.get(word, ()):
            if syn not in seen:
                seen.add(syn)
                added.append(syn)
    return f"{query} {' '.join(added)}".strip() if added else query


class _Rewrite(BaseModel):
    queries: list[str] = Field(default_factory=list, max_length=4)


REWRITE_SYSTEM = (
    "You turn a user's question about Apache Airflow into 1-3 short keyword search "
    "queries for a search engine over Airflow provider docs, operator docstrings and "
    "source code. Use exact class / module / parameter names when you know them. "
    'Answer with JSON: {"queries": ["..."]}.'
)


async def rewrite_queries(llm: Any, query: str) -> list[str]:
    """The query plus up to three LLM rewrites (failures fall back to the query)."""
    from autoair.ai.llm import LLMError

    try:
        out = await llm.acomplete_structured(REWRITE_SYSTEM, query, _Rewrite, max_tokens=300)
    except LLMError:
        return [query]
    extra = [q.strip() for q in out.queries if q.strip() and q.strip() != query]
    return [query, *extra[:3]]


__all__ = ["SYNONYMS", "expand_query", "rewrite_queries"]
