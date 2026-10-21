"""Example custom operators (AutoAir demo): discovered from Airflow's plugins folder."""

from __future__ import annotations

from collections.abc import Sequence

from airflow.sdk import BaseOperator
from airflow.sdk.bases.sensor import BaseSensorOperator


class GitlabCreateIssueOperator(BaseOperator):
    """Create an issue in a GitLab project.

    :param project: Project path, e.g. ``group/repo``.
    :param title: Issue title (templated).
    :param labels: Labels to set.
    :param gitlab_conn_id: GitLab connection.
    """

    template_fields: Sequence[str] = ("title",)
    ui_color = "#fc6d26"

    def __init__(
        self,
        *,
        project: str,
        title: str,
        labels: list[str] | None = None,
        gitlab_conn_id: str = "gitlab_default",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.project = project
        self.title = title
        self.labels = labels or []
        self.gitlab_conn_id = gitlab_conn_id

    def execute(self, context):
        self.log.info("would create issue %r in %s", self.title, self.project)
        return {"project": self.project, "title": self.title}


class GitlabMergeRequestSensor(BaseSensorOperator):
    """Wait until a merge request is merged."""

    def __init__(self, *, project: str, iid: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.project = project
        self.iid = iid

    def poke(self, context) -> bool:
        return True
