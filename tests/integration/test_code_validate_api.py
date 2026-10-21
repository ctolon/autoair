"""POST /api/lsp/validate over HTTP: size limit, response shape, permission."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoair import sections
from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.settings import Settings

pytestmark = pytest.mark.airflow


@pytest.fixture(autouse=True)
def _reset_sections():
    yield
    sections.reset()


def client(tmp_path: Path, **settings: object) -> TestClient:
    s = Settings(dags_folder=tmp_path, subfolder="", **settings)  # type: ignore[arg-type]
    return TestClient(create_app(build_services(s, AuthBackend())))


def test_validate_json(tmp_path):
    resp = client(tmp_path).post("/api/lsp/validate", json={"language": "json", "code": "{,}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["validated"] is True
    assert body["tools"] == [{"name": "json", "available": True, "ran": True, "note": None}]
    assert body["diagnostics"][0]["line"] == 1
    assert body["diagnostics"][0]["source"] == "json"


def test_validate_size_limit(tmp_path):
    c = client(tmp_path, editor={"max_source_chars": 1000})
    resp = c.post("/api/lsp/validate", json={"language": "python", "code": "x" * 2000})
    assert resp.status_code == 422
    assert "AUTOAIR_EDITOR__MAX_SOURCE_CHARS" in resp.json()["detail"]


def test_unknown_language_is_rejected(tmp_path):
    resp = client(tmp_path).post("/api/lsp/validate", json={"language": "cobol", "code": ""})
    assert resp.status_code == 422
