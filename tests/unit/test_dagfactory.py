from __future__ import annotations

import yaml

from autoair.codegen.dagfactory import export_dag_factory
from autoair.spec import DagSpec


def test_export(golden_spec_data):
    result = export_dag_factory(DagSpec.model_validate(golden_spec_data))
    data = yaml.safe_load(result.yaml)["etl_full"]
    tasks = {t["task_id"]: t for t in data["tasks"]}
    assert tasks["download"]["task_group_name"] == "extract"
    assert tasks["process"]["expand"]["bash_command"]
    assert data["schedule"] == "0 6 * * 1-5"
    assert any("python_task" in w for w in result.warnings)


def test_hitl_numeric_response_timeout_is_exported_as_timedelta():
    spec = DagSpec.model_validate(
        {
            "dag_id": "h",
            "nodes": [
                {
                    "kind": "operator",
                    "id": "a",
                    "task_id": "approve",
                    "operator": "airflow.providers.standard.operators.hitl.HITLOperator",
                    "params": {"subject": "s", "options": ["ok"], "response_timeout": 600},
                }
            ],
        }
    )
    doc = yaml.safe_load(export_dag_factory(spec).yaml)
    task = next(iter(doc.values()))["tasks"]
    task = task["approve"] if isinstance(task, dict) else task[0]
    assert task["response_timeout"] == {"__type__": "datetime.timedelta", "seconds": 600}
