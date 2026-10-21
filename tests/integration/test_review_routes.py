"""Rules-only review and secret scan: available with ``features.ai`` off (no AI gate)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.settings import Settings

pytestmark = pytest.mark.airflow

LEAKY = {
    "dag_id": "leaky",
    "nodes": [
        {
            "kind": "operator",
            "id": "t",
            "task_id": "t",
            "operator": "airflow.providers.standard.operators.bash.BashOperator",
            "params": {"bash_command": "curl -u admin:" + "hunter2hunter2" + " https://x"},
        }
    ],
}


def test_rules_review_and_secret_scan_without_ai(tmp_path: Path) -> None:
    settings = Settings(dags_folder=tmp_path, subfolder="", features={"ai": False})
    client = TestClient(create_app(build_services(settings, AuthBackend())))
    assert client.post("/api/ai/review", json={"spec": LEAKY}).status_code == 404
    review = client.post("/api/review/rules", json={"spec": LEAKY})
    assert review.status_code == 200, review.text
    assert review.json()["kind"] == "review"
    assert review.json()["ai_used"] is False
    assert review.json()["findings"]  # e.g. no retries
    scan = client.post("/api/lint/secrets", json={"spec": LEAKY})
    assert scan.status_code == 200, scan.text
    report = scan.json()
    assert report["kind"] == "leaks"
    assert any(f["category"] == "secret" for f in report["findings"]), report["findings"]
    # finding texts mask the value (the spec echoed in proposed_spec is the caller's own)
    assert "hunter2hunter2" not in str(report["findings"])
    assert client.post("/api/lint/secrets", json={"spec": {"nodes": "x"}}).status_code == 422
