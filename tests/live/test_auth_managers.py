"""RBAC of the AutoAir plugin against a *running* Airflow with a real auth manager.

Start one of:
  ./scripts/live-auth-fab.sh        (FAB, port 8091: admin / editor / viewer)
  ./scripts/live-auth-keycloak.sh   (Keycloak, port 8092: same users in realm "airflow")
then: AUTOAIR_LIVE_AIRFLOW_URL=http://localhost:8091 pytest tests/live -m live
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx2
import pytest

URL = os.environ.get("AUTOAIR_LIVE_AIRFLOW_URL", "")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not URL, reason="AUTOAIR_LIVE_AIRFLOW_URL not set"),
]
DAG_ID = f"live_rbac_{int(time.time())}"
SPEC = {
    "dag_id": DAG_ID,
    "nodes": [
        {
            "kind": "operator",
            "id": "a",
            "task_id": "a",
            "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
        }
    ],
}


def token(user: str) -> str:
    """Password grant via Airflow's /auth/token (FAB and Keycloak auth managers both offer it)."""
    r = httpx2.post(f"{URL}/auth/token", json={"username": user, "password": user}, timeout=30)
    assert r.status_code in (200, 201), r.text
    return str(r.json()["access_token"])


@pytest.fixture(scope="module")
def tokens() -> dict[str, str]:
    return {u: token(u) for u in ("admin", "editor", "viewer")}


def api(path: str, tok: str | None = None, method: str = "GET", **kw: object) -> httpx2.Response:
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    return httpx2.request(method, f"{URL}/autoair/api{path}", headers=headers, timeout=60, **kw)  # type: ignore[arg-type]


def test_anonymous_is_rejected():
    assert api("/meta").status_code == 401
    assert api(f"/specs/{DAG_ID}", method="PUT", json={"spec": SPEC}).status_code == 401


def test_every_role_can_read(tokens):
    for user in ("admin", "editor", "viewer"):
        r = api("/meta", tokens[user])
        assert r.status_code == 200, (user, r.text)
        assert api("/specs", tokens[user]).status_code == 200, user


def test_viewer_cannot_deploy_or_run(tokens):
    r = api(f"/specs/{DAG_ID}", tokens["viewer"], "PUT", json={"spec": SPEC})
    assert r.status_code == 403, r.text
    r = api("/runner/test-task", tokens["viewer"], "POST", json={"spec": SPEC, "task_id": "a"})
    assert r.status_code == 403, r.text


def test_editor_and_admin_can_deploy(tokens):
    for user in ("editor", "admin"):
        spec = {**SPEC, "dag_id": f"{DAG_ID}_{user}"}
        r = api(f"/specs/{spec['dag_id']}", tokens[user], "PUT", json={"spec": spec})
        assert r.status_code == 200, (user, r.text)
        assert api(f"/specs/{spec['dag_id']}", tokens["viewer"]).status_code == 200
        api(f"/specs/{spec['dag_id']}", tokens["admin"], "DELETE")


def test_collab_requires_edit_permission(tokens):
    pytest.importorskip("websockets")
    import websockets

    ws_url = URL.replace("http", "ws", 1) + f"/autoair/collab/{DAG_ID}"

    async def connect(ticket: str) -> bool:
        try:
            async with websockets.connect(f"{ws_url}?ticket={ticket}", open_timeout=10) as ws:
                await ws.send(b"\x00\x00\x01\x00")  # sync step 1 with an empty state vector
                await asyncio.wait_for(ws.recv(), 10)
                return True
        except websockets.InvalidStatus:
            return False

    # Tickets come from the authenticated HTTP API and need DAG edit permission.
    assert api(f"/collab/{DAG_ID}/ticket", tokens["viewer"], "POST").status_code == 403
    assert api(f"/collab/{DAG_ID}/ticket", None, "POST").status_code == 401
    ticket = api(f"/collab/{DAG_ID}/ticket", tokens["editor"], "POST")
    assert ticket.status_code == 200, ticket.text
    assert asyncio.run(connect(ticket.json()["ticket"])) is True
    assert asyncio.run(connect("forged.ticket")) is False
    assert asyncio.run(connect(tokens["editor"])) is False  # a JWT is not a ticket
