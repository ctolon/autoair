"""One-shot: deploy the demo DAG specs (seed/specs/*.json) through AutoAir's REST API.

Idempotent: a DAG that AutoAir already knows is left alone. Uses replica A, so the deploy goes
through validation, policy, governance and the configured deploy target like a UI deploy.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import httpx2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _demo_http import airflow_token, fail, log, wait_for

AUTOAIR = os.environ.get("SEED_AUTOAIR_URL", "http://autoair-a:8765").rstrip("/")
AIRFLOW = os.environ.get("SEED_AIRFLOW_URL", "http://airflow-apiserver:8080").rstrip("/")


def main(folder: str) -> None:
    if os.environ.get("SEED_ENABLED", "true").lower() not in ("1", "true", "yes"):
        log("seeding disabled (DEMO_SEED_DAGS=false)")
        return
    wait_for(f"{AUTOAIR}/api/health")
    headers = {"Authorization": f"Bearer {airflow_token(AIRFLOW)}"}
    client = httpx2.Client(base_url=f"{AUTOAIR}/api", headers=headers, timeout=180)
    failures = 0
    for path in sorted(pathlib.Path(folder).glob("*.json")):
        spec = json.loads(path.read_text())
        dag_id = spec["dag_id"]
        if client.get(f"/specs/{dag_id}").status_code == 200:
            log(f"{dag_id}: already present, skipped")
            continue
        resp = client.put(
            f"/specs/{dag_id}",
            json={"spec": spec, "message": f"Seed demo DAG {dag_id}"},
        )
        if resp.is_success:
            data = resp.json()
            where = data.get("pull_request") or data.get("location") or data.get("path") or ""
            log(f"{dag_id}: deployed ({resp.status_code}) {where}".rstrip())
        else:
            failures += 1
            log(f"{dag_id}: deploy failed {resp.status_code}: {resp.text[:400]}")
    if failures:
        fail(f"{failures} demo DAG(s) could not be deployed")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/opt/autoair-demo/seed/specs")
