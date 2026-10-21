"""The ``react_apps`` entries of the Airflow plugin (navigation entry and DAG tab).

Kept apart from :mod:`autoair.plugin`, which builds the whole API app when it is imported, so
the entries can be built (and tested) for any Airflow version without that.
"""

from __future__ import annotations

import logging
from typing import Any

from autoair.sections import PluginSettings

log = logging.getLogger(__name__)

# Airflow's UI evaluates applies_to paths against the DAG record (DAGResponse); tags are a
# list of {name, dag_id}, so this path collects every tag name of the DAG.
DAG_TAG_PATH = "dag.tags.name"


def react_apps(
    cfg: PluginSettings,
    url_prefix: str,
    *,
    react_apps_supported: bool,
    applies_to_supported: bool,
) -> list[dict[str, Any]]:
    """Airflow >= 3.1: "DAG Builder" in the navigation and an "AutoAir" tab on DAG pages.

    On Airflow >= 3.4 ``plugin.dag_tab_tags`` scopes the tab to DAGs with one of those tags.
    """
    if not react_apps_supported:
        return []
    common = {"bundle_url": f"{url_prefix}/static/main.js", "icon": f"{url_prefix}/static/icon.svg"}
    apps: list[dict[str, Any]] = [
        # Navigation entry: the full builder.
        {
            **common,
            "name": cfg.nav_name,
            "destination": "nav",
            "url_route": "dag-builder",
            "category": cfg.nav_category,
        },
    ]
    if not cfg.dag_tab:
        return apps
    # "AutoAir" tab on DAG pages: opens (or offers to import) that DAG.
    tab: dict[str, Any] = {
        **common,
        "name": cfg.dag_tab_name,
        "destination": "dag",
        "url_route": "autoair",
    }
    tags = [t for t in (s.strip() for s in cfg.dag_tab_tags) if t]
    if tags and applies_to_supported:
        tab["applies_to"] = {DAG_TAG_PATH: tags}
    elif tags:
        log.info(
            "AutoAir: plugin.dag_tab_tags needs Airflow >= 3.4 (applies_to); "
            "the DAG tab is shown on every DAG page"
        )
    apps.append(tab)
    return apps
