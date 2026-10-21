"""Custom operators in plain folders (plugins/, dags/, include/) are discovered, configurable and
importable by generated DAGs — like provider operators."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from autoair.catalog import OperatorCatalog
from autoair.catalog.discovery import PackagesSource, PathsSource
from autoair.codegen import render
from autoair.spec import DagSpec
from autoair.validation import validate_spec
from autoair.validation.dagbag_check import check_source

pytestmark = pytest.mark.airflow

GITLAB = '''
from __future__ import annotations

from collections.abc import Sequence

from airflow.sdk import BaseOperator
from airflow.sdk.bases.sensor import BaseSensorOperator

from my_ops._base import CompanyOperator


class GitlabCreateIssueOperator(BaseOperator):
    """Create an issue in a GitLab project.

    :param project: Project path, e.g. ``group/repo``.
    :param title: Issue title (templated).
    :param labels: Labels to set.
    :param gitlab_conn_id: GitLab connection.
    """

    template_fields: Sequence[str] = ("title",)

    def __init__(
        self,
        *,
        project: str,
        title: str,
        labels: list[str] | None = None,
        confidential: bool = False,
        gitlab_conn_id: str = "gitlab_default",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.project = project
        self.title = title
        self.labels = labels or []
        self.confidential = confidential
        self.gitlab_conn_id = gitlab_conn_id

    def execute(self, context):
        return {"project": self.project, "title": self.title}


class GitlabMergeRequestSensor(BaseSensorOperator):
    def __init__(self, *, project: str, iid: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.project = project
        self.iid = iid

    def poke(self, context) -> bool:
        return True


class JiraTicketOperator(CompanyOperator):
    def __init__(self, *, ticket: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.ticket = ticket
'''

BASE = '''
from airflow.sdk import BaseOperator


class CompanyOperator(BaseOperator):
    """Shared base with company defaults."""

    def __init__(self, *, team: str = "data", **kwargs) -> None:
        super().__init__(**kwargs)
        self.team = team

    def execute(self, context):
        return self.team
'''

SQL = '''
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator


class WarehouseQueryOperator(SQLExecuteQueryOperator):
    """Company SQL operator with a default connection."""

    def __init__(self, *, warehouse: str = "main", **kwargs) -> None:
        kwargs.setdefault("conn_id", "warehouse")
        super().__init__(**kwargs)
        self.warehouse = warehouse
'''


@pytest.fixture(scope="module")
def plugins(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("plugins")
    pkg = root / "my_ops"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "_base.py").write_text(textwrap.dedent(BASE))
    (pkg / "gitlab.py").write_text(textwrap.dedent(GITLAB))
    (pkg / "warehouse.py").write_text(textwrap.dedent(SQL))
    (root / "not_an_operator.py").write_text("x = 1\n")
    return root


@pytest.fixture(scope="module")
def catalog(plugins: Path) -> OperatorCatalog:
    return OperatorCatalog([PathsSource([plugins])], use_cache=False)


def test_custom_operators_are_discovered(catalog):
    by_name = {s.class_name: s for s in catalog.summaries()}
    assert {
        "GitlabCreateIssueOperator",
        "GitlabMergeRequestSensor",
        "JiraTicketOperator",
        "WarehouseQueryOperator",
    } <= set(by_name)
    assert "CompanyOperator" in by_name  # a concrete shared base is usable too
    gitlab = by_name["GitlabCreateIssueOperator"]
    assert gitlab.fqn == "my_ops.gitlab.GitlabCreateIssueOperator"
    assert gitlab.provider.startswith("custom:")
    assert gitlab.builder_supported
    assert by_name["GitlabMergeRequestSensor"].category == "sensor"

    detail = catalog.get(gitlab.fqn)
    props = detail.params_schema["properties"]
    assert props["project"]["type"] == "string"
    assert props["labels"]["anyOf"][0]["type"] == "array" or props["labels"].get("type") == "array"
    assert set(detail.params_schema["required"]) >= {"project", "title"}
    assert detail.template_fields == ["title"]
    assert "gitlab" in detail.connection_types or "gitlab_conn_id" in props
    # Inherited parameters (company base, provider SQL operator) are part of the schema.
    assert "team" in catalog.get("my_ops.gitlab.JiraTicketOperator").params_schema["properties"]
    sql = catalog.get("my_ops.warehouse.WarehouseQueryOperator").params_schema["properties"]
    assert {"warehouse", "sql", "conn_id"} <= set(sql)


def test_generated_dag_imports_custom_operators(catalog, plugins):
    spec = DagSpec.model_validate(
        {
            "dag_id": "custom_ops",
            "nodes": [
                {
                    "kind": "operator",
                    "id": "issue",
                    "task_id": "issue",
                    "operator": "my_ops.gitlab.GitlabCreateIssueOperator",
                    "params": {"project": "data/etl", "title": "Run {{ ds }}", "labels": ["etl"]},
                },
                {
                    "kind": "operator",
                    "id": "mr",
                    "task_id": "mr",
                    "operator": "my_ops.gitlab.GitlabMergeRequestSensor",
                    "params": {"project": "data/etl", "iid": 7},
                },
                {
                    "kind": "operator",
                    "id": "jira",
                    "task_id": "jira",
                    "operator": "my_ops.gitlab.JiraTicketOperator",
                    "params": {"ticket": "DATA-1", "team": "platform"},
                },
                {
                    "kind": "operator",
                    "id": "q",
                    "task_id": "q",
                    "operator": "my_ops.warehouse.WarehouseQueryOperator",
                    "params": {"sql": "select 1"},
                },
            ],
            "edges": [
                {"source": "issue", "target": "mr"},
                {"source": "mr", "target": "jira"},
                {"source": "jira", "target": "q"},
            ],
        }
    )
    report = validate_spec(spec, catalog=catalog)
    assert report.ok, report.problems
    source = render(spec)
    assert "from my_ops.gitlab import" in source
    # The DagBag check runs in a subprocess: the plugins folder must be importable there too.
    check = check_source(source, "custom_ops.py")
    assert check.ok, check.import_errors
    assert sorted(check.task_ids["custom_ops"]) == ["issue", "jira", "mr", "q"]

    bad = spec.model_copy(deep=True)
    bad.nodes[0].params = {"title": "x", "confidential": "yes"}  # missing project, wrong type
    problems = " ".join(p.message for p in validate_spec(bad, catalog=catalog).problems)
    assert "project" in problems
    assert "confidential" in problems


def test_installed_package_source(plugins):
    cat = OperatorCatalog([PackagesSource(["my_ops"])], use_cache=False)
    assert "my_ops.gitlab.GitlabCreateIssueOperator" in {s.fqn for s in cat.summaries()}


def test_catalog_cache_notices_edited_custom_operators(plugins, tmp_path):
    cat = OperatorCatalog([PathsSource([plugins])], cache_dir=tmp_path)
    key = cat.cache_key_parts()
    f = plugins / "my_ops" / "extra.py"
    f.write_text(
        "from airflow.sdk import BaseOperator\n\n"
        "class NewOperator(BaseOperator):\n"
        "    def execute(self, context):\n        return 1\n"
    )
    try:
        assert cat.cache_key_parts() != key
    finally:
        f.unlink()
