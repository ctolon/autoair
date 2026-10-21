"""policy.rules reach /api/validate with the selected environment."""

from __future__ import annotations

from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.settings import Settings


def test_validate_applies_environment_rules(tmp_path, simple_spec_data):
    settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        policy={
            "rules": [
                {
                    "id": "no-bash-prod",
                    "match": {"environments": ["prod"], "operators": ["BashOperator"]},
                    "check": "forbid",
                },
                {
                    "id": "retries",
                    "severity": "warning",
                    "check": {"type": "min_retries", "value": 1},
                },
            ]
        },
    )
    client = TestClient(create_app(build_services(settings, AuthBackend())))
    spec = {**simple_spec_data, "environments": {"prod": {}, "dev": {}}}

    def rule_problems(env: str | None) -> list[tuple[str, str | None]]:
        params = {"environment": env} if env else {}
        resp = client.post("/api/validate", json=spec, params=params)
        assert resp.status_code == 200, resp.text
        return sorted(
            (p["severity"], p["node_id"])
            for p in resp.json()["problems"]
            if p["code"] == "policy_rule"
        )

    assert rule_problems("dev") == [("warning", "a"), ("warning", "b")]
    assert rule_problems("prod") == [("error", "b"), ("warning", "a"), ("warning", "b")]
