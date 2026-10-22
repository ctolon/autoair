"""Templates that carry hand-written DAG code need ``code:raw``, like saving a raw code
DAG: a DAG template's ``spec.raw_code`` (a raw code DAG) and ``spec.source.code`` (the
hand-written file preserve writes back) are arbitrary Python. Python task bodies are not
"raw code" here, exactly as in specs (they are governed by ``allow_python_code``)."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.auth import TokenVerifier, bearer_passthrough
from autoair.iam.config import IamSettings
from autoair.marketplace import build_index, generate_keypair
from autoair.settings import Settings

pytestmark = pytest.mark.airflow

AIRFLOW = "http://airflow.test"
RAW = 'from airflow.sdk import DAG\n\nwith DAG("raw_one", schedule=None):\n    pass\n'
EMPTY = "airflow.providers.standard.operators.empty.EmptyOperator"


def _jwt(**claims: Any) -> str:
    def enc(obj: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'HS512', 'typ': 'JWT'})}.{enc(claims)}.signature"


def as_role(role: str) -> dict[str, str]:
    token = _jwt(sub=role.lower(), role=f"AutoAir {role}", exp=time.time() + 3600)
    return {"Authorization": f"Bearer {token}"}


def _airflow(request: httpx2.Request) -> httpx2.Response:
    if request.url.path == "/api/v2/dags":
        return httpx2.Response(200, json={"dags": [], "total_entries": 0})
    return httpx2.Response(404, json={"detail": "not found"})


def client(tmp_path: Path, **settings: Any) -> TestClient:
    s = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path / "dags",
        subfolder="",
        ai={"enabled": False},
        airflow_url=AIRFLOW,
        iam=IamSettings(strategies=["role_mapping"]),
        **settings,
    )
    verifier = TokenVerifier(AIRFLOW, transport=httpx2.MockTransport(_airflow))
    return TestClient(
        create_app(build_services(s, bearer_passthrough(verifier))), raise_server_exceptions=False
    )


def dag_template(tid: str, **spec: Any) -> dict[str, Any]:
    return {
        "id": tid,
        "title": tid,
        "kind": "dag",
        "spec": {"dag_id": tid.replace("-", "_"), "nodes": [], **spec},
    }


def test_raw_code_dag_template_needs_code_raw(tmp_path: Path) -> None:
    c = client(tmp_path)
    raw = dag_template("raw-one", raw_code=RAW)
    refused = c.post("/api/templates", json=raw, headers=as_role("TemplateManager"))
    assert refused.status_code == 403, refused.text
    assert "code:raw" in refused.json()["detail"]
    assert c.get("/api/templates/raw-one", headers=as_role("Admin")).status_code == 404
    ok = c.post("/api/templates", json=raw, headers=as_role("Admin"))
    assert ok.status_code == 200, ok.text
    # nor can a template manager overwrite it with another version of the code
    again = c.post(
        "/api/templates",
        json=dag_template("raw-one", raw_code=RAW + "# changed\n"),
        headers=as_role("TemplateManager"),
    )
    assert again.status_code == 403


def test_hand_written_source_in_a_template_needs_code_raw(tmp_path: Path) -> None:
    c = client(tmp_path)
    tpl = dag_template(
        "kept-file",
        nodes=[{"kind": "operator", "id": "a", "task_id": "a", "operator": EMPTY}],
        source={"policy": "preserve", "origin": "python", "code": RAW},
    )
    refused = c.post("/api/templates", json=tpl, headers=as_role("TemplateManager"))
    assert refused.status_code == 403, refused.text
    assert c.post("/api/templates", json=tpl, headers=as_role("Admin")).status_code == 200


def test_templates_without_raw_code_keep_template_write(tmp_path: Path) -> None:
    c = client(tmp_path)
    plain = dag_template(
        "plain", nodes=[{"kind": "operator", "id": "a", "task_id": "a", "operator": EMPTY}]
    )
    assert (
        c.post("/api/templates", json=plain, headers=as_role("TemplateManager")).status_code == 200
    )
    # python_task bodies are governed by allow_python_code, as in specs, not by code:raw
    py = {
        "id": "py-frag",
        "title": "py",
        "kind": "fragment",
        "fragment": {
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "p",
                    "task_id": "p",
                    "function_name": "p",
                    "code": "return 1",
                }
            ]
        },
    }
    resp = c.post("/api/templates", json=py, headers=as_role("TemplateManager"))
    assert resp.status_code == 200, resp.text


def test_marketplace_install_of_raw_code_needs_code_raw(tmp_path: Path) -> None:
    private, public = generate_keypair()
    root = tmp_path / "market"
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "raw-one.json").write_text(
        json.dumps(dag_template("raw-one", raw_code=RAW))
    )
    (root / "index.json").write_text(json.dumps(build_index(root, "acme", private)))
    c = client(
        tmp_path,
        marketplace_sources=[
            {"name": "acme", "url": (root / "index.json").as_uri(), "public_key": public}
        ],
    )
    body = {"source": "acme", "entry_id": "raw-one"}
    refused = c.post("/api/marketplace/install", json=body, headers=as_role("TemplateManager"))
    assert refused.status_code == 403, refused.text
    assert "code:raw" in refused.json()["detail"]
    assert c.get("/api/templates/raw-one", headers=as_role("Admin")).status_code == 404
    ok = c.post("/api/marketplace/install", json=body, headers=as_role("Admin"))
    assert ok.status_code == 200, ok.text
