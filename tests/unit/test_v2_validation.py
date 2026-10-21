from __future__ import annotations

import pytest

from autoair.codegen import generate_source
from autoair.codegen.builder import effective_edges
from autoair.codegen.features import required_features
from autoair.spec import DagSpec
from autoair.validation import Policy, validate_spec
from autoair.validation.jinja import check_template

EMPTY = "airflow.providers.standard.operators.empty.EmptyOperator"
BASH = "airflow.providers.standard.operators.bash.BashOperator"


def op(node_id: str, **extra: object) -> dict[str, object]:
    return {"kind": "operator", "id": node_id, "task_id": node_id, "operator": EMPTY, **extra}


def codes(report, severity: str = "error") -> set[str]:
    return {p.code for p in report.problems if p.severity == severity}


def test_v1_documents_are_migrated():
    spec = DagSpec.model_validate({"spec_version": 1, "dag_id": "old"})
    assert spec.spec_version == 2


def test_newer_spec_version_is_rejected():
    with pytest.raises(ValueError, match="newer"):
        DagSpec.model_validate({"spec_version": 99, "dag_id": "x"})


def test_disabled_tasks_are_bridged():
    spec = DagSpec.model_validate(
        {
            "dag_id": "d",
            "nodes": [op("a"), op("b", disabled=True), op("c")],
            "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}],
        }
    )
    assert [(e.source, e.target) for e in effective_edges(spec)] == [("a", "c")]
    source = generate_source(spec)
    assert "t_b" not in source
    assert "t_a >> t_c" in source


def test_notes_are_not_tasks_and_go_to_docs():
    spec = DagSpec.model_validate(
        {
            "dag_id": "n",
            "nodes": [
                op("a"),
                {"kind": "note", "id": "note", "text": "hello docs", "include_in_docs": True},
            ],
        }
    )
    source = generate_source(spec)
    assert "hello docs" in source
    assert validate_spec(spec).ok


def test_note_edges_rejected():
    spec = DagSpec.model_validate(
        {
            "dag_id": "n",
            "nodes": [op("a"), {"kind": "note", "id": "x"}],
            "edges": [{"source": "a", "target": "x"}],
        }
    )
    assert "note_edge" in codes(validate_spec(spec))


def test_branch_targets_checked():
    spec = DagSpec.model_validate(
        {
            "dag_id": "b",
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "br",
                    "task_id": "br",
                    "function_name": "br",
                    "decorator": "branch",
                    "code": "return 'nope'",
                },
                op("x"),
            ],
            "edges": [{"source": "br", "target": "x"}],
        }
    )
    report = validate_spec(spec)
    assert "branch" in codes(report, "warning")
    assert "@task.branch(" in generate_source(spec)


def test_teardown_rules():
    spec = DagSpec.model_validate(
        {"dag_id": "t", "nodes": [op("td", is_teardown=True, trigger_rule="always")]}
    )
    report = validate_spec(spec)
    assert "teardown" in codes(report)


def test_version_gates():
    spec = DagSpec.model_validate(
        {
            "dag_id": "v",
            "nodes": [op("a", retry_policy={"rules": [{"exception": "builtins.ValueError"}]})],
        }
    )
    assert {f.key for f in required_features(spec)} == {"retry_policy"}
    report = validate_spec(spec, target_version=(3, 1, 0))
    assert "airflow_version" in codes(report)
    assert "airflow_version" not in codes(validate_spec(spec, target_version=(3, 3, 0)))


def test_policy_rules():
    spec = DagSpec.model_validate({"dag_id": "x", "nodes": [op("a")]})
    policy = Policy(
        require_owner=True,
        require_description=True,
        min_retries=1,
        required_tags=["team-a"],
        dag_id_prefix="team_a_",
        forbidden_operators=["*.empty.*"],
        max_tasks=0,
    )
    report = validate_spec(spec, policy=policy)
    messages = " ".join(p.message for p in report.problems if p.code == "policy")
    for needle in ("owner", "description", "retries", "team-a", "team_a_", "forbidden", "at most"):
        assert needle in messages


def test_jinja_checks():
    assert check_template("{{ params.limit }}", declared_params=["limit"]) == []
    issues = check_template("{{ params.nope }} {{ foo }}", declared_params=["limit"])
    assert {i.severity for i in issues} == {"error", "warning"}
    assert check_template("{{ ds", declared_params=[])[0].severity == "error"
    warn = check_template("{{ ds }}", time_available=False)
    assert "logical date" in warn[0].message


def test_templates_validated_in_spec():
    spec = DagSpec.model_validate(
        {
            "dag_id": "j",
            "params": {"n": {"default": 1}},
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "p",
                    "task_id": "p",
                    "function_name": "p",
                    "arguments": {"x": "{{ params.missing }}"},
                    "code": "return x",
                }
            ],
        }
    )
    report = validate_spec(spec)
    assert "template" in codes(report)


def test_environment_overlay():
    spec = DagSpec.model_validate(
        {
            "dag_id": "e",
            "schedule": {"type": "none"},
            "params": {"n": {"default": 1, "type": "integer"}},
            "environments": {
                "prod": {
                    "schedule": {"type": "preset", "preset": "@daily"},
                    "params_defaults": {"n": 5},
                }
            },
        }
    )
    prod = spec.with_environment("prod")
    assert prod.schedule.type == "preset"
    assert prod.params["n"].default == 5
    assert prod.params["n"].type == "integer"
    with pytest.raises(KeyError):
        spec.with_environment("nope")


def test_mapping_requires_exactly_one_mode():
    with pytest.raises(ValueError, match="exactly one"):
        DagSpec.model_validate({"dag_id": "m", "nodes": [op("a", mapping={})]})


def test_param_schema_kwargs():
    spec = DagSpec.model_validate(
        {
            "dag_id": "p",
            "params": {
                "n": {
                    "default": 3,
                    "type": "integer",
                    "minimum": 1,
                    "max_length": 4,
                    "section": "S",
                }
            },
        }
    )
    src = generate_source(spec)
    assert "Param(3, type='integer'" in src
    assert "maxLength=4" in src
    assert "section='S'" in src


@pytest.mark.parametrize(
    ("version", "module"),
    [((3, 0, 6), "airflow.timetables.trigger"), ((3, 3, 2), "airflow.sdk")],
)
def test_imports_follow_target_airflow_version(version, module):
    spec = DagSpec.model_validate(
        {"dag_id": "c", "schedule": {"type": "cron", "expression": "0 1 * * *", "timezone": "UTC"}}
    )
    source = generate_source(spec, airflow_version=version)
    assert f"from {module} import" in source
    assert "CronTriggerTimetable" in source


def test_hitl_params_become_param_objects():
    spec = DagSpec.model_validate(
        {
            "dag_id": "h",
            "nodes": [
                {
                    "kind": "operator",
                    "id": "a",
                    "task_id": "approve",
                    "operator": "airflow.providers.standard.operators.hitl.HITLEntryOperator",
                    "params": {
                        "subject": "Fill",
                        "params": {"amount": {"default": 5, "type": "integer"}},
                    },
                }
            ],
        }
    )
    source = generate_source(spec)
    assert "params={'amount': Param(5, type='integer')}" in source


def _loop_spec(**loop_extra):
    return {
        "dag_id": "l",
        "nodes": [
            {
                "kind": "task_group",
                "id": "g",
                "group_id": "body",
                "loop": {"max_iterations": 3, **loop_extra},
            },
            {
                "kind": "python_task",
                "id": "a",
                "task_id": "a",
                "parent_group": "g",
                "function_name": "a",
                "uses_loop_context": True,
                "code": "return loop.index",
            },
            {
                "kind": "python_task",
                "id": "b",
                "task_id": "b",
                "parent_group": "g",
                "function_name": "b",
                "code": "return 1",
            },
            op("after"),
        ],
        "edges": [{"source": "a", "target": "b"}, {"source": "g", "target": "after"}],
    }


def test_loop_group_codegen_and_rules():
    spec = DagSpec.model_validate(_loop_spec(until_code="return loop.result > 0"))
    source = generate_source(spec, airflow_version=(3, 4, 0))
    assert "@task_group(group_id='body')" in source
    assert ".loop(max_iterations=3, until=until_done)" in source
    assert "def a(*, loop=None):" in source
    report = validate_spec(spec, target_version=(3, 4, 0))
    assert report.ok, report.problems
    assert "airflow_version" in codes(validate_spec(spec, target_version=(3, 3, 0)))


def test_loop_needs_single_terminal_task():
    data = _loop_spec()
    data["edges"] = [{"source": "g", "target": "after"}]  # a and b are both terminal
    assert "loop" in codes(validate_spec(DagSpec.model_validate(data), target_version=(3, 4, 0)))


def test_loop_body_not_wired_from_outside():
    data = _loop_spec()
    data["edges"].append({"source": "b", "target": "after"})
    assert "loop" in codes(validate_spec(DagSpec.model_validate(data), target_version=(3, 4, 0)))


def test_result_and_stub():
    spec = DagSpec.model_validate(
        {
            "dag_id": "r",
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "r",
                    "task_id": "r",
                    "function_name": "r",
                    "is_result": True,
                    "code": "return 1",
                },
                {
                    "kind": "python_task",
                    "id": "s",
                    "task_id": "s",
                    "function_name": "s",
                    "decorator": "stub",
                    "code": "...",
                },
            ],
        }
    )
    source = generate_source(spec, airflow_version=(3, 4, 0))
    assert "@result\n    @task(task_id='r')" in source
    assert "decorator" in codes(validate_spec(spec, target_version=(3, 4, 0)))


def test_python_task_lint_maps_lines():
    spec = DagSpec.model_validate(
        {
            "dag_id": "p",
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "p",
                    "task_id": "p",
                    "function_name": "p",
                    "arguments": {"x": 1},
                    "code": "y = x + 1\nunused = 2\nreturn y + missing_name",
                }
            ],
        }
    )
    lint = {p.code: p for p in validate_spec(spec).problems if p.code.startswith("lint:")}
    assert lint["lint:F821"].line == 3
    assert lint["lint:F821"].severity == "error"
    assert lint["lint:F841"].line == 2


def test_hard_coded_secrets_are_warned_about():
    spec = DagSpec.model_validate(
        {
            "dag_id": "s",
            "nodes": [
                op("a", params={"api_key": "sk-live-123456", "password_conn_id": "pg"}),
                op("b", params={"password": "{{ conn.pg.password }}", "token_url": "https://x/y"}),
                op(
                    "d",
                    params={
                        "bash_command": {
                            "$type": "file",
                            "name": "run.sh",
                            "content": 'export API_TOKEN="0123456789abc"\necho hi\n',
                        }
                    },
                ),
                {
                    "kind": "python_task",
                    "id": "c",
                    "task_id": "c",
                    "function_name": "c",
                    "code": "def c():\n    token = 'abcdef123456'\n    return token\n",
                },
            ],
        }
    )
    secrets = [p for p in validate_spec(spec).problems if p.code == "secret"]
    assert {p.node_id for p in secrets} == {"a", "c", "d"}
    assert {p.severity for p in secrets} == {"warning"}
    assert len(secrets) == 3


def test_loop_until_code_needs_python_code_permission():
    """Security review #2: the loop's until condition is Python emitted into the DAG."""
    spec = DagSpec.model_validate(
        {
            "dag_id": "loopy",
            "nodes": [
                {
                    "kind": "task_group",
                    "id": "g",
                    "group_id": "g",
                    "loop": {"until_code": "return True", "max_iterations": 3},
                },
                op("a", parent_group="g"),
            ],
        }
    )
    blocked = validate_spec(spec, policy=Policy(allow_python_code=False))
    assert any(p.code == "policy" and p.field == "loop.until_code" for p in blocked.problems)
    allowed = validate_spec(spec, policy=Policy(allow_python_code=True))
    assert not any(p.field == "loop.until_code" for p in allowed.problems)
