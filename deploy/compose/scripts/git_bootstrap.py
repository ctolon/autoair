"""One-shot: prepare Gitea as AutoAir's git deploy target and register the Airflow connection.

1. create the repository GITEA_ADMIN_USER/GITEA_REPO (main branch, initial commit);
2. create an access token, unless the connection already holds a working one (older demo
   tokens are revoked when a new one is created);
3. store url + user + token in the Airflow connection GIT_CONN_ID (metadata database, password
   encrypted with Airflow's Fernet key).

Runs after `airflow db migrate` and BEFORE the Airflow components and AutoAir replicas start:
they build the git deploy target / GitDagBundle once at startup from that connection. The
token is only ever written to the Airflow connection: AutoAir and the GitDagBundle both
resolve it by conn_id. It is never printed, passed on a command line or written to disk.
"""

from __future__ import annotations

import os
import sys
import time

import httpx2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _demo_http import fail, log, wait_for

GITEA = os.environ["GITEA_URL"].rstrip("/")
USER = os.environ["GITEA_ADMIN_USER"]
PASSWORD = os.environ["GITEA_ADMIN_PASSWORD"]
REPO = os.environ.get("GITEA_REPO", "dags")
CONN_ID = os.environ.get("GIT_CONN_ID", "gitea_dags")
TOKEN_PREFIX = "autoair-demo-"  # noqa: S105 (a token *name* prefix)


def main() -> None:
    wait_for(f"{GITEA}/api/healthz")
    gitea = httpx2.Client(base_url=f"{GITEA}/api/v1", auth=(USER, PASSWORD), timeout=30)

    resp = gitea.get(f"/repos/{USER}/{REPO}")
    if resp.status_code == 404:
        resp = gitea.post(
            "/user/repos",
            json={
                "name": REPO,
                "auto_init": True,
                "default_branch": "main",
                "private": True,
                "description": "DAGs deployed by the AutoAir demo (GitDagBundle 'gitea')",
            },
        )
        if not resp.is_success:
            fail(f"creating repository {USER}/{REPO}: {resp.status_code} {resp.text[:200]}")
        log(f"repository {USER}/{REPO} created")
    elif resp.is_success:
        log(f"repository {USER}/{REPO} exists")
    else:
        fail(f"reading repository {USER}/{REPO}: {resp.status_code}")

    current = stored_token()
    if (
        current
        and httpx2.get(
            f"{GITEA}/api/v1/user", headers={"Authorization": f"token {current}"}, timeout=30
        ).is_success
    ):
        # Keep a working token: running replicas resolved it at startup, rotating it on
        # every `docker compose up` would lock them out until they restart.
        log(f"Airflow connection {CONN_ID!r} already holds a valid token, kept")
        return

    for tok in gitea.get(f"/users/{USER}/tokens").json() or []:
        if str(tok.get("name", "")).startswith(TOKEN_PREFIX):
            gitea.delete(f"/users/{USER}/tokens/{tok['id']}")
    resp = gitea.post(
        f"/users/{USER}/tokens",
        json={
            "name": f"{TOKEN_PREFIX}{int(time.time())}",
            "scopes": ["write:repository", "read:user"],
        },
    )
    if not resp.is_success:
        fail(f"creating access token: {resp.status_code} {resp.text[:200]}")
    token = resp.json()["sha1"]

    register_connection(token)
    log(f"Airflow connection {CONN_ID!r} -> {GITEA}/{USER}/{REPO}.git (token stored there only)")


def stored_token() -> str | None:
    """The token of the existing Airflow connection, if any."""
    from airflow.models.connection import Connection
    from airflow.utils.session import create_session
    from sqlalchemy import select

    with create_session() as session:
        conn = session.scalar(select(Connection).where(Connection.conn_id == CONN_ID))
        return conn.password if conn is not None and conn.password else None


def register_connection(token: str) -> None:
    """Create or update the connection through Airflow's ORM (AIRFLOW__DATABASE__*)."""
    from airflow.models.connection import Connection
    from airflow.utils.session import create_session
    from sqlalchemy import select

    with create_session() as session:
        conn = session.scalar(select(Connection).where(Connection.conn_id == CONN_ID))
        if conn is None:
            conn = Connection(conn_id=CONN_ID, conn_type="git")
            session.add(conn)
        conn.conn_type = "git"
        conn.description = "AutoAir demo: Gitea repository (token created by git-bootstrap)"
        conn.host = f"{GITEA}/{USER}/{REPO}.git"
        conn.login = USER
        conn.set_password(token)


if __name__ == "__main__":
    main()
