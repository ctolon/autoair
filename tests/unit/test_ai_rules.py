"""Deterministic leak detection, review rules, JSON patches and run-history tuning."""

from __future__ import annotations

from typing import Any

import pytest

from autoair.ai.findings import (
    Finding,
    PatchError,
    PatchOp,
    apply_findings,
    apply_patch,
    resolve_pointer,
    spec_document,
)
from autoair.ai.rules import mask, review_rules, scan_leaks
from autoair.ai.tuning import RunRecord, history_stats, suggested_timeout, tuning_findings
from autoair.catalog import OperatorCatalog
from autoair.gateway import DagRunInfo, TaskInstanceInfo
from autoair.registry import default_registry
from autoair.spec import DagSpec

pytestmark = pytest.mark.airflow

BASH = "airflow.providers.standard.operators.bash.BashOperator"
FILE_SENSOR = "airflow.providers.standard.sensors.filesystem.FileSensor"


@pytest.fixture(scope="module")
def catalog() -> OperatorCatalog:
    return OperatorCatalog()


def spec(*nodes: dict[str, Any], **dag: Any) -> DagSpec:
    return DagSpec.model_validate({"dag_id": "t", "nodes": list(nodes), **dag})


def bash(node_id: str, command: str, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "operator",
        "id": node_id,
        "task_id": node_id,
        "operator": BASH,
        "params": {"bash_command": command},
        **extra,
    }


def py(node_id: str, code: str) -> dict[str, Any]:
    return {
        "kind": "python_task",
        "id": node_id,
        "task_id": node_id,
        "function_name": node_id,
        "code": code,
    }


def rules_of(findings: list[Finding]) -> dict[str, Finding]:
    return {f.rule or "": f for f in findings}


# --------------------------------------------------------------------------- leaks


def test_detects_hard_coded_secrets_with_masked_evidence() -> None:
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    found = rules_of(
        scan_leaks(
            spec(
                bash("upload", f"AWS_ACCESS_KEY_ID={key} aws s3 cp a b"),
                bash("db", "psql postgresql://admin:hunter22@db.internal/prod -c 'select 1'"),
                py("call", 'token = "ghp_' + "a" * 36 + '"\nreturn 1'),
            )
        )
    )
    aws = found["secret:aws-access-key"]
    assert aws.severity == "critical"
    assert (aws.node_id, aws.field) == ("upload", "bash_command")
    assert aws.path == "/nodes/@upload/params/bash_command"
    assert key not in (aws.evidence or "")
    assert found["secret:credential-url"].node_id == "db"
    assert found["secret:github-token"].field == "code"


def test_ignores_templates_placeholders_and_conn_ids() -> None:
    found = scan_leaks(
        spec(
            bash("ok", "curl --max-time 10 https://{{ conn.api.host }}/x"),
            {
                "kind": "operator",
                "id": "op",
                "task_id": "op",
                "operator": BASH,
                "params": {"bash_command": "echo hi", "env": {"API_TOKEN_CONN_ID": "my_api"}},
            },
            py("p", 'password = "changeme"\nreturn None'),
        )
    )
    assert not [f for f in found if f.category == "secret"]


def test_detects_credential_exposure_in_logs_templates_and_xcom() -> None:
    found = rules_of(
        scan_leaks(
            spec(
                bash("render", "mysql -p{{ conn.mysql_default.password }} -e 'select 1'"),
                bash("echo", "set -x\necho $DB_PASSWORD"),
                py(
                    "leak",
                    "from airflow.sdk import Connection\n"
                    "conn = Connection.get('pg')\n"
                    "print('pw', conn.password)\n"
                    "return {'token': api_token}",
                ),
            )
        )
    )
    assert found["rendered-secret"].node_id == "render"
    assert found["shell-echo-secret"].node_id == "echo"
    assert found["shell-xtrace"].node_id == "echo"
    assert found["log-secret"].node_id == "leak"
    assert found["xcom-secret"].severity == "high"


def test_unbounded_resources(catalog: OperatorCatalog) -> None:
    sensor = {
        "kind": "operator",
        "id": "wait",
        "task_id": "wait",
        "operator": FILE_SENSOR,
        "params": {"filepath": "/data/x"},
    }
    found = rules_of(
        scan_leaks(
            spec(
                bash("fetch", "curl https://example.com/data"),
                py("loop", "import requests\nwhile True:\n    requests.get(url)\n"),
                sensor,
                schedule={"type": "preset", "preset": "@daily"},
                catchup=True,
            ),
            catalog,
        )
    )
    assert found["curl-no-timeout"].node_id == "fetch"
    assert found["http-no-timeout"].node_id == "loop"
    assert found["unbounded-loop"].node_id == "loop"
    timeout = found["no-execution-timeout"]
    assert timeout.patch == [
        PatchOp(op="add", path="/default_args/execution_timeout_seconds", value=3600)
    ]
    assert found["catchup-unbounded"].patch[0].path == "/max_active_runs"
    assert found["sensor-timeout"].patch[0].path == "/nodes/@wait/params/timeout"
    assert found["sensor-deferrable"].field == "deferrable"


def test_mask() -> None:
    assert mask("supersecretvalue") == "supe…******"
    assert mask("abc") == "***"


# --------------------------------------------------------------------------- review


def test_review_rules(catalog: OperatorCatalog) -> None:
    found = rules_of(
        review_rules(
            spec(
                bash("load", 'psql -c "delete from t where d = NOW()" {{ execution_date }}'),
                py("now", "import datetime\nreturn datetime.datetime.now().isoformat()"),
                py("db", "from airflow.settings import Session\nreturn 1"),
                py("legacy", "from airflow.operators.bash import BashOperator\nreturn 1"),
                py("models", "from airflow.models import Variable\nreturn Variable.get('x')"),
                schedule={"type": "cron", "expression": "0 6 * * *"},
            ),
            catalog,
        )
    )
    retries = found["no-retries"]
    assert [p.path for p in retries.patch] == [
        "/default_args/retries",
        "/default_args/retry_delay_seconds",
    ]
    assert found["airflow3-template"].severity == "high"
    assert found["airflow3-template"].node_id == "load"
    assert found["non-idempotent-time"].node_id == "load"
    assert found["non-idempotent-time-py"].node_id == "now"
    assert found["airflow3-db-access"].node_id == "db"
    assert found["airflow3-legacy-import"].node_id == "legacy"
    assert found["airflow3-sdk-import"].node_id == "models"
    assert {"no-owner", "no-tags", "no-docs", "no-deadline"} <= set(found)


def test_review_rules_clean_spec(catalog: OperatorCatalog) -> None:
    clean = spec(
        bash("a", "echo {{ ds }}"),
        default_args={"owner": "data", "retries": 2, "execution_timeout_seconds": 600},
        tags=["x"],
        description="d",
    )
    assert review_rules(clean, catalog) == []
    assert scan_leaks(clean, catalog) == []


def test_dag_run_conf_is_not_flagged(catalog: OperatorCatalog) -> None:
    found = review_rules(spec(bash("a", "echo {{ dag_run.conf['x'] }} {{ ds }}")), catalog)
    assert "airflow3-template" not in rules_of(found)


# --------------------------------------------------------------------------- patches


def test_apply_patch_with_node_id_pointers() -> None:
    doc = spec_document(spec(bash("a", "echo a"), bash("b", "echo b")))
    out = apply_patch(
        doc,
        [
            PatchOp(op="add", path="/nodes/@b/retries", value=3),
            PatchOp(op="replace", path="/nodes/@a/params/bash_command", value="echo A"),
            PatchOp(op="add", path="/default_args/owner", value="me"),
            PatchOp(op="add", path="/tags/-", value="new"),
            PatchOp(op="remove", path="/nodes/@a/params/bash_command"),
        ],
    )
    assert out["nodes"][1]["retries"] == 3
    assert "bash_command" not in out["nodes"][0]["params"]
    assert out["default_args"]["owner"] == "me"
    assert out["tags"] == ["new"]
    assert doc["nodes"][1].get("retries") is None  # input untouched
    assert resolve_pointer(doc, "/nodes/@b/params") == "/nodes/1/params"
    with pytest.raises(PatchError, match="no node with id"):
        apply_patch(doc, [PatchOp(op="add", path="/nodes/@zzz/retries", value=1)])
    with pytest.raises(PatchError):
        apply_patch(doc, [PatchOp(op="remove", path="/nope")])


def test_apply_findings_skips_bad_patches(catalog: OperatorCatalog) -> None:
    base = spec(bash("a", "echo a"))
    good = Finding(
        id="g",
        severity="low",
        category="reliability",
        title="retries",
        message="",
        patch=[PatchOp(op="add", path="/nodes/@a/retries", value=2)],
    )
    schema_breaking = good.model_copy(
        update={
            "id": "bad",
            "patch": [PatchOp(op="add", path="/nodes/@a/retries", value=-5)],
        }
    )
    missing = good.model_copy(
        update={"id": "gone", "patch": [PatchOp(op="add", path="/nodes/@x/retries", value=1)]}
    )
    result = apply_findings(
        base, [good, schema_breaking, missing], catalog=catalog, registry=default_registry()
    )
    assert result.applied == ["g"]
    assert result.skipped == ["bad", "gone"]
    assert result.ok
    assert result.spec is not None
    assert result.spec.node_map()["a"].retries == 2  # type: ignore[union-attr]
    only = apply_findings(base, [good], catalog=catalog, registry=default_registry(), only=[])
    assert only.applied == []


# --------------------------------------------------------------------------- tuning


def _run(i: int, minutes: int, states: dict[str, tuple[str, int, float]]) -> RunRecord:
    start = f"2026-01-{i + 1:02d}T00:00:00+00:00"
    end = f"2026-01-{i + 1:02d}T00:{minutes:02d}:00+00:00"
    return RunRecord(
        run=DagRunInfo(dag_run_id=f"r{i}", state="success", start_date=start, end_date=end),
        tasks=[
            TaskInstanceInfo(task_id=t, state=s, try_number=tries, duration=d)
            for t, (s, tries, d) in states.items()
        ],
    )


def test_history_stats_and_tuning(catalog: OperatorCatalog) -> None:
    history = [
        _run(
            i,
            10 + i,
            {
                "extract": ("success", 2 if i in (1, 3) else 1, 100.0 + 10 * i),
                "load": ("success", 1, 600.0),
                "broken": ("failed", 1, 1.0),
            },
        )
        for i in range(6)
    ]
    stats = history_stats("t", history)
    assert stats.runs == 6
    assert stats.run_success_rate == 1.0
    extract = stats.task("extract")
    assert extract is not None
    assert extract.retried_then_succeeded == 2
    assert extract.p50_seconds == pytest.approx(125.0)
    assert extract.max_seconds == 150.0

    dag = spec(
        bash("extract", "echo e"),
        bash("load", "echo l", execution_timeout_seconds=300),
        bash("broken", "exit 1"),
    )
    found = {(f.rule, f.node_id): f for f in tuning_findings(dag, stats, catalog)}
    missing = found[("tune-timeout-missing", "extract")]
    assert missing.patch[0].path == "/nodes/@extract/execution_timeout_seconds"
    assert missing.patch[0].value == suggested_timeout(extract.p95_seconds or 0, 150.0)
    assert found[("tune-timeout-tight", "load")].severity == "high"
    assert found[("tune-retries-flaky", "extract")].patch[0].value == 3
    assert found[("tune-always-failing", "broken")].patch == []
    assert ("tune-dagrun-timeout", None) in found


def test_suggested_timeout() -> None:
    assert suggested_timeout(10, None) == 300
    assert suggested_timeout(400, 500) == 1200
