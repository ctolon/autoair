"""AI router review fixes: input guardrails on every endpoint, per-request model
allow-lists, Python-disabled -> 403, background audit writes with retention."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from autoair.ai.audit_log import AiAuditLog, AiAuditRecord, now_iso
from autoair.ai.config import AiAuditSettings, AiSettings, GuardrailSettings, UserLlmSettings
from autoair.ai.events import parse_sse_frames
from autoair.ai.guardrails import GuardrailPipeline, GuardrailResult, GuardrailViolation
from autoair.ai.testing import FakeLLM
from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.settings import Settings

pytestmark = pytest.mark.airflow


def make(
    tmp_path: Path, llm: Any = None, features: Any = None, **ai: Any
) -> tuple[TestClient, Any]:
    extra: dict[str, Any] = {"features": features} if features else {}
    settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        ai=AiSettings(**ai),
        import_check_on_deploy=False,
        **extra,
    )
    services = build_services(settings, AuthBackend())
    if llm is not None:
        services.llm = llm
    return TestClient(create_app(services)), services


def test_max_input_chars_is_enforced_on_plain_and_stream_endpoints(tmp_path: Path) -> None:
    llm = FakeLLM(["fine"] * 4)
    client, _ = make(tmp_path, llm, guardrails=GuardrailSettings(max_input_chars=1_000))
    big = {"code": "x" * 1_500, "language": "text"}
    plain = client.post("/api/ai/explain-code", json=big)
    assert plain.status_code == 422
    assert "size" in plain.json()["detail"]
    stream = client.post("/api/ai/explain-code/stream", json=big)
    assert stream.status_code == 422
    assert client.post("/api/ai/ask", json={"question": "q" * 1_500}).status_code == 422
    ok = client.post("/api/ai/explain-code", json={"code": "echo hi", "language": "bash"})
    assert ok.status_code == 200


class _Deny:
    name = "acme"

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    async def check(self, text: str, direction: str) -> GuardrailResult:
        self.seen.append((direction, text))
        if "forbidden" in text:
            raise GuardrailViolation(self.name, f"policy {direction}")
        return GuardrailResult(text)


def test_external_guardrails_check_input_and_final_output(tmp_path: Path) -> None:
    llm = FakeLLM(["a forbidden answer", "a clean answer"])
    client, services = make(tmp_path, llm)
    guard = _Deny()
    services._autoair_guardrail_pipeline = GuardrailPipeline(GuardrailSettings(), extra=[guard])
    blocked_in = client.post("/api/ai/explain-code", json={"code": "forbidden code"})
    assert blocked_in.status_code == 422
    assert "policy input" in blocked_in.json()["detail"]
    blocked_out = client.post("/api/ai/explain-code", json={"code": "echo hi"})
    assert blocked_out.status_code == 422
    assert "policy output" in blocked_out.json()["detail"]
    ok = client.post("/api/ai/explain-code", json={"code": "echo hi"})
    assert ok.json()["explanation"] == "a clean answer"
    assert ("output", "a clean answer") in guard.seen


def test_request_model_must_be_on_the_user_allow_list(tmp_path: Path) -> None:
    client, _ = make(
        tmp_path,
        llm_mode="per_user",
        user_llm=UserLlmSettings(
            allowed_providers=["anthropic"], allowed_models=["claude-haiku-*"]
        ),
    )
    saved = client.put(
        "/api/ai/me/model",
        json={"provider": "anthropic", "model": "claude-haiku-5-5", "api_key": "sk-ant-u-123456"},
    )
    assert saved.status_code == 200
    spec = {"spec": {"dag_id": "x"}}
    denied = client.post("/api/ai/describe", json={**spec, "model": "claude-opus-5-5"})
    assert denied.status_code == 422
    assert "not allowed" in denied.json()["detail"]
    stream = client.post(
        "/api/ai/explain-code/stream", json={"code": "x", "model": "gpt-5-expensive"}
    )
    assert stream.status_code == 422


def test_request_model_must_be_on_the_central_list(tmp_path: Path) -> None:
    client, _ = make(tmp_path, FakeLLM(["ok"]), models=["fake-a"])
    bad = client.post("/api/ai/explain-code", json={"code": "x", "model": "other"})
    assert bad.status_code == 422


def test_python_disabled_writer_is_403(tmp_path: Path) -> None:
    client, _ = make(tmp_path, FakeLLM(), features={"python_code": False})
    body = {"kind": "task_body", "description": "return one", "research": False}
    resp = client.post("/api/ai/write", json=body)
    assert resp.status_code == 403
    events = parse_sse_frames(client.post("/api/ai/write/stream", json=body).text)
    error = next(e for e in events if e["type"] == "error")
    assert error["status"] == 403


def test_audit_records_are_written_in_the_background(tmp_path: Path) -> None:
    client, _ = make(tmp_path, FakeLLM(["fine"]))
    assert client.post("/api/ai/explain-code", json={"code": "echo"}).status_code == 200
    audit = client.get("/api/ai/audit").json()
    assert audit[0]["feature"] == "explain_code"


def test_audit_retention_by_age_and_size(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    log = AiAuditLog(AiAuditSettings(retention_days=7, max_total_mb=1), str(root))
    today = datetime.date(2026, 10, 15)
    for offset in (0, 1, 3, 10, 30):
        day = (today - datetime.timedelta(days=offset)).isoformat()
        (root / day).mkdir(parents=True)
        (root / day / "x.jsonl").write_text("{}\n")
    removed = log.prune(today.isoformat())
    assert sorted(Path(p).name for p in removed) == ["2026-09-15", "2026-10-05"]
    assert sorted(p.name for p in root.iterdir()) == ["2026-10-12", "2026-10-14", "2026-10-15"]
    # size cap: the oldest days go first, the newest day always stays
    (root / "2026-10-12" / "big.jsonl").write_bytes(b"x" * (1024 * 1024 + 10))
    removed = log.prune(today.isoformat())
    assert [Path(p).name for p in removed] == ["2026-10-12"]
    # and records still land (pruning runs on day roll-over, in the writer thread)
    log.submit(AiAuditRecord(timestamp=now_iso(), feature="f"))
    log.flush()
    assert any(json.loads(line).get("feature") == "f" for line in _lines(root))


def _lines(root: Path) -> list[str]:
    return [line for f in root.rglob("*.jsonl") for line in f.read_text().splitlines() if line]


def test_model_test_uses_the_unsaved_form(tmp_path: Path) -> None:
    client, _ = make(
        tmp_path,
        llm_mode="per_user",
        user_llm=UserLlmSettings(allowed_providers=["anthropic"], allowed_models=["claude-*"]),
    )
    bad = client.post("/api/ai/me/model/test", json={"provider": "gemini", "api_key": "k" * 20})
    assert bad.status_code == 422
    bad_model = client.post(
        "/api/ai/me/model/test", json={"provider": "anthropic", "model": "gpt-5"}
    )
    assert bad_model.status_code == 422
    # the form is never saved by a test
    assert client.get("/api/ai/me/model").json()["settings"]["configured"] is False
    # no body: the saved settings (none here)
    assert client.post("/api/ai/me/model/test").status_code == 428
