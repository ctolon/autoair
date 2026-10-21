"""features.airflow off (``autoair local``): nothing calls an Airflow server."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.gateway import AirflowError, AirflowGateway
from autoair.settings import Settings


def _client(tmp_path: Path, **features: bool) -> TestClient:
    settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        import_check_on_deploy=False,
        features=features,
    )
    services = build_services(settings, AuthBackend())
    services.catalog.refresh = lambda: services.catalog.stats()  # type: ignore[method-assign]
    return TestClient(create_app(services))


def _no_airflow(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record (and fail) every gateway call that would reach Airflow."""
    calls: list[str] = []

    def fail(name: str) -> Any:
        async def call(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AirflowError(502, "Airflow must not be called")

        return call

    for name in ("dag_source", "dag_status", "reparse"):
        monkeypatch.setattr(AirflowGateway, name, fail(name))
    return calls


def test_import_from_airflow_is_gated_by_the_airflow_feature(tmp_path, monkeypatch):
    calls = _no_airflow(monkeypatch)
    off = _client(tmp_path, airflow=False)
    resp = off.post("/api/interchange/airflow/some_dag/import")
    assert resp.status_code == 404
    assert "airflow feature is disabled" in resp.json()["detail"]
    assert calls == []

    on = _client(tmp_path, airflow=True)
    resp = on.post("/api/interchange/airflow/some_dag/import")
    assert resp.status_code == 502  # reached the (failing) gateway
    assert calls == ["dag_source"]


def test_deploy_without_the_airflow_feature_does_not_request_a_reparse(
    tmp_path, monkeypatch, simple_spec_data
):
    calls = _no_airflow(monkeypatch)
    client = _client(tmp_path, airflow=False)
    resp = client.put("/api/specs/simple", json={"spec": simple_spec_data})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deployed"], body
    assert "could not contact Airflow" not in body["message"]
    assert body["message"].endswith("Airflow will pick it up on the next bundle refresh")
    assert calls == []
