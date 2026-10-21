"""Airflow UI session -> AutoAir (docs/sso.md), against a mocked Airflow."""

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
from autoair.settings import Settings

pytestmark = pytest.mark.airflow

AIRFLOW = "http://airflow.test:8080"
SIMPLE: dict[str, Any] = {
    "dag_id": "simple",
    "nodes": [
        {
            "kind": "operator",
            "id": "a",
            "task_id": "a",
            "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
        }
    ],
}


def jwt(**claims: Any) -> str:
    def enc(obj: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'HS512', 'typ': 'JWT'})}.{enc(claims)}.signature"


# Built at collection time: lifetimes of a day and two days keep the expiry assertions
# below true however long the suite runs before these tests (slow CI runners take >10 min).
GOOD = jwt(sub="alice", given_name="Alice", family_name="A", exp=time.time() + 86400)
RENEWED = jwt(sub="alice", given_name="Alice", family_name="A", exp=time.time() + 2 * 86400)


class FakeAirflow:
    """Accepts GOOD/RENEWED; /api/v2/version re-issues the cookie (JWT refresh middleware)."""

    def __init__(self) -> None:
        self.valid = {GOOD, RENEWED}
        self.calls: list[httpx2.Request] = []
        self.renew = True

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(request)
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.valid:
            return httpx2.Response(401, json={"detail": "invalid token"})
        if request.url.path == "/api/v2/version":
            headers = {}
            if self.renew:
                headers["set-cookie"] = f"_token={RENEWED}; Path=/; HttpOnly; SameSite=lax"
            return httpx2.Response(200, json={"version": "3.3.2"}, headers=headers)
        if request.url.path == "/api/v2/auth/logout":
            self.valid.discard(token)
            return httpx2.Response(200, json={})
        return httpx2.Response(200, json={"dags": [], "total_entries": 0})


def make(tmp_path: Path, airflow: FakeAirflow | None = None, **settings: Any) -> TestClient:
    fake = airflow or FakeAirflow()
    s = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path / "dags",
        subfolder="",
        ai={"enabled": False},
        airflow_url=AIRFLOW,
        import_check_on_deploy=False,
        iam=IamSettings(),
        **settings,
    )
    transport = httpx2.MockTransport(fake)
    services = build_services(s, bearer_passthrough(TokenVerifier(AIRFLOW, transport=transport)))
    services.sso_transport = transport  # type: ignore[attr-defined]
    from autoair.iam.login import LoginService

    services.login = LoginService(AIRFLOW, s.iam, transport=transport)
    return TestClient(create_app(services), raise_server_exceptions=False)


def test_auth_config_is_public_and_carries_branding(tmp_path: Path) -> None:
    c = make(
        tmp_path, ui={"title": "ACME DAGs", "banner_text": "PRODUCTION", "banner_color": "red"}
    )
    cfg = c.get("/api/auth/config").json()
    assert cfg["mode"] == "standalone"
    assert cfg["login_enabled"] is True
    assert cfg["sso"]["enabled"] is True
    assert cfg["sso"]["cookie_name"] == "_token"
    assert cfg["sso"]["airflow_login_url"] == "/api/auth/sso/login"
    assert cfg["ui"]["title"] == "ACME DAGs"
    assert cfg["ui"]["banner_text"] == "PRODUCTION"


def test_airflow_cookie_signs_in(tmp_path: Path) -> None:
    c = make(tmp_path)
    assert c.get("/api/me").status_code == 401
    assert c.get("/api/auth/session").status_code == 401
    c.cookies.set("_token", GOOD)
    me = c.get("/api/me")
    assert me.status_code == 200, me.text
    assert me.json()["user"] == "alice"
    session = c.get("/api/auth/session").json()
    assert session["source"] == "cookie"
    assert session["user"] == "alice"
    assert session["display_name"] == "Alice A"
    assert session["verified"] is True
    assert session["refreshable"] is True
    assert session["expires_in"] > 3000


def test_bearer_session_is_not_refreshable(tmp_path: Path) -> None:
    c = make(tmp_path)
    headers = {"Authorization": f"Bearer {GOOD}"}
    assert c.get("/api/auth/session", headers=headers).json()["refreshable"] is False
    assert c.post("/api/auth/refresh", headers=headers).status_code == 409


def test_expired_and_rejected_cookies(tmp_path: Path) -> None:
    c = make(tmp_path)
    c.cookies.set("_token", jwt(sub="alice", exp=time.time() - 5))
    assert c.get("/api/auth/session").status_code == 401
    c.cookies.set("_token", jwt(sub="mallory", exp=time.time() + 60))  # unknown to Airflow
    assert c.get("/api/auth/session").status_code == 401
    assert c.get("/api/me").status_code == 401


def test_cookie_name_and_switch(tmp_path: Path) -> None:
    named = make(tmp_path / "named", sso={"cookie_name": "af_session"})
    named.cookies.set("_token", GOOD)
    assert named.get("/api/me").status_code == 401
    named.cookies.clear()
    named.cookies.set("af_session", GOOD)
    assert named.get("/api/me").status_code == 200

    off = make(tmp_path / "off", sso={"enabled": False})
    off.cookies.set("_token", GOOD)
    assert off.get("/api/me").status_code == 401
    assert off.get("/api/auth/config").json()["sso"]["enabled"] is False
    assert off.get("/api/auth/sso/login", follow_redirects=False).status_code == 404


def test_cookie_requests_need_the_csrf_header(tmp_path: Path) -> None:
    c = make(tmp_path)
    c.cookies.set("_token", GOOD)
    body = {"spec": SIMPLE}
    refused = c.put("/api/specs/simple", json=body)
    assert refused.status_code == 403
    assert "X-AutoAir-CSRF" in refused.json()["detail"]
    csrf = {"X-AutoAir-CSRF": "1"}
    cross = c.put("/api/specs/simple", json=body, headers={**csrf, "Origin": "http://evil.example"})
    assert cross.status_code == 403
    same = c.put("/api/specs/simple", json=body, headers={**csrf, "Origin": "http://testserver"})
    assert same.status_code == 200, same.text
    # bearer tokens are never sent by the browser on its own: no CSRF header needed
    c.cookies.clear()
    bearer = c.put("/api/specs/simple", json=body, headers={"Authorization": f"Bearer {GOOD}"})
    assert bearer.status_code == 200, bearer.text
    # safe methods are never blocked
    c.cookies.set("_token", GOOD)
    assert c.get("/api/specs").status_code == 200


def test_refresh_relays_airflows_new_cookie(tmp_path: Path) -> None:
    fake = FakeAirflow()
    c = make(tmp_path, fake, server={"cookie_secure": "always"})
    c.cookies.set("_token", GOOD)
    resp = c.post("/api/auth/refresh", headers={"X-AutoAir-CSRF": "1"})
    assert resp.status_code == 200, resp.text
    set_cookie = resp.headers["set-cookie"]
    assert set_cookie.startswith(f"_token={RENEWED}")
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    # RENEWED expires two days after collection, GOOD after one: a remaining lifetime longer
    # than GOOD's whole lifetime can only be the renewed token's.
    assert resp.json()["expires_in"] > 86400
    sent = next(r for r in fake.calls if r.url.path == "/api/v2/version")
    assert f"_token={GOOD}" in sent.headers["cookie"]

    # Airflow ended the session: the cookie is cleared here too
    fake.valid.clear()
    ended = c.post("/api/auth/refresh", headers={"X-AutoAir-CSRF": "1"})
    assert ended.status_code == 401
    assert (
        "Max-Age=0" in ended.headers["set-cookie"]
        or "max-age=0" in ended.headers["set-cookie"].lower()
    )


def test_logout_propagates_to_the_airflow_session(tmp_path: Path) -> None:
    fake = FakeAirflow()
    c = make(tmp_path, fake)
    c.cookies.set("_token", GOOD)
    out = c.post("/api/auth/logout", headers={"X-AutoAir-CSRF": "1"})
    assert out.status_code == 200, out.text
    assert out.json()["revoked"] is True
    assert "max-age=0" in out.headers["set-cookie"].lower()
    assert GOOD not in fake.valid  # Airflow revoked it

    keep = FakeAirflow()
    c2 = make(tmp_path / "keep", keep, sso={"propagate_logout": False})
    c2.cookies.set("_token", GOOD)
    resp = c2.post("/api/auth/logout", headers={"X-AutoAir-CSRF": "1"})
    assert resp.json()["revoked"] is False
    assert "set-cookie" not in resp.headers
    assert GOOD in keep.valid


def test_sso_login_redirect(tmp_path: Path) -> None:
    # AutoAir behind Airflow's host: Airflow accepts the return URL
    same = make(tmp_path / "same", server={"public_url": "http://airflow.test:8080/autoair"})
    resp = same.get(
        "/api/auth/sso/login", params={"next": "/autoair/ui/?dag=x"}, follow_redirects=False
    )
    assert resp.status_code == 307
    assert resp.headers["location"] == (
        "http://airflow.test:8080/auth/login?next=http%3A%2F%2Fairflow.test%3A8080%2Fautoair"
        "%2Fui%2F%3Fdag%3Dx"
    )
    # another host: Airflow would refuse the return URL -> plain login page
    other = make(
        tmp_path / "other",
        server={"public_url": "https://autoair.example.com"},
        sso={"airflow_public_url": "https://airflow.example.com"},
    )
    resp = other.get("/api/auth/sso/login", params={"next": "/ui/"}, follow_redirects=False)
    assert resp.headers["location"] == "https://airflow.example.com/auth/login"
    # open redirects are not possible
    resp = same.get(
        "/api/auth/sso/login", params={"next": "//evil.example/x"}, follow_redirects=False
    )
    assert "evil" not in resp.headers["location"]


def test_security_headers_on_the_standalone_server(tmp_path: Path) -> None:
    c = make(tmp_path, server={"hsts_seconds": 600, "frame_ancestors": ["'none'"]})
    resp = c.get("/api/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert "strict-transport-security" not in resp.headers  # plain HTTP
    https = TestClient(c.app, base_url="https://testserver").get("/api/health")
    assert https.headers["strict-transport-security"] == "max-age=600"

    plain = make(tmp_path / "off", server={"security_headers": False})
    assert "content-security-policy" not in plain.get("/api/health").headers


def test_ui_page_csp_allows_its_inline_config(tmp_path: Path) -> None:
    from autoair.api import app as app_module

    if app_module._index_html("/api") is None:
        pytest.skip("frontend bundle not built")
    c = make(tmp_path, ui={"title": "ACME"})
    page = c.get("/ui/")
    csp = page.headers["content-security-policy"]
    script = app_module._config_script("/api")
    from autoair.api.platform import script_hash

    assert f"'sha256-{script_hash(script)}'" in csp
    assert "<title>ACME</title>" in page.text


def test_plugin_mode_session_follows_airflows_cookie(tmp_path: Path) -> None:
    """Inside Airflow the cookie is validated by Airflow itself; /auth/session only tells the
    UI whether the session still exists (an Airflow sign-out removes the cookie)."""
    from autoair.auth import AuthBackend

    s = Settings(  # type: ignore[call-arg]
        mode="plugin",
        dags_folder=tmp_path / "dags",
        subfolder="",
        ai={"enabled": False},
        sso={"cookie_name": "ignored_in_plugin_mode"},
    )
    c = TestClient(create_app(build_services(s, AuthBackend(name="airflow"))))
    assert c.get("/api/auth/session").status_code == 401
    c.cookies.set("_token", GOOD)
    session = c.get("/api/auth/session").json()
    assert session["source"] == "cookie"
    assert session["verified"] is False  # Airflow's get_user validates real requests
    cfg = c.get("/api/auth/config").json()
    assert cfg["mode"] == "plugin"
    assert cfg["sso"]["cookie_name"] == "_token"
    # no CSRF middleware in plugin mode (Airflow's SameSite=Lax model) and no security
    # headers of the standalone server
    assert "content-security-policy" not in c.get("/api/health").headers
