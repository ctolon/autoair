"""Typed tool results (their JSON schemas become the tools' ``outputSchema``)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from autoair.ai.memory import MemoryItem
from autoair.ai.problems import SpecProblem
from autoair.ai.rag import Hit
from autoair.catalog import OperatorCategory
from autoair.gateway import ConnectionInfo, DagRunInfo, VariableInfo
from autoair.mcp.store import StoredDag
from autoair.spec import DagSpec
from autoair.templates import Template, TemplateSummary, TemplateVersionInfo


class Page(BaseModel):
    total: int = Field(description="Number of matches before paging.")
    truncated: bool = Field(description="True when more results exist (see next_offset).")
    next_offset: int | None = Field(
        default=None, description="Pass as `offset` to get the next page."
    )


class OperatorBrief(BaseModel):
    fqn: str = Field(description="Use as `operator` of a `kind: operator` node.")
    class_name: str
    provider: str
    category: OperatorCategory
    summary: str = ""
    builder_supported: bool = True


class OperatorList(Page):
    operators: list[OperatorBrief]


class NodeKindInfo(BaseModel):
    kind: str = Field(description="Value of the node's `kind` field.")
    handler: str | None = Field(
        default=None, description="For custom node kinds: the node's `handler` value."
    )
    title: str
    description: str
    config_schema: dict[str, Any] | None = None


class NodeKindList(BaseModel):
    node_kinds: list[NodeKindInfo]


class NotifierList(BaseModel):
    available: bool = Field(description="False when this AutoAir build has no notifier catalog.")
    notifiers: list[dict[str, Any]]


class ValidationOutcome(BaseModel):
    ok: bool
    problems: list[SpecProblem]
    import_errors: list[str] = []


class CodeResult(BaseModel):
    code: str | None
    problems: list[SpecProblem]


class DagCode(BaseModel):
    dag_id: str
    document_type: str = Field(
        description="raw: the hand-written file (edit and save it with save_raw_dag_code); "
        "spec: the file generated from the visual spec (edit the spec instead)."
    )
    code: str


class DagList(Page):
    dags: list[StoredDag]


class SaveResult(BaseModel):
    saved: bool
    dry_run: bool
    location: str | None = None
    problems: list[SpecProblem] = []
    message: str


class DeleteOutcome(BaseModel):
    deleted: bool
    message: str


class ConnectionList(BaseModel):
    connections: list[ConnectionInfo]


class VariableList(BaseModel):
    variables: list[VariableInfo]


class TriggerOutcome(BaseModel):
    triggered: bool
    dag_run_id: str | None = None
    state: str | None = None
    message: str


class KnowledgeHits(BaseModel):
    hits: list[Hit]
    retrieval: str = Field(description="'bm25' or 'hybrid' (BM25 + embeddings).")


class PatchResult(BaseModel):
    ok: bool = Field(description="True when the patched spec validates.")
    spec: DagSpec | None = Field(description="The patched spec (null if it no longer parses).")
    problems: list[SpecProblem]
    error: str | None = Field(default=None, description="Why the patch could not be applied.")


class ImportedSpec(BaseModel):
    dag_id: str
    spec: DagSpec | None
    warnings: list[str]
    error: str | None = None


class ImportResult(BaseModel):
    dags: list[ImportedSpec]
    import_errors: list[str]


class RunList(BaseModel):
    runs: list[DagRunInfo]


class TaskLog(BaseModel):
    text: str = Field(description="Tail of the log.")
    truncated: bool
    total_chars: int


class MemoryList(Page):
    memories: list[MemoryItem]


class MemoryOutcome(BaseModel):
    ok: bool
    memory: MemoryItem | None = None
    message: str


class OperatorTemplateList(Page):
    templates: list[TemplateSummary]


class OperatorTemplateDetail(BaseModel):
    template: Template = Field(
        description="The template; `blueprint.node` is the task, `locked`/`required` its "
        "field modes. Pass `etag` as `if_match` to save_operator_template."
    )
    versions: list[TemplateVersionInfo] = Field(description="Stored versions, newest first.")


class TemplateSaveResult(BaseModel):
    saved: bool
    dry_run: bool
    template: Template | None = None
    problems: list[SpecProblem] = Field(
        default_factory=list,
        description="Validation of the template's task (fields asked on insert excluded).",
    )
    message: str
