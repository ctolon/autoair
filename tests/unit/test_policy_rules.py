"""Declarative policy rules (policy.rules) and the optional OPA decision (policy.opa_url)."""

from __future__ import annotations

import json

import httpx2
import pytest
from pydantic import ValidationError

from autoair.policy_rules import OpaOptions, PolicyRule
from autoair.sections import PolicyRules
from autoair.spec import DagSpec
from autoair.validation import Policy, policy_from_settings, validate_spec
from autoair.validation.opa import clear_cache, decision_problems, opa_problems
from autoair.validation.policy_rules import evaluate_rules

BASH = "airflow.providers.standard.operators.bash.BashOperator"


def _spec(simple_spec_data, **dag) -> DagSpec:
    return DagSpec.model_validate({**simple_spec_data, **dag})


def _rules(*raw) -> list[PolicyRule]:
    return PolicyRules.model_validate({"rules": list(raw)}).rules


def test_forbid_operator_only_in_matching_environment(simple_spec_data):
    spec = _spec(simple_spec_data)
    rules = _rules(
        {
            "id": "no-bash-prod",
            "message": "BashOperator is forbidden in prod",
            "match": {"environments": ["prod*"], "operators": ["BashOperator"]},
            "check": "forbid",
        }
    )
    assert evaluate_rules(spec, rules, None) == []  # environment unknown: rule does not apply
    assert evaluate_rules(spec, rules, "dev") == []
    [problem] = evaluate_rules(spec, rules, "prod-eu")
    assert problem.code == "policy_rule"
    assert problem.severity == "error"
    assert problem.node_id == "b"
    assert "BashOperator is forbidden in prod" in problem.message
    assert "no-bash-prod" in problem.message


def test_assert_alias_task_fields_and_default_args(simple_spec_data):
    spec = _spec(simple_spec_data, default_args={"retries": 1})
    rules = _rules(
        {"id": "retries", "assert": {"type": "min_retries", "value": 2}, "severity": "warning"},
        {"id": "owner", "check": "require_owner"},
        {
            "id": "pool",
            "match": {"operators": ["airflow.providers.standard.*"]},
            "check": {"type": "task_field", "field": "retries", "max": 0},
        },
    )
    problems = evaluate_rules(spec, rules)
    by_rule = {}
    for p in problems:
        by_rule.setdefault(p.message.rsplit("policy rule ", 1)[1].rstrip("]"), []).append(p)
    assert {p.node_id for p in by_rule["retries"]} == {"a", "b"}
    assert all(p.severity == "warning" for p in by_rule["retries"])
    assert {p.field for p in by_rule["owner"]} == {"owner"}
    assert len(by_rule["pool"]) == 2  # default_args.retries=1 > 0

    owned = _spec(simple_spec_data, default_args={"owner": "data", "retries": 3})
    assert evaluate_rules(owned, rules[:2]) == []


def test_param_match_and_value_checks(simple_spec_data):
    spec = _spec(simple_spec_data)
    rules = _rules(
        {
            "id": "no-rm",
            "match": {"params": [{"name": "bash_command", "pattern": r"rm\s+-rf"}]},
            "check": "forbid",
        },
        {
            "id": "echo-only",
            "match": {"operators": ["BashOperator"]},
            "check": {"type": "param", "condition": {"name": "bash_command", "pattern": "^echo"}},
        },
        {
            "id": "needs-cwd",
            "match": {"operators": ["BashOperator"]},
            "check": {"type": "require_param", "name": "cwd"},
        },
    )
    problems = evaluate_rules(spec, rules)
    assert [p.message.split("[")[1] for p in problems] == ["policy rule needs-cwd]"]
    simple_spec_data["nodes"][1]["params"]["bash_command"] = "rm -rf /tmp/x"
    codes = [p.message for p in evaluate_rules(_spec(simple_spec_data), rules)]
    assert any("no-rm" in m for m in codes)
    assert any("echo-only" in m for m in codes)


def test_dag_level_checks_and_selectors(simple_spec_data):
    spec = _spec(
        simple_spec_data,
        schedule={"type": "preset", "preset": "@daily"},
        team="data",
        tags=["etl"],
        catchup=True,
    )
    rules = _rules(
        {"id": "daily", "match": {"schedule": ["0 0 * * *"]}, "check": {"type": "catchup"}},
        {
            "id": "team-tag",
            "match": {"teams": ["da*"]},
            "check": {"type": "tags_include", "team": True},
        },
        {"id": "desc", "match": {"tags": ["etl"]}, "check": "require_description"},
        {"id": "small", "check": {"type": "max_tasks", "value": 1}},
        {
            "id": "runs",
            "check": {"type": "dag_field", "field": "max_active_runs", "present": True},
        },
        {"id": "other-dag", "match": {"dag_ids": ["finance_*"]}, "check": "forbid"},
        {"id": "off", "enabled": False, "check": "forbid"},
    )
    fields = {p.message.rsplit("rule ", 1)[1][:-1]: p.field for p in evaluate_rules(spec, rules)}
    assert fields == {
        "daily": "catchup",
        "team-tag": "tags",
        "desc": "description",
        "small": None,
        "runs": "max_active_runs",
    }


def test_rules_are_validated():
    with pytest.raises(ValidationError, match="duplicate policy rule id"):
        _rules({"id": "a", "check": "forbid"}, {"id": "a", "check": "forbid"})
    with pytest.raises(ValidationError):
        _rules({"id": "a", "check": {"type": "no_such_check"}})
    with pytest.raises(ValidationError, match="regular expression"):
        _rules({"id": "a", "match": {"params": [{"name": "x", "pattern": "("}]}, "check": "forbid"})


def test_rules_reach_validation_and_block_on_error(simple_spec_data):
    settings_like = PolicyRules.model_validate(
        {"rules": [{"id": "bash", "match": {"operators": ["BashOperator"]}, "check": "forbid"}]}
    )
    policy = Policy(rules=tuple(settings_like.rules))
    report = validate_spec(_spec(simple_spec_data), policy=policy)
    assert not report.ok
    assert any(p.code == "policy_rule" for p in report.problems)


def test_policy_from_settings_carries_rules_and_opa():
    from autoair.settings import Settings

    s = Settings.model_validate(
        {
            "policy": {
                "rules": [{"id": "x", "check": "forbid"}],
                "opa_url": "http://opa:8181/v1/data/autoair/violations",
                "opa_fail_mode": "closed",
            }
        }
    )
    policy = policy_from_settings(s)
    assert [r.id for r in policy.rules] == ["x"]
    assert policy.opa is not None
    assert policy.opa.fail_mode == "closed"


# --------------------------------------------------------------------------- OPA


def test_decision_shapes():
    assert decision_problems(None) == []
    assert decision_problems(True) == []
    assert decision_problems(False)[0].severity == "error"
    [a, b] = decision_problems(
        ["no", {"message": "careful", "severity": "warning", "node_id": "b"}]
    )
    assert (a.severity, a.message) == ("error", "no")
    assert (b.severity, b.node_id) == ("warning", "b")
    out = decision_problems({"deny": ["bad"], "warn": [{"msg": "meh", "field": "tags"}]})
    assert [(p.severity, p.message, p.field) for p in out] == [
        ("error", "bad", None),
        ("warning", "meh", "tags"),
    ]
    assert decision_problems({"allow": False})[0].message == "denied by the OPA policy"
    assert decision_problems("weird")[0].severity == "warning"


def test_opa_receives_spec_and_context(simple_spec_data):
    clear_cache()
    seen: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            {"body": json.loads(request.content), "auth": request.headers.get("authorization")}
        )
        return httpx2.Response(
            200, json={"result": {"deny": [{"message": "nope", "node_id": "a"}]}}
        )

    options = OpaOptions(url="http://opa/v1/data/x", token="t0k", cache_seconds=60)
    spec = _spec(simple_spec_data)
    transport = httpx2.MockTransport(handler)
    first = opa_problems(spec, {"environment": "prod"}, options, transport=transport)
    again = opa_problems(spec, {"environment": "prod"}, options, transport=transport)
    assert [(p.code, p.message, p.node_id) for p in first] == [("policy_opa", "nope", "a")]
    assert again == first
    assert len(seen) == 1  # cached
    assert seen[0]["auth"] == "Bearer t0k"
    assert seen[0]["body"]["input"]["spec"]["dag_id"] == "simple"
    assert seen[0]["body"]["input"]["context"] == {"environment": "prod"}


@pytest.mark.parametrize(("mode", "severity"), [("open", "warning"), ("closed", "error")])
@pytest.mark.parametrize(
    "failure",
    [
        lambda r: httpx2.Response(500, text="boom"),
        lambda r: httpx2.Response(200, text="not json"),
        lambda r: (_ for _ in ()).throw(httpx2.ConnectError("down", request=r)),
        lambda r: (_ for _ in ()).throw(httpx2.ReadTimeout("slow", request=r)),
    ],
)
def test_opa_failure_modes(simple_spec_data, mode, severity, failure):
    clear_cache()
    options = OpaOptions(url="http://opa/v1/data/x", fail_mode=mode)
    [problem] = opa_problems(
        _spec(simple_spec_data), {}, options, transport=httpx2.MockTransport(failure)
    )
    assert problem.severity == severity
    assert problem.code == "policy_opa"
    assert ("fail closed" if mode == "closed" else "fail open") in problem.message


def test_opa_options_require_http():
    with pytest.raises(ValidationError):
        OpaOptions(url="file:///etc/passwd")
