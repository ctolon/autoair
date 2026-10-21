"""Airflow plugin UI entries (navigation + DAG tab) per Airflow version."""

from __future__ import annotations

import logging

import pytest

from autoair import _compat
from autoair.plugin_ui import DAG_TAG_PATH, react_apps
from autoair.sections import PluginSettings


def _apps(cfg: PluginSettings, *, react: bool = True, applies_to: bool = True) -> list[dict]:
    return react_apps(cfg, "/autoair", react_apps_supported=react, applies_to_supported=applies_to)


def test_airflow_30_has_no_react_apps() -> None:
    assert _apps(PluginSettings(), react=False) == []


def test_default_tab_is_on_every_dag_page() -> None:
    nav, tab = _apps(PluginSettings())
    assert nav["destination"] == "nav"
    assert nav["url_route"] == "dag-builder"
    assert tab["destination"] == "dag"
    assert tab["url_route"] == "autoair"
    assert tab["name"] == "AutoAir"
    assert "applies_to" not in tab
    assert tab["bundle_url"] == nav["bundle_url"] == "/autoair/static/main.js"


def test_tab_can_be_switched_off() -> None:
    assert [a["destination"] for a in _apps(PluginSettings(dag_tab=False))] == ["nav"]


def test_tags_scope_the_tab_on_airflow_34() -> None:
    cfg = PluginSettings(dag_tab_tags=["autoair", " managed ", ""])
    tab = _apps(cfg)[1]
    assert tab["applies_to"] == {DAG_TAG_PATH: ["autoair", "managed"]}
    # The navigation entry is never scoped.
    assert "applies_to" not in _apps(cfg)[0]


def test_tags_are_ignored_before_airflow_34(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="autoair.plugin_ui"):
        tab = _apps(PluginSettings(dag_tab_tags=["autoair"]), applies_to=False)[1]
    assert "applies_to" not in tab
    assert "needs Airflow >= 3.4" in caplog.text


def test_blank_tags_do_not_scope() -> None:
    assert "applies_to" not in _apps(PluginSettings(dag_tab_tags=["  "]))[1]


@pytest.mark.airflow
def test_installed_airflow_accepts_the_scoped_tab() -> None:
    """Airflow's own plugin loader keeps the tab and its scoping (3.4+ validates the path
    against DAGResponse and drops a view whose path matches no field)."""
    if not _compat.supports_plugin_applies_to():
        pytest.skip("installed Airflow has no applies_to (< 3.4)")
    from airflow import plugins_manager

    tab = _apps(PluginSettings(dag_tab_tags=["autoair"]))[1]
    assert plugins_manager._validate_applies_to("autoair", tab, "a React App") is True
    assert tab["applies_to"] == {DAG_TAG_PATH: ["autoair"]}


@pytest.mark.airflow
def test_applies_to_detection_matches_the_installed_version() -> None:
    version = _compat.airflow_version()
    assert version is not None
    assert _compat.supports_plugin_applies_to() is (version >= (3, 4, 0))
