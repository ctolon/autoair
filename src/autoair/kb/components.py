"""The documentation components the knowledge base knows, and installed versions.

Apache components are Sphinx sites under ``airflow.apache.org/docs/<package>/<version>/``
(the same layout in a downloaded copy or an internal mirror) and rst sources under fixed
folders of the apache/airflow repository. Astronomer components are ``llms.txt`` indexes of
Markdown pages. ``autoair`` is AutoAir's own documentation, packaged with the wheel
(:mod:`autoair.docs`).
"""

from __future__ import annotations

import fnmatch
import importlib.metadata as md
import re
from dataclasses import dataclass
from typing import Any

from autoair.kb.models import ComponentInfo

#: The official Apache Airflow documentation site (mirrors are ``site`` sources).
OFFICIAL_DOCS = "https://airflow.apache.org/docs/"
PROVIDER_PREFIX = "apache-airflow-providers-"
#: Docs package with the general provider documentation (packages list, how-tos).
PROVIDERS_INDEX = "apache-airflow-providers"


@dataclass(frozen=True)
class Component:
    id: str
    title: str
    family: str
    default: bool
    description: str
    site_package: str | None = None
    distribution: str | None = None
    repo_path: str | None = None
    llms_url: str | None = None


COMPONENTS: dict[str, Component] = {
    c.id: c
    for c in (
        Component(
            "core",
            "Apache Airflow",
            "apache",
            True,
            "Core documentation: concepts, how-tos, configuration, CLI, REST API, upgrades.",
            site_package="apache-airflow",
            distribution="apache-airflow",
            repo_path="airflow-core/docs",
        ),
        Component(
            "task_sdk",
            "Task SDK",
            "apache",
            True,
            "airflow.sdk: DAG authoring interface (Airflow 3).",
            site_package="task-sdk",
            distribution="apache-airflow-task-sdk",
            repo_path="task-sdk/docs",
        ),
        Component(
            "providers",
            "Provider packages",
            "apache",
            True,
            "Operators, hooks, sensors and connections of the provider packages.",
        ),
        Component(
            "helm_chart",
            "Helm chart",
            "apache",
            True,
            "The official Helm chart for Kubernetes.",
            site_package="helm-chart",
            repo_path="chart/docs",
        ),
        Component(
            "airflowctl",
            "airflowctl",
            "apache",
            True,
            "The airflowctl remote command-line client.",
            site_package="apache-airflow-ctl",
            distribution="apache-airflow-ctl",
            repo_path="airflow-ctl/docs",
        ),
        Component(
            "astronomer_airflow",
            "Astronomer: Airflow guides",
            "astronomer",
            True,
            "Astronomer's Airflow tutorials, concepts and guides (formerly Learn).",
            llms_url="https://www.astronomer.io/docs/airflow/llms.txt",
        ),
        Component(
            "astro_cli",
            "Astronomer: Astro CLI",
            "astronomer",
            False,
            "The Astro CLI (local development of Airflow projects).",
            llms_url="https://www.astronomer.io/docs/cli/llms.txt",
        ),
        Component(
            "astro",
            "Astronomer: Astro",
            "astronomer",
            False,
            "Astro, Astronomer's managed Airflow platform.",
            llms_url="https://www.astronomer.io/docs/astro/llms.txt",
        ),
        Component(
            "astro_runtime",
            "Astronomer: Astro Runtime",
            "astronomer",
            False,
            "Astro Runtime, Astronomer's Airflow distribution.",
            llms_url="https://www.astronomer.io/docs/runtime/llms.txt",
        ),
        Component(
            "autoair",
            "AutoAir",
            "autoair",
            True,
            "AutoAir's own documentation (installation, builder, deploy, configuration, IAM, "
            "AI and MCP), packaged with the installed version: read without network.",
            distribution="autoair",
        ),
    )
}


def component_infos() -> list[ComponentInfo]:
    return [
        ComponentInfo(
            id=c.id,
            title=c.title,
            family="astronomer"
            if c.family == "astronomer"
            else "autoair"
            if c.family == "autoair"
            else "apache",
            default=c.default,
            description=c.description,
        )
        for c in COMPONENTS.values()
    ]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_version(distribution: str) -> str | None:
    try:
        return md.version(distribution)
    except md.PackageNotFoundError:
        return None


def installed_providers() -> dict[str, str]:
    """``apache-airflow-providers-*`` distribution -> installed version."""
    out: dict[str, str] = {}
    for dist in md.distributions():
        meta: Any = dist.metadata  # email.message.Message: .get() avoids KeyError
        name = _norm(str(meta.get("Name") or ""))
        if name.startswith(PROVIDER_PREFIX) and name not in out:
            out[name] = str(meta.get("Version") or "")
    return dict(sorted(out.items()))


def provider_short(package: str) -> str:
    """``apache-airflow-providers-common-sql`` -> ``common-sql``."""
    return package[len(PROVIDER_PREFIX) :] if package.startswith(PROVIDER_PREFIX) else package


def provider_wanted(package: str, include: list[str], exclude: list[str]) -> bool:
    short = provider_short(package)
    names = (short, package)

    def hit(patterns: list[str]) -> bool:
        return any(fnmatch.fnmatchcase(n, p) for p in patterns for n in names)

    return hit(include or ["*"]) and not hit(exclude)


def provider_repo_path(package: str) -> str:
    """Docs folder of a provider in apache/airflow: ``providers/common/sql/docs``."""
    return "providers/" + provider_short(package).replace("-", "/") + "/docs"


def resolve_versions(wanted: list[str], installed: str | None) -> list[tuple[str, bool]]:
    """Versions to index as ``(version, from_installed)``; ``installed`` without an
    installed package becomes ``stable``. Duplicates are dropped, order kept."""
    out: list[tuple[str, bool]] = []
    for entry in wanted or ["installed"]:
        value = entry.strip()
        if not value:
            continue
        if value == "installed":
            pair = (installed, True) if installed else ("stable", False)
        else:
            pair = (value, False)
        if pair[0] not in [v for v, _ in out]:
            out.append(pair)
    return out


__all__ = [
    "COMPONENTS",
    "OFFICIAL_DOCS",
    "PROVIDERS_INDEX",
    "PROVIDER_PREFIX",
    "Component",
    "component_infos",
    "installed_providers",
    "installed_version",
    "provider_repo_path",
    "provider_short",
    "provider_wanted",
    "resolve_versions",
]
