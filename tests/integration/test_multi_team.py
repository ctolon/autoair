from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.gateway import PoolInfo, for_team
from autoair.settings import Settings
from autoair.storage.config import StorageSettings

pytestmark = pytest.mark.airflow
EMPTY = "airflow.providers.standard.operators.empty.EmptyOperator"


def spec(dag_id: str, team: str | None = None) -> dict[str, object]:
    return {
        "dag_id": dag_id,
        "team": team,
        "nodes": [{"kind": "operator", "id": "a", "task_id": "a", "operator": EMPTY}],
    }


def test_team_targets_and_prefix_policy(tmp_path):
    team_dir = tmp_path / "team-a-bundle"
    settings = Settings(
        dags_folder=tmp_path / "shared",
        subfolder="",
        import_check_on_deploy=False,
        ai={"enabled": False},
        features={"mcp": False},
        team_dag_id_prefix=True,
        team_storage={"team_a": StorageSettings(mode="local", local_path=team_dir)},
    )
    client = TestClient(create_app(build_services(settings, AuthBackend())))

    bad = client.put("/api/specs/orders", json={"spec": spec("orders", "team_a")}).json()
    assert bad["deployed"] is False
    assert any("team prefix" in p["message"] for p in bad["report"]["problems"])

    ok = client.put(
        "/api/specs/team_a_orders", json={"spec": spec("team_a_orders", "team_a")}
    ).json()
    assert ok["deployed"], ok
    assert (team_dir / "team_a_orders.py").exists()
    assert client.put("/api/specs/shared_dag", json={"spec": spec("shared_dag")}).json()["deployed"]
    assert (tmp_path / "shared" / "shared_dag.py").exists()

    listed = [s["dag_id"] for s in client.get("/api/specs").json()]
    assert listed == ["shared_dag", "team_a_orders"]
    assert client.get("/api/specs/team_a_orders").json()["spec"]["team"] == "team_a"
    response = client.delete("/api/specs/team_a_orders")
    assert response.json() == {"deleted": True}
    assert not (team_dir / "team_a_orders.py").exists()


def test_picker_team_filter():
    pools = [
        PoolInfo(name="g"),
        PoolInfo(name="a", team_name="team_a"),
        PoolInfo(name="b", team_name="team_b"),
    ]
    assert [p.name for p in for_team(pools, "team_a")] == ["g", "a"]
    assert len(for_team(pools, None)) == 3


def test_airflow_without_multi_team_returns_only_global_objects():
    """Airflow < 3.2.0 (or [core] multi_team off) sends no team_name: everything is global."""
    pools = [PoolInfo.model_validate({"name": "g", "slots": 1}), PoolInfo(name="h")]
    assert [p.name for p in for_team(pools, "team_a")] == ["g", "h"]


def test_documented_multi_team_minimum_matches_airflow():
    """website/docs/user-guide/multi-team.mdx states 3.2.0: Airflow's own config says so."""
    from pathlib import Path

    import airflow

    config = Path(airflow.__file__).parent / "config_templates" / "config.yml"
    text = config.read_text(encoding="utf-8")
    marker = "\n    multi_team:\n"
    if marker not in text:  # CI also runs Airflow 3.0 / 3.1, which have no multi-team
        from packaging.version import Version

        assert Version(airflow.__version__) < Version("3.2.0")
        return
    block = text[text.index(marker) :][:1200]
    assert "version_added: 3.2.0" in block
