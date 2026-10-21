"""Canvas icons: bundled integration logos, custom uploads (re-encoded), assignments."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.icons import IconError, builtin_files, infer_icon, normalize_png
from autoair.settings import Settings
from autoair.spec import DagSpec

pytestmark = pytest.mark.airflow


def png(size=(300, 200), color=(220, 40, 40, 255), fmt="PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGBA" if fmt == "PNG" else "RGB", size, color[: 4 if fmt == "PNG" else 3]).save(
        out, format=fmt
    )
    return out.getvalue()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(dags_folder=tmp_path, subfolder="", ai={"enabled": False})
    return TestClient(create_app(build_services(settings, AuthBackend())))


def test_bundled_logos_and_inference():
    assert len(builtin_files()) > 150
    s3 = infer_icon(
        class_name="S3CreateBucketOperator",
        module="airflow.providers.amazon.aws.operators.s3",
        provider="apache-airflow-providers-amazon",
        integration="Amazon Simple Storage Service (S3)",
    )
    assert s3 is not None
    assert "storage-service-s3" in s3
    jira = infer_icon(
        class_name="JiraOperator",
        module="airflow.providers.atlassian.jira.operators.jira",
        provider="apache-airflow-providers-atlassian-jira",
        integration=None,
    )
    assert jira == "builtin:jira"
    # Third-party package without integration metadata: inferred from the class name.
    dbt = infer_icon(
        class_name="DbtRunOperator",
        module="airflow_dbt_python.operators.dbt",
        provider="airflow-dbt-python",
        integration=None,
    )
    assert dbt == "builtin:dbt"
    py = infer_icon(
        class_name="PythonOperator",
        module="airflow.providers.standard.operators.python",
        provider="apache-airflow-providers-standard",
        integration=None,
    )
    assert py == "builtin:python"


def test_upload_is_reencoded_and_validated():
    out = Image.open(io.BytesIO(normalize_png(png((1000, 500)))))
    assert out.format == "PNG"
    assert max(out.size) <= 128
    assert out.info.get("exif") is None
    assert Image.open(io.BytesIO(normalize_png(png(fmt="JPEG")))).format == "PNG"
    with pytest.raises(IconError):
        normalize_png(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    with pytest.raises(IconError):
        normalize_png(b"GIF89a-not-really")
    with pytest.raises(IconError):
        normalize_png(b"x" * (600 * 1024))


def test_icon_api_roundtrip(client):
    icons = client.get("/api/icons").json()
    jira = next(i for i in icons if i["id"] == "builtin:jira")
    got = client.get(f"/api{jira['url']}")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/")
    assert "sandbox" in got.headers["content-security-policy"]

    data = base64.b64encode(png()).decode()
    up = client.post(
        "/api/icons", json={"name": "GitLab Logo", "data": f"data:image/png;base64,{data}"}
    )
    assert up.status_code == 200, up.text
    assert up.json()["id"] == "custom:gitlab-logo"
    assert client.get("/api/icons/custom:gitlab-logo").headers["content-type"] == "image/png"
    assert client.post("/api/icons", json={"name": "x", "data": "!!!"}).status_code == 400

    op = "airflow.providers.standard.operators.empty.EmptyOperator"
    a = client.put("/api/icons/assignments", json={"operator": op, "icon": "custom:gitlab-logo"})
    assert a.json()["operators"][op] == "custom:gitlab-logo"
    listed = {o["fqn"]: o for o in client.get("/api/catalog/operators").json()}
    assert listed[op]["icon"] == "custom:gitlab-logo"
    assert listed["airflow.providers.standard.operators.python.PythonOperator"]["icon"] == (
        "builtin:python"
    )
    bad = client.put("/api/icons/assignments", json={"operator": op, "icon": "custom:nope"})
    assert bad.status_code == 400
    # Deleting an icon also drops its assignments.
    assert client.delete("/api/icons/custom:gitlab-logo").json() == {"deleted": True}
    assert op not in client.get("/api/icons/assignments").json()["operators"]
    assert client.delete("/api/icons/builtin:jira").status_code == 400
    assert client.get("/api/icons/custom:../../etc").status_code == 404


def test_node_icon_is_canvas_only():
    spec = DagSpec.model_validate(
        {
            "dag_id": "icons",
            "nodes": [
                {
                    "kind": "operator",
                    "id": "a",
                    "task_id": "a",
                    "icon": "builtin:jira",
                    "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
                }
            ],
        }
    )
    from autoair.codegen import render

    assert "jira" not in render(spec)
    with pytest.raises(ValueError, match="icon"):
        DagSpec.model_validate(
            {"dag_id": "x", "nodes": [{**spec.nodes[0].model_dump(), "icon": "http://evil"}]}
        )


def test_operators_without_a_logo_get_a_concept_glyph():
    from autoair.icons import builtin_bytes, infer_icon

    def icon(
        cls: str, module: str, provider: str = "apache-airflow-providers-common-ai"
    ) -> str | None:
        return infer_icon(class_name=cls, module=module, provider=provider, integration=None)

    assert (
        icon("AgentOperator", "airflow.providers.common.ai.operators.agent")
        == "builtin:glyph-agent"
    )
    assert icon("LLMOperator", "airflow.providers.common.ai.operators.llm") == "builtin:glyph-llm"
    std = "apache-airflow-providers-standard"
    assert icon("HITLBranchOperator", "airflow.providers.standard.operators.hitl", std) == (
        "builtin:glyph-human"
    )
    assert icon("TimeDeltaSensor", "airflow.providers.standard.sensors.time_delta", std) == (
        "builtin:glyph-hourglass"
    )
    assert icon("GitlabMergeRequestSensor", "acme.gitlab", "custom:plugins") == "builtin:gitlab"
    # Vendor logos still win over concept glyphs.
    assert (
        icon(
            "BedrockInvokeModelOperator",
            "airflow.providers.amazon.aws.operators.bedrock",
            "apache-airflow-providers-amazon",
        )
        != "builtin:glyph-llm"
    )
    data = builtin_bytes("builtin:glyph-agent")
    assert data is not None
    assert data[1] == "image/svg+xml"
