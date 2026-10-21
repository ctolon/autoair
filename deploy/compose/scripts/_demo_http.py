"""Small helpers shared by the demo's one-shot Python jobs (stdlib + httpx2 only)."""

from __future__ import annotations

import sys
import time

import httpx2


def log(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def wait_for(url: str, *, timeout: float = 300, verify: bool | str = True) -> None:
    """Poll ``url`` until it answers 2xx."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if httpx2.get(url, timeout=5, verify=verify).is_success:
                return
        except httpx2.HTTPError:
            pass
        if time.monotonic() > deadline:
            fail(f"{url} did not answer within {timeout:.0f}s")
        time.sleep(2)


def airflow_token(airflow_url: str) -> str:
    """An Airflow access token. The demo runs SimpleAuthManager with all users as admins,
    where GET /auth/token hands out an admin token without credentials."""
    resp = httpx2.get(f"{airflow_url}/auth/token", timeout=15)
    if not resp.is_success:
        fail(f"Airflow /auth/token answered {resp.status_code}")
    return str(resp.json()["access_token"])
