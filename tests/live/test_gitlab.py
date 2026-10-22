"""AutoAir's GitLab deploy target against a real GitLab (``./scripts/live-gitlab.sh``).

AUTOAIR_LIVE_GITLAB_URL=http://localhost:8929 AUTOAIR_LIVE_GITLAB_TOKEN=glpat-…
AUTOAIR_LIVE_GITLAB_PROJECT=root/autoair-dags pytest tests/live/test_gitlab.py -m live
"""

from __future__ import annotations

import os
import time

import httpx2
import pytest

from autoair.rendering import render_files
from autoair.spec import DagSpec
from autoair.storage import ConflictError
from autoair.storage.config import GitStorageSettings, StorageSettings, build_target

URL = os.environ.get("AUTOAIR_LIVE_GITLAB_URL", "")
TOKEN = os.environ.get("AUTOAIR_LIVE_GITLAB_TOKEN", "")
PROJECT = os.environ.get("AUTOAIR_LIVE_GITLAB_PROJECT", "root/autoair-dags")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not (URL and TOKEN), reason="AUTOAIR_LIVE_GITLAB_URL/TOKEN not set"),
]
SUFFIX = str(int(time.time()))


def spec(dag_id: str, task: str = "a") -> DagSpec:
    return DagSpec.model_validate(
        {
            "dag_id": dag_id,
            "nodes": [
                {
                    "kind": "operator",
                    "id": "a",
                    "task_id": task,
                    "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
                }
            ],
        }
    )


def target(mode: str, **git: object):
    return build_target(
        StorageSettings(
            mode="git",
            git=GitStorageSettings(
                provider="gitlab",
                repo=PROJECT,
                token=TOKEN,  # type: ignore[arg-type]
                api_url=f"{URL}/api/v4",
                base_branch="main",
                subdir="dags",
                mode=mode,  # type: ignore[arg-type]
                **git,  # type: ignore[arg-type]
            ),
        ),
        default_local_path=__import__("pathlib").Path("."),
    )


def api(method: str, path: str, **kw: object) -> httpx2.Response:
    return httpx2.request(
        method,
        f"{URL}/api/v4{path}",
        headers={"PRIVATE-TOKEN": TOKEN},
        timeout=30,
        **kw,  # type: ignore[arg-type]
    )


def test_push_mode_commit_history_and_conflict():
    t = target("push")
    dag = spec(f"gl_push_{SUFFIX}")
    first = t.save(dag, render_files(dag))
    assert first.pr_url is None
    stored = t.get(dag.dag_id)
    assert stored is not None
    assert stored.spec.dag_id == dag.dag_id
    raw = api(
        "GET",
        f"/projects/{PROJECT.replace('/', '%2F')}/repository/files/dags%2F{dag.dag_id}.py/raw",
        params={"ref": "main"},
    )
    assert raw.status_code == 200
    assert "EmptyOperator" in raw.text

    changed = spec(dag.dag_id, task="b")
    second = t.save(changed, render_files(changed), base_etag=stored.etag)
    assert second.version != first.version
    assert [h.version for h in t.history(dag.dag_id)][:2] == [second.version, first.version]
    with pytest.raises(ConflictError):  # stale base etag → optimistic locking
        t.save(
            spec(dag.dag_id, task="c"),
            render_files(spec(dag.dag_id, task="c")),
            base_etag=stored.etag,
        )
    deleted = t.delete(dag.dag_id)
    assert deleted
    assert t.get(dag.dag_id) is None


def test_pr_mode_opens_a_merge_request():
    t = target("pr")
    dag = spec(f"gl_pr_{SUFFIX}")
    result = t.save(dag, render_files(dag))
    assert result.pr_url is not None
    assert "/-/merge_requests/" in result.pr_url
    assert result.branch == f"autoair/{dag.dag_id}"
    # A second deploy updates the same MR branch instead of opening another MR.
    again = t.save(spec(dag.dag_id, task="b"), render_files(spec(dag.dag_id, task="b")))
    assert again.pr_url == result.pr_url
    mrs = api(
        "GET",
        f"/projects/{PROJECT.replace('/', '%2F')}/merge_requests",
        params={"state": "opened", "source_branch": result.branch},
    ).json()
    assert len(mrs) == 1
    assert mrs[0]["target_branch"] == "main"
    assert t.status().kind == "git"
