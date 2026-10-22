"""Typed tool results (their JSON schemas become the tools' ``outputSchema``)."""

from __future__ import annotations

from typing import Any, Literal

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


class BlueprintList(BaseModel):
    available: bool = Field(
        description="False when the blueprint catalog is off (features.blueprints) or missing."
    )
    blueprints: list[dict[str, Any]] = Field(
        description="Blueprint versions: name, version, class_path, config_class, config_schema."
    )
    total: int = 0


class ValidationOutcome(BaseModel):
    ok: bool
    problems: list[SpecProblem]
    import_errors: list[str] = Field(
        default_factory=list,
        description="Errors of importing the generated DAG (import_check_dag_spec only).",
    )
    note: str | None = Field(
        default=None, description="Deprecation notes about the arguments of this call."
    )


class CodeResult(BaseModel):
    code: str | None
    problems: list[SpecProblem]


class DagCode(BaseModel):
    dag_id: str
    document_type: str = Field(
        description="raw: the hand-written file (edit and save it with save_raw_dag_code); "
        "spec: the DAG file of a visual spec, generated or (source policy preserve) the "
        "hand-written file the DAG keeps; edit the spec instead (saves patch that file)."
    )
    code: str


class DagList(Page):
    dags: list[StoredDag]


class SourceSummary(BaseModel):
    """How the DAG file is written (source policy): generated, or the existing file patched."""

    policy: str = Field(description="generate, preserve or strict-preserve.")
    action: str = Field(
        description="generate, preserve (the existing file is patched), unchanged, blocked "
        "(a file changed outside AutoAir), refused (the change cannot be applied to the file)."
    )
    target_state: str = Field(description="absent, clean, ejected or foreign.")
    operations: list[str] = []
    warnings: list[str] = []
    refusals: list[str] = []
    options: list[str] = []
    diff: str = Field(default="", description="Unified diff of the DAG file (may be cut).")
    diff_truncated: bool = False
    target_sha: str | None = Field(
        default=None,
        description="Hash of the DAG file the plan was made from ('' = none): pass it to "
        "save_dag_spec as expected_target_sha to save exactly what was previewed.",
    )
    content_hidden: bool = Field(
        default=False,
        description="The target holds a hand-written file AutoAir never wrote: its content "
        "is not shown.",
    )
    message: str = ""


class SaveResult(BaseModel):
    saved: bool
    dry_run: bool
    location: str | None = None
    problems: list[SpecProblem] = []
    source: SourceSummary | None = None
    etag: str | None = Field(
        default=None, description="Etag of the saved spec: the base_etag of the next save."
    )
    current_etag: str | None = Field(
        default=None,
        description="Etag of the spec stored now (the DAG exists): pass it as base_etag to "
        "save_dag_spec to overwrite it.",
    )
    status: Literal["saved", "pr_opened", "pending_approval"] | None = Field(
        default=None,
        description="saved: written live; pr_opened: written to a pull request (live once "
        "merged); pending_approval: stored as a deploy proposal that another user must "
        "approve in AutoAir (iam.require_approval). None for dry runs and refusals.",
    )
    proposal_etag: str | None = Field(
        default=None, description="The deploy proposal awaiting approval (pending_approval)."
    )
    message: str


class DeleteOutcome(BaseModel):
    deleted: bool
    current_etag: str | None = Field(
        default=None,
        description="Etag of the spec stored now: pass it as base_etag to delete_dag_spec.",
    )
    status: Literal["deleted", "pr_opened", "pending_approval"] | None = Field(
        default=None,
        description="deleted: removed live; pr_opened: a pull request removes it (live until "
        "merged); pending_approval: stored as a delete proposal another user must approve.",
    )
    proposal_etag: str | None = Field(
        default=None, description="The delete proposal awaiting approval (pending_approval)."
    )
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
