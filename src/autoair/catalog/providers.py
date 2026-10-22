"""Import-free index of installed Airflow providers.

Each provider registers an ``apache_airflow_provider`` entry point pointing at a
generated ``get_provider_info()`` function that returns one big dict literal. We
read it with :mod:`ast` (``literal_eval``) instead of importing it — importing any
``airflow.*`` module initialises Airflow's configuration (~0.5 s and side effects).
When a provider's info is not a pure literal we fall back to loading the entry point.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from importlib.metadata import Distribution, distributions
from typing import Any

from autoair.catalog.static import find_module_file

PROVIDER_ENTRY_POINT_GROUP = "apache_airflow_provider"


@dataclass
class ProviderInfo:
    package: str
    version: str
    data: dict[str, Any]
    requires_airflow: str | None = None


@dataclass
class ProviderIndex:
    providers: dict[str, ProviderInfo] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def items(self) -> list[tuple[str, ProviderInfo]]:
        return sorted(self.providers.items())


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def airflow_distributions() -> list[tuple[str, str]]:
    """Sorted ``(name, version)`` of installed ``apache-airflow*`` distributions and of every
    third-party provider (``apache_airflow_provider`` entry point)."""
    seen: dict[str, str] = {}
    for dist in distributions():
        name = _normalize((dist.name or "") or "")
        from autoair.catalog.discovery import depends_on_airflow

        is_provider = any(ep.group == PROVIDER_ENTRY_POINT_GROUP for ep in dist.entry_points)
        if name.startswith("apache-airflow") or is_provider or depends_on_airflow(dist):
            seen.setdefault(name, dist.version)
    return sorted(seen.items())


def distributions_fingerprint() -> str:
    raw = json.dumps(airflow_distributions(), separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _requires_airflow(dist: Distribution) -> str | None:
    for req in dist.requires or []:
        if ";" in req and "extra ==" in req:
            continue
        m = re.match(r"\s*apache-airflow(?:-core)?\s*(\[[^\]]*\])?\s*\(?([^;()]*)\)?", req)
        if m and not re.match(r"\s*apache-airflow-(?!core)", req):
            spec = m.group(2).strip()
            return spec or None
    return None


def _static_provider_info(target: str) -> dict[str, Any] | None:
    module, _, func = target.partition(":")
    path = find_module_file(module.strip())
    if path is None:
        return None
    try:
        tree = ast.parse(path.read_bytes())
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func.strip():
            for stmt in node.body:
                if isinstance(stmt, ast.Return) and stmt.value is not None:
                    try:
                        value = ast.literal_eval(stmt.value)
                    except (ValueError, SyntaxError, TypeError):
                        return None
                    return value if isinstance(value, dict) else None
    return None


def load_provider_index(*, allow_import: bool = True) -> ProviderIndex:
    """Read every installed provider's metadata (statically when possible)."""
    index = ProviderIndex()
    for dist in distributions():
        eps = [ep for ep in dist.entry_points if ep.group == PROVIDER_ENTRY_POINT_GROUP]
        if not eps:
            continue
        ep = eps[0]
        data = _static_provider_info(ep.value)
        if data is None and allow_import:
            try:
                data = ep.load()()
            except Exception as exc:
                index.errors.append((ep.value, f"{type(exc).__name__}: {exc}"))
                continue
        if not isinstance(data, dict):
            index.errors.append((ep.value, "provider info is not a literal dict"))
            continue
        package = str(data.get("package-name") or _normalize((dist.name or "") or ""))
        if package in index.providers:
            continue
        index.providers[package] = ProviderInfo(
            package=package,
            version=dist.version,
            data=data,
            requires_airflow=_requires_airflow(dist),
        )
    return index


__all__ = [
    "PROVIDER_ENTRY_POINT_GROUP",
    "ProviderIndex",
    "ProviderInfo",
    "airflow_distributions",
    "distributions_fingerprint",
    "load_provider_index",
]
