"""AI API end to end: SSE streaming, per-user models, knowledge admin, audit, feature
flags, and proxy mode (main app -> separate AI service in-process via ASGI transport)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from autoair.ai.config import AiServiceSettings, AiSettings, UserLlmSettings
from autoair.ai.events import parse_sse_frames
from autoair.ai.testing import FakeLLM, tool_calls
from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.settings import Settings

pytestmark = pytest.mark.airflow

BASH = "airflow.providers.standard.operators.bash.BashOperator"


def make(tmp_path: Path, llm: Any = None, **ai: Any) -> tuple[TestClient, Any]:
    settings = Settings(
        dags_folder=tmp_path, subfolder="", ai=AiSettings(**ai), import_check_on_deploy=False
    )
    services = build_services(settings, AuthBackend())
    if llm is not None:
        services.llm = llm
    return TestClient(create_app(services)), services


def frames(resp: httpx2.Response) -> list[dict[str, Any]]:
    assert resp.headers["content-type"].startswith("text/event-stream")
    return parse_sse_frames(resp.text)


def test_status_reports_streaming_runtime_and_role(tmp_path: Path) -> None:
    client, _ = make(tmp_path, FakeLLM())
    status = client.get("/api/ai/status").json()
    assert status["configured"]
    assert status["enabled"]
    assert status["streaming"]
    assert status["llm_mode"] == "central"
    assert status["llm_source"] == "central"
    assert "write" in status["features"]
    assert "bash" in status["writers"]
    assert status["role"] == "admin"  # authentication disabled
    _off, _ = make(tmp_path / "off")
    off_client = TestClient(
        create_app(
            build_services(
                Settings(dags_folder=tmp_path, subfolder="", features={"ai": False}),  # type: ignore[arg-type]
                AuthBackend(),
            )
        )
    )
    assert off_client.get("/api/ai/status").json()["enabled"] is False
    assert off_client.post("/api/ai/leaks", json={"spec": {"dag_id": "x"}}).status_code == 404


def test_ask_stream_tokens_tools_result_and_audit(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            tool_calls(("search_catalog", {"query": "bash"}), text="Searching the catalog"),
            tool_calls(("final_answer", {"answer": "Use BashOperator."})),
        ]
    )
    client, _ = make(tmp_path, llm)
    resp = client.post(
        "/api/ai/ask/stream",
        json={"question": "how do I run a script?", "runtime": "loop", "research": False},
    )
    assert resp.status_code == 200
    events = frames(resp)
    kinds = [e["type"] for e in events]
    assert kinds[-2:] == ["result", "done"]
    assert "token" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    tokens = "".join(e["text"] for e in events if e["type"] == "token")
    assert tokens.startswith("Searching")
    result = events[-2]
    assert result["kind"] == "agent"
    assert result["data"]["answer"] == "Use BashOperator."
    audit = client.get("/api/ai/audit").json()
    assert audit[0]["feature"] == "ask"
    assert audit[0]["tools"]
    assert audit[0]["outcome"] == "ok"
    assert audit[0]["llm_source"] == "central"


def test_explain_code_stream_and_error_event(tmp_path: Path) -> None:
    client, _ = make(tmp_path, FakeLLM(["It prints hello world."]))
    resp = client.post("/api/ai/explain-code/stream", json={"code": "echo hi", "language": "bash"})
    events = frames(resp)
    assert "".join(e["text"] for e in events if e["type"] == "token") == "It prints hello world."
    assert events[-2]["data"]["explanation"] == "It prints hello world."
    failing, _ = make(tmp_path / "f", FakeLLM([]))
    bad = frames(failing.post("/api/ai/explain-code/stream", json={"code": "x"}))
    assert bad[-2]["type"] == "error"
    assert bad[-2]["status"] == 502
    # Not configured: a plain HTTP error before the stream starts.
    none, _ = make(tmp_path / "n")
    assert none.post("/api/ai/explain-code/stream", json={"code": "x"}).status_code == 503


def test_write_stream_and_plain(tmp_path: Path) -> None:
    llm = FakeLLM(
        [{"code": "return 1", "explanation": "e", "requirements": [], "connections": []}] * 2
    )
    client, _ = make(tmp_path, llm)
    events = frames(
        client.post(
            "/api/ai/write/stream",
            json={"kind": "task_body", "description": "return one", "research": False},
        )
    )
    stages = [e["stage"] for e in events if e["type"] == "status"]
    assert stages[:3] == ["environment", "write", "validate"]
    assert events[-2]["kind"] == "writer"
    assert events[-2]["data"]["ok"] is True
    plain = client.post(
        "/api/ai/write", json={"kind": "task_body", "description": "x", "research": False}
    )
    assert plain.status_code == 200
    assert plain.json()["code"] == "return 1"
    info = client.get("/api/ai/writers").json()
    assert "operator_config" in info["kinds"]
    assert info["environment"]["airflow_version"]


def test_per_user_model_mode(tmp_path: Path) -> None:
    client, _services = make(
        tmp_path,
        llm_mode="per_user",
        user_llm=UserLlmSettings(allowed_providers=["anthropic", "openai"]),
    )
    status = client.get("/api/ai/status").json()
    assert status["configured"] is False
    assert status["llm_mode"] == "per_user"
    assert client.post("/api/ai/describe", json={"spec": {"dag_id": "x"}}).status_code == 428
    state = client.get("/api/ai/me/model").json()
    assert state["settings"]["configured"] is False
    assert state["policy"]["providers"] == ["anthropic", "openai"]
    bad = client.put("/api/ai/me/model", json={"provider": "gemini", "api_key": "k" * 20})
    assert bad.status_code == 422
    saved = client.put(
        "/api/ai/me/model",
        json={
            "provider": "anthropic",
            "model": "claude-haiku-5-5",
            "api_key": "sk-ant-user-1234567890",
        },
    ).json()
    assert saved["settings"]["api_key_set"] is True
    assert saved["settings"]["api_key_last4"] == "7890"
    assert "sk-ant-user" not in json.dumps(saved)
    status = client.get("/api/ai/status").json()
    assert status["configured"]
    assert status["llm_source"] == "user"
    assert status["model"] == "claude-haiku-5-5"
    assert status["user_model_configured"]
    assert client.delete("/api/ai/me/model").json()["settings"]["configured"] is False
    central, _ = make(tmp_path / "c", FakeLLM())
    assert central.put("/api/ai/me/model", json={"provider": "anthropic"}).status_code == 409


def test_knowledge_admin_api(tmp_path: Path) -> None:
    client, _ = make(tmp_path, FakeLLM())
    docs = {d["id"]: d for d in client.get("/api/ai/knowledge").json()}
    assert docs["airflow3-best-practices"]["origin"] == "builtin"
    put = client.put(
        "/api/ai/knowledge/airflow3-best-practices", json={"content": "# Ours\nRetries: 5."}
    )
    assert put.status_code == 200
    assert put.json()["origin"] == "overridden"
    got = client.get("/api/ai/knowledge/airflow3-best-practices").json()
    assert got["content"].startswith("# Ours")
    assert "Airflow 3" in got["default_content"]
    hits = client.get("/api/ai/search", params={"q": "Retries 5 ours", "kinds": "doc"}).json()
    assert hits[0]["title"].startswith("Ours")
    off = client.post("/api/ai/knowledge/dagspec-authoring/enabled", json={"enabled": False})
    assert off.json()["enabled"] is False
    reset = client.delete("/api/ai/knowledge/airflow3-best-practices").json()
    assert reset["origin"] == "builtin"
    assert client.put("/api/ai/knowledge/..%2Fx", json={"content": "x"}).status_code in (404, 422)
    assert client.get("/api/ai/knowledge/missing").status_code == 404
    prompts = client.get("/api/ai/prompts").json()
    assert any(p["name"] == "WRITER_BASH_SYSTEM" for p in prompts)


def test_admin_endpoints_need_admin_role(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from autoair.api.routers import ai

    services = build_services(Settings(dags_folder=tmp_path, subfolder=""), AuthBackend())
    api = FastAPI()
    api.state.services = services
    api.include_router(ai.router)  # no IAM principal, no permission flags: viewer
    client = TestClient(api)
    assert client.get("/ai/knowledge").status_code == 403
    assert client.get("/ai/audit").status_code == 403
    assert client.get("/ai/status").json()["role"] == "viewer"


# --------------------------------------------------------------------------- proxy mode


def test_proxy_mode_forwards_auth_identity_and_streams(tmp_path: Path) -> None:
    from autoair.ai.service import create_ai_service

    secret = SecretStr("shared-secret-0123456789")
    service_settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        ai=AiSettings(service=AiServiceSettings(shared_secret=secret, verify_tokens=True)),
    )
    seen_auth: list[str | None] = []

    async def verify(request: Request) -> None:
        seen_auth.append(request.headers.get("authorization"))
        from fastapi import HTTPException

        if request.headers.get("authorization") != "Bearer good":
            raise HTTPException(status_code=401, detail="bad token")

    from fastapi import Depends

    ai_services = build_services(
        service_settings, AuthBackend(app=[Depends(verify)], name="bearer")
    )
    ai_services.llm = FakeLLM(["streamed explanation"])
    ai_app = create_ai_service(ai_services)

    main_settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        ai=AiSettings(
            service_url="http://ai-service",
            service=AiServiceSettings(shared_secret=secret, proxy_mcp=True),
        ),
    )
    main_services = build_services(main_settings, AuthBackend())
    main_services.ai_proxy_transport = httpx2.ASGITransport(app=ai_app)  # type: ignore[attr-defined]
    client = TestClient(create_app(main_services))

    status = client.get("/api/ai/status", headers={"Authorization": "Bearer good"})
    assert status.status_code == 200, status.text
    assert status.json()["configured"] is True
    assert status.json()["role"] == "admin"  # signed by the proxy from the main server's IAM
    stream = client.post(
        "/api/ai/explain-code/stream",
        json={"code": "echo hi"},
        headers={"Authorization": "Bearer good"},
    )
    events = frames(stream)
    assert "".join(e["text"] for e in events if e["type"] == "token") == "streamed explanation"
    # Signed requests are trusted without re-verifying the token...
    assert seen_auth == []
    # ...direct, unsigned requests to the service must carry a valid token.
    direct = TestClient(ai_app)
    assert direct.get("/api/ai/status").status_code == 401
    assert direct.get("/api/ai/status", headers={"Authorization": "Bearer good"}).status_code == 200
    forged = direct.get(
        "/api/ai/status",
        headers={"x-autoair-ai-identity": "e30=", "x-autoair-ai-signature": "00"},
    )
    assert forged.status_code == 401
    assert direct.get("/health").json()["service"] == "ai"
    # MCP proxied to the AI service
    mcp = client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": "Bearer good",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert mcp.status_code == 200, mcp.text
    names = {t["name"] for t in mcp.json()["result"]["tools"]}
    assert {"search_installed_docs", "read_source", "write_code"} <= names


def test_proxy_unreachable_is_502(tmp_path: Path) -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    settings = Settings(
        dags_folder=tmp_path, subfolder="", ai=AiSettings(service_url="http://ai-down")
    )
    services = build_services(settings, AuthBackend())
    services.ai_proxy_transport = httpx2.MockTransport(down)  # type: ignore[attr-defined]
    client = TestClient(create_app(services))
    assert client.get("/api/ai/status").status_code == 502


@pytest.mark.parametrize(
    ("default_roles", "status", "role"),
    [
        ([], 403, None),
        (["Viewer"], 403, None),
        (["Author"], 200, "editor"),
        (["Admin"], 200, "admin"),
    ],
)
def test_direct_ai_service_calls_go_through_iam(
    tmp_path: Path, default_roles: list[str], status: int, role: str | None
) -> None:
    """Unsigned calls to a separate AI service get the same IAM principal as the main app
    (no implicit admin): ``ai:use`` is required and the AI role follows the permissions."""
    from fastapi import Depends

    from autoair.ai.service import create_ai_service
    from autoair.iam.config import IamSettings

    async def verify(request: Request) -> None:
        from fastapi import HTTPException

        if request.headers.get("authorization") != "Bearer good":
            raise HTTPException(status_code=401, detail="bad token")

    settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        ai=AiSettings(service=AiServiceSettings(verify_tokens=True)),
        iam=IamSettings(strategies=["role_mapping"], default_roles=default_roles),
    )
    services = build_services(settings, AuthBackend(app=[Depends(verify)], name="bearer"))
    services.llm = FakeLLM(["x"])
    client = TestClient(create_ai_service(services))
    resp = client.get("/api/ai/status", headers={"Authorization": "Bearer good"})
    assert resp.status_code == status, resp.text
    if role:
        assert resp.json()["role"] == role
