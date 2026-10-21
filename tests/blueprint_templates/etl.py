"""Blueprint templates for the tests: a TaskGroup, a second version, a single task and one
with an explicit name whose defaults are a complete config."""

from __future__ import annotations

from typing import Literal

from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import TaskGroup
from blueprint import BaseModel, Blueprint, Field


class ExtractConfig(BaseModel):
    source_table: str = Field(description="Source table (schema.table)")
    batch_size: int = Field(default=1000, ge=1)


class Extract(Blueprint[ExtractConfig]):
    """Extract rows from a source table. Validates first."""

    def render(self, config: ExtractConfig) -> TaskGroup:
        with TaskGroup(group_id=self.step_id) as group:
            validate = EmptyOperator(task_id="validate")
            extract = EmptyOperator(task_id="extract")
            validate >> extract
        return group


class ExtractV2Config(BaseModel):
    tables: list[str] = Field(min_length=1)


class ExtractV2(Blueprint[ExtractV2Config]):
    """Extract several tables in parallel."""

    def render(self, config: ExtractV2Config) -> TaskGroup:
        with TaskGroup(group_id=self.step_id) as group:
            for table in config.tables:
                EmptyOperator(task_id=f"extract_{table}")
        return group


class LoadConfig(BaseModel):
    target_table: str
    mode: Literal["append", "overwrite"] = "append"


class Load(Blueprint[LoadConfig]):
    """Load into a table."""

    def render(self, config: LoadConfig) -> EmptyOperator:
        return EmptyOperator(task_id=self.step_id)


class NotifyConfig(BaseModel):
    channel: str = "#data"


class TeamNotifier(Blueprint[NotifyConfig]):
    """Tell the team."""

    name = "notify_team"
    version = 1

    def render(self, config: NotifyConfig) -> TaskGroup:
        with TaskGroup(group_id=self.step_id) as group:
            EmptyOperator(task_id="compose") >> EmptyOperator(task_id="send")
        return group
