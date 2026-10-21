from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.settings import Settings

pytestmark = pytest.mark.airflow
otel = pytest.importorskip("opentelemetry.sdk.trace")


def test_tracing_spans(tmp_path):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        otel_enabled=True,
        ai={"enabled": False},
        features={"mcp": False},
    )
    client = TestClient(create_app(build_services(settings, AuthBackend())))
    assert client.post("/api/validate", json={"dag_id": "t"}).json()["ok"]
    client.get("/api/health")
    names = [s.name for s in exporter.get_finished_spans()]
    assert "validate" in names
    assert any("/validate" in n for n in names)
    # (Health-check exclusion is not asserted: some Airflow versions instrument FastAPI
    # globally, which also traces our health endpoint regardless of our exclusions.)
