"""Bulk import of a DAG repository: per-file summary, existing DAGs are never overwritten
without confirmation (create_only / stored etag)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.importer.bulk import BulkImportError, check_git_url, safe_relative
from autoair.settings import Settings

pytestmark = pytest.mark.airflow

HELPER = "DEFAULT_OWNER = 'data'\n"
DAG = """
from airflow.sdk import DAG
from airflow.providers.standard.operators.empty import EmptyOperator
from common.defaults import DEFAULT_OWNER

with DAG("{dag_id}", schedule=None, default_args={{"owner": DEFAULT_OWNER}}):
    EmptyOperator(task_id="start")
"""


def _client(tmp_path, **settings) -> TestClient:
    services = build_services(
        Settings(dags_folder=tmp_path / "dags", subfolder="", **settings), AuthBackend()
    )
    return TestClient(create_app(services))


def _files() -> list[dict[str, str]]:
    return [
        {"path": "common/__init__.py", "source": ""},
        {"path": "common/defaults.py", "source": HELPER},
        {"path": "dags/orders.py", "source": DAG.format(dag_id="orders")},
        {"path": "dags/customers.py", "source": DAG.format(dag_id="customers")},
        {
            "path": "dags/broken.py",
            "source": "from airflow.sdk import DAG\nraise RuntimeError('dag boom')\n",
        },
        {"path": "tests/test_dags.py", "source": "import airflow  # dag tests\n"},
        {"path": "README.py", "source": "print('hello')\n"},
    ]


def test_bulk_upload_summary_and_create_only(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/interchange/bulk/scan", json={"files": _files()})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    status = {f["path"]: f["status"] for f in data["files"]}
    assert status == {
        "README.py": "no_dag",
        "common/__init__.py": "no_dag",
        "common/defaults.py": "no_dag",
        "dags/broken.py": "error",
        "dags/customers.py": "imported",
        "dags/orders.py": "imported",
        "tests/test_dags.py": "excluded",
    }
    broken = next(f for f in data["files"] if f["path"] == "dags/broken.py")
    assert "dag boom" in " ".join(broken["import_errors"])
    orders = next(f for f in data["files"] if f["path"] == "dags/orders.py")["dags"][0]
    assert orders["spec"]["default_args"]["owner"] == "data"  # helper module resolved
    assert orders["exists"] is False
    assert data["summary"] == {
        "files": 7,
        "imported_files": 2,
        "dags": 2,
        "convertible": 2,
        "existing": 0,
        "errors": 1,
    }

    # deploy one; a second create_only deploy of the same DAG must not overwrite it
    first = client.put("/api/specs/orders", json={"spec": orders["spec"], "create_only": True})
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "deployed"
    again = client.put("/api/specs/orders", json={"spec": orders["spec"], "create_only": True})
    assert again.json()["status"] == "conflict"
    assert again.json()["deployed"] is False

    rescan = client.post("/api/interchange/bulk/scan", json={"files": _files()}).json()
    orders2 = next(f for f in rescan["files"] if f["path"] == "dags/orders.py")["dags"][0]
    assert orders2["exists"] is True
    assert orders2["stored_etag"]
    assert rescan["summary"]["existing"] == 1
    # confirmed overwrite: deploy against the stored version
    confirmed = client.put(
        "/api/specs/orders",
        json={"spec": orders2["spec"], "create_only": True, "base_etag": orders2["stored_etag"]},
    )
    assert confirmed.json()["status"] == "deployed"


def test_bulk_import_respects_python_policy_and_feature(tmp_path):
    no_code = _client(tmp_path, allow_python_code=False)
    assert no_code.post("/api/interchange/bulk/scan", json={"files": _files()}).status_code == 403
    no_imports = _client(tmp_path, features={"imports": False})
    assert no_imports.post("/api/interchange/bulk/scan", json={"files": _files()}).status_code in (
        403,
        404,
    )


def test_bulk_request_validation(tmp_path):
    client = _client(tmp_path, interchange={"bulk_git_allowed_hosts": ["github.com"]})
    assert client.post("/api/interchange/bulk/scan", json={}).status_code == 422
    bad_path = {"files": [{"path": "../evil.py", "source": "import airflow  # dag"}]}
    assert client.post("/api/interchange/bulk/scan", json=bad_path).status_code == 422
    other_host = {"git": {"url": "https://gitlab.example.com/acme/dags.git"}}
    resp = client.post("/api/interchange/bulk/scan", json=other_host)
    assert resp.status_code == 422
    assert "not allowed" in resp.json()["detail"]


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b.git",
        "file:///etc",
        "ssh://git@github.com/a/b.git",
        "https://user:pw@github.com/a/b.git",
        "--upload-pack=x",
    ],
)
def test_git_urls_are_restricted(url):
    with pytest.raises(BulkImportError):
        check_git_url(url, ["*"])


def test_git_disabled_and_paths():
    with pytest.raises(BulkImportError, match="disabled"):
        check_git_url("https://github.com/a/b.git", [])
    assert check_git_url("https://github.com/a/b.git", ["github.com"])
    for bad in ("/abs.py", "a/../../b.py", ""):
        with pytest.raises(BulkImportError):
            safe_relative(bad)
