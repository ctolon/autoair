"""Airflow 3 plugin: serves the builder inside the Airflow API server.

* Airflow >= 3.1: a ``react_apps`` entry adds "DAG Builder" to the navigation and
  renders the UMD bundle natively inside Airflow's UI.
* Airflow 3.0: the same UI is available as a full page at ``/autoair/ui/``.
"""

from __future__ import annotations

import logging
import mimetypes
from typing import Any

from airflow.plugins_manager import AirflowPlugin

from autoair._compat import supports_react_apps

log = logging.getLogger(__name__)

URL_PREFIX = "/autoair"
# StaticFiles must serve the ES module bundle with a JavaScript MIME type.
mimetypes.add_type("application/javascript", ".js")


def _settings() -> Any:
    """Settings of the plugin (``AUTOAIR_*``, ``AUTOAIR_CONFIG_FILE`` / ``[autoair]
    config_file`` in airflow.cfg); ``None`` when they are invalid (logged)."""
    try:
        from autoair.settings import Settings

        settings = Settings(mode="plugin")
        from autoair.logging_setup import apply_levels

        apply_levels(settings.logging)  # Airflow owns handlers/formats: levels only
        return settings
    except Exception:
        log.exception("AutoAir: invalid configuration")
        return None


_SETTINGS = _settings()


def _fastapi_apps() -> list[dict[str, Any]]:
    if _SETTINGS is None:
        return []
    try:
        from autoair.api.app import build_services, create_app
        from autoair.auth import airflow_auth

        app = create_app(build_services(_SETTINGS, airflow_auth()))
    except Exception:  # never take the API server down because of the plugin
        log.exception("AutoAir plugin failed to initialise")
        return []
    return [{"app": app, "url_prefix": URL_PREFIX, "name": "AutoAir DAG Builder"}]


def _react_apps(settings: Any = None) -> list[dict[str, Any]]:
    if not supports_react_apps():
        return []
    from autoair.sections import PluginSettings

    cfg: PluginSettings = getattr(settings, "plugin", None) or PluginSettings()
    common = {"bundle_url": f"{URL_PREFIX}/static/main.js", "icon": f"{URL_PREFIX}/static/icon.svg"}
    apps = [
        # Navigation entry: the full builder.
        {
            **common,
            "name": cfg.nav_name,
            "destination": "nav",
            "url_route": "dag-builder",
            "category": cfg.nav_category,
        },
    ]
    if cfg.dag_tab:
        # "AutoAir" tab on every DAG page: opens (or offers to import) that DAG.
        apps.append(
            {**common, "name": cfg.dag_tab_name, "destination": "dag", "url_route": "autoair"}
        )
    return apps


class AutoAirPlugin(AirflowPlugin):  # type: ignore[misc]
    name = "autoair"
    fastapi_apps = _fastapi_apps()
    react_apps = _react_apps(_SETTINGS)
