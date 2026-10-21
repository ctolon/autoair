"""Template gallery: built-in starter DAGs plus user-saved templates and fragments.

* ``kind="dag"``: a complete :class:`DagSpec` to start a new DAG from.
* ``kind="fragment"``: a reusable sub-graph (nodes + edges) inserted into the current
  canvas — AutoAir's "macro" / building-block mechanism. Node ids are re-generated on
  insert by the frontend.
* ``kind="operator"``: an *operator template* (blueprint) — one configured task (operator +
  params + execution settings) a platform team publishes as a reusable step. Fields can be
  ``locked`` (read-only on insert; validation flags changes of tasks carrying the template's
  ``template_ref``) or ``required`` (asked when inserting); the rest are defaults.

User templates are stored as documents of the persistence layer (files under any fsspec
URL, or rows of the SQL database). Each saved operator template version is also appended to
a per-template history document, so older versions stay readable; tasks keep the version
they were inserted from (``template_ref.version``) and validation compares it with the
current one. Saves and deletes are compare-and-set on a content etag (``If-Match``).
"""

from __future__ import annotations

import builtins
import datetime
import functools
import hashlib
import json
import logging
import re
from collections.abc import Iterable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from autoair._compat import airflow_version
from autoair.persistence.documents import DocumentStore, FileDocumentStore
from autoair.spec import CustomNode, DagSpec, Edge, Node, OperatorNode, PythonTaskNode
from autoair.templates.builtin import BUILTIN_TEMPLATES
from autoair.validation import MISSING_VALUE, Problem, TemplateLock, template_field_value

TemplateKind = Literal["dag", "fragment", "operator"]
log = logging.getLogger(__name__)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")
_ICON = r"^(builtin|custom|package):[a-z0-9][a-z0-9-]{0,80}$"
# Dict-valued node fields whose entries can be locked/required one by one.
_DICT_FIELDS = frozenset({"params", "arguments", "decorator_kwargs", "config", "executor_config"})
# Identity/canvas fields a template never fixes (they are per-task).
_NOT_LOCKABLE = frozenset(
    {"id", "kind", "task_id", "label", "icon", "parent_group", "position", "template_ref"}
)
FieldPath = Annotated[
    str, Field(pattern=r"^[a-z_][a-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$", max_length=200)
]
BlueprintNode = Annotated[OperatorNode | PythonTaskNode | CustomNode, Field(discriminator="kind")]


class OperatorBlueprint(BaseModel):
    """One configured task plus the guardrails applied when it is reused."""

    node: BlueprintNode
    locked: list[FieldPath] = Field(
        default_factory=list,
        description="Field paths fixed by the template (`retries`, `params.bash_command`): "
        "read-only on insert, a validation error when changed.",
    )
    required: list[FieldPath] = Field(
        default_factory=list,
        description="Field paths the user fills in when inserting (asked in a form); every "
        "other field keeps the template's value as an editable default.",
    )

    @model_validator(mode="after")
    def _paths(self) -> OperatorBlueprint:
        both = set(self.locked) & set(self.required)
        if both:
            raise ValueError(f"fields both locked and required: {', '.join(sorted(both))}")
        fields = type(self.node).model_fields
        for path in [*self.locked, *self.required]:
            head, _, rest = path.partition(".")
            if head in _NOT_LOCKABLE or head not in fields:
                raise ValueError(f"{path!r} is not a configurable field of a {self.node.kind} task")
            if rest and head not in _DICT_FIELDS:
                raise ValueError(f"{path!r}: only {', '.join(sorted(_DICT_FIELDS))} have entries")
        return self

    def lock(self, title: str, version: str | None) -> TemplateLock:
        node = self.node
        locked = {p: template_field_value(node, p) for p in self.locked}
        # The task type itself is always fixed.
        if isinstance(node, PythonTaskNode):
            locked.setdefault("decorator", node.decorator)
        elif isinstance(node, CustomNode):
            locked.setdefault("handler", node.handler)
        return TemplateLock(
            title=title,
            version=version,
            kind=node.kind,
            operator=node.operator if isinstance(node, OperatorNode) else None,
            locked={k: v for k, v in locked.items() if v is not MISSING_VALUE},
            required=tuple(self.required),
        )


class Fragment(BaseModel):
    nodes: list[Node] = Field(min_length=1)
    edges: list[Edge] = Field(default_factory=list)


class TemplateSummary(BaseModel):
    id: str
    title: str
    description: str = ""
    category: str = "general"
    tags: list[str] = []
    kind: TemplateKind
    builtin: bool
    requires: list[str] = Field(default_factory=list, description="Provider packages needed.")
    min_airflow: str | None = Field(default=None, description="Minimum Airflow version.")
    available: bool = True
    owner_team: str | None = Field(default=None, description="Team maintaining the template.")
    version: str | None = Field(
        default=None, description="Template version (operator templates: bumped on each save)."
    )
    icon: str | None = Field(default=None, pattern=_ICON, description="Palette/canvas icon.")
    operator_class: str | None = Field(
        default=None,
        description="Operator templates: the task type (operator class path, @task.<decorator> "
        "or custom handler).",
    )
    docs_url: str | None = Field(
        default=None, description="Operator templates: a documentation link (http or https)."
    )
    updated_at: str | None = Field(default=None, description="When this version was saved (UTC).")
    updated_by: str | None = Field(default=None, description="Who saved this version.")


class Template(TemplateSummary):
    spec: DagSpec | None = None
    fragment: Fragment | None = None
    blueprint: OperatorBlueprint | None = None
    etag: str | None = Field(
        default=None,
        description="Opaque version of the stored document: send it as `If-Match` to update "
        "or delete only if nobody changed the template since (409 otherwise).",
    )


class TemplateVersionInfo(BaseModel):
    """One stored version of an operator template (newest first in listings)."""

    version: str
    title: str
    updated_at: str | None = None
    updated_by: str | None = None
    operator_class: str | None = None
    current: bool = Field(description="This is the version tasks are validated against.")


class TemplateInput(BaseModel):
    id: str = Field(pattern=_ID_RE.pattern)
    title: str = Field(min_length=1, max_length=120)
    description: str = ""
    category: str = "custom"
    tags: list[str] = []
    kind: TemplateKind
    owner_team: str | None = Field(default=None, max_length=120)
    version: str | None = Field(
        default=None,
        max_length=40,
        description="Explicit version; empty bumps the previous numeric version (1, 2, …).",
    )
    icon: str | None = Field(default=None, pattern=_ICON)
    docs_url: str | None = Field(default=None, max_length=500, pattern=r"^https?://\S+$")
    spec: DagSpec | None = None
    fragment: Fragment | None = None
    blueprint: OperatorBlueprint | None = None


def _type_label(node: OperatorNode | PythonTaskNode | CustomNode) -> str:
    if isinstance(node, OperatorNode):
        return node.operator
    if isinstance(node, PythonTaskNode):
        return "@task" if node.decorator == "task" else f"@task.{node.decorator}"
    return node.handler


def _next_version(previous: str | None) -> str:
    if previous and previous.isdigit():
        return str(int(previous) + 1)
    return "1"


#: Node id of the task in a stored blueprint and in its throwaway validation spec.
BLUEPRINT_NODE_ID = "template"
PREVIEW_DAG_ID = "operator_template_preview"


def blueprint_spec(blueprint: OperatorBlueprint) -> DagSpec:
    """A one-task DAG holding the blueprint's task, to validate and render it on its own
    (no ``template_ref``: the template's own locks do not apply to itself)."""
    raw = blueprint.node.model_dump(mode="json", exclude={"template_ref"})
    raw.update(id=BLUEPRINT_NODE_ID, parent_group=None)
    return DagSpec.model_validate({"dag_id": PREVIEW_DAG_ID, "nodes": [raw], "edges": []})


def blueprint_problems(problems: Iterable[Problem], blueprint: OperatorBlueprint) -> list[Problem]:
    """The problems of :func:`blueprint_spec` that concern the template: the task's own,
    minus those of required fields (their value is left out on purpose and asked on
    insert). DAG-level findings of the throwaway DAG (schedule, tags, owner...) are dropped."""
    asked = set(blueprint.required) | {p.removeprefix("params.") for p in blueprint.required}
    return [
        p for p in problems if p.node_id == BLUEPRINT_NODE_ID and not (p.field and p.field in asked)
    ]


class TemplateError(Exception):
    pass


class TemplateConflict(TemplateError):
    """The template changed since the caller read it (``If-Match``), or it exists already
    (``If-None-Match: *``)."""

    def __init__(self, message: str, current: Template | None = None) -> None:
        super().__init__(message)
        self.current = current


def _content_etag(data: bytes) -> str:
    # Derived from the bytes, not the backend's row version: the same value on file and SQL
    # stores, and checkable inside DocumentStore.update (which only sees the bytes).
    return hashlib.sha256(data).hexdigest()[:20]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _parse(data: bytes | None) -> Template | None:
    if data is None:
        return None
    try:
        tpl = Template.model_validate(json.loads(data))
    except ValueError:
        log.warning("ignoring an unreadable stored template", exc_info=True)
        return None
    return tpl.model_copy(update={"builtin": False, "etag": _content_etag(data)})


@functools.cache
def _builtin_templates() -> dict[str, Template]:
    """Built-in templates, validated once per process (they are static data)."""
    installed = airflow_version()
    out: dict[str, Template] = {}
    for raw in BUILTIN_TEMPLATES:
        tpl = Template.model_validate(raw)
        if tpl.min_airflow and installed is not None:
            need = tuple(int(x) for x in tpl.min_airflow.split("."))
            tpl.available = installed >= need
        out[tpl.id] = tpl
    return out


class TemplateStore:
    """User templates (one document per id: the current version) plus, for operator
    templates, the version history (one document per id in ``history``)."""

    def __init__(
        self,
        url: str | None = None,
        storage_options: dict[str, Any] | None = None,
        *,
        documents: DocumentStore | None = None,
        history: DocumentStore | None = None,
        history_limit: int = 100,
    ) -> None:
        # Files <url>/<id>.json by default; rows of the SQL database when one is configured.
        if documents is None:
            if url is None:
                raise ValueError("TemplateStore needs a url or a document store")
            documents = FileDocumentStore(url, storage_options=storage_options)
            if history is None:
                history = FileDocumentStore(
                    f"{url.rstrip('/')}/_history", storage_options=storage_options
                )
        self.documents: DocumentStore = documents
        self.history: DocumentStore | None = history
        self.history_limit = max(1, history_limit)

    def _path(self, template_id: str) -> str:
        if not _ID_RE.fullmatch(template_id):
            raise TemplateError(f"invalid template id {template_id!r}")
        return template_id

    def _builtin(self) -> dict[str, Template]:
        return dict(_builtin_templates())

    def _user(self) -> dict[str, Template]:
        out: dict[str, Template] = {}
        try:
            paths = self.documents.keys()
        except FileNotFoundError:
            return out
        for path in sorted(paths):
            try:
                found = self.documents.get(path)
                if found is None:
                    continue
                tpl = Template.model_validate(json.loads(found.data))
            except Exception:
                log.warning("skipping unreadable template %s", path, exc_info=True)
                continue
            out[tpl.id] = tpl.model_copy(update={"builtin": False})
        return out

    def list(self) -> list[TemplateSummary]:
        merged = {**self._builtin(), **self._user()}
        return [
            TemplateSummary.model_validate(t.model_dump(include=set(TemplateSummary.model_fields)))
            for t in merged.values()
        ]

    def get(self, template_id: str) -> Template | None:
        user = self._read_user(template_id)
        if user is not None:
            return user
        builtin = _builtin_templates().get(template_id)
        # A copy: callers may adjust the result without touching the shared cache.
        return builtin.model_copy(deep=True) if builtin is not None else None

    def _read_user(self, template_id: str) -> Template | None:
        """One user template by id (not a scan of the whole folder)."""
        path = self._path(template_id)
        try:
            found = self.documents.get(path)
        except FileNotFoundError:
            return None
        except OSError:
            log.warning("ignoring unreadable template %s", path, exc_info=True)
            return None
        return _parse(found.data) if found is not None else None

    def lock(self, template_id: str) -> TemplateLock | None:
        """What an operator template enforces (validation of ``template_ref``), or None."""
        try:
            tpl = self.get(template_id)
        except TemplateError:
            return None
        if tpl is None or tpl.blueprint is None:
            return None
        return tpl.blueprint.lock(tpl.title, tpl.version)

    def save(
        self,
        data: TemplateInput,
        *,
        user: str | None = None,
        if_match: str | None = None,
        create_only: bool = False,
    ) -> Template:
        """Store ``data`` as the current version. Operator templates get the next version
        number and a history entry. ``if_match``: only if the stored etag still equals it;
        ``create_only``: only if the id is free (:class:`TemplateConflict` otherwise). The
        version bump is one atomic read-modify-write, so concurrent saves on several replicas
        get distinct versions."""
        if data.id in self._builtin():
            raise TemplateError(f"{data.id!r} is a built-in template id")
        if (
            (data.kind == "dag") != (data.spec is not None)
            or (data.kind == "fragment") != (data.fragment is not None)
            or (data.kind == "operator") != (data.blueprint is not None)
        ):
            raise TemplateError(
                "a dag template needs `spec`, a fragment needs `fragment`, an operator "
                "template needs `blueprint`"
            )
        blueprint = data.blueprint
        if blueprint is not None:
            # Stored without per-task identity: the canvas assigns ids/positions on insert.
            raw = blueprint.node.model_dump(exclude={"position", "parent_group", "template_ref"})
            node = type(blueprint.node).model_validate({**raw, "id": BLUEPRINT_NODE_ID})
            blueprint = blueprint.model_copy(update={"node": node})
        key = self._path(data.id)
        result: dict[str, Any] = {}
        # A published version is immutable: tasks record it and the history keeps it.
        if (
            blueprint is not None
            and data.version
            and any(e.get("version") == data.version for e in self._history_entries(data.id))
        ):
            raise TemplateError(
                f"version {data.version!r} of {data.id!r} exists already; leave the "
                "version empty to publish the next number"
            )

        def build(current: bytes | None) -> bytes:
            previous = _parse(current)
            if create_only and current is not None:
                raise TemplateConflict(f"template {data.id!r} already exists", previous)
            if if_match is not None and (current is None or _content_etag(current) != if_match):
                raise TemplateConflict(
                    f"template {data.id!r} was changed or deleted since you loaded it", previous
                )
            extra: dict[str, Any] = {"updated_at": _now(), "updated_by": user}
            if blueprint is not None and previous is not None and data.version == previous.version:
                raise TemplateError(
                    f"version {data.version!r} of {data.id!r} exists already; leave the "
                    "version empty to publish the next number"
                )
            if blueprint is not None:
                extra["version"] = data.version or _next_version(
                    previous.version if previous is not None else None
                )
                extra["operator_class"] = _type_label(blueprint.node)
            tpl = Template(
                **{**data.model_dump(exclude={"spec", "fragment", "blueprint"}), **extra},
                builtin=False,
                spec=data.spec,
                fragment=data.fragment,
                blueprint=blueprint,
            )
            payload = json.dumps(
                tpl.model_dump(mode="json", exclude_none=True, exclude={"etag"}), indent=2
            ).encode()
            result.update(tpl=tpl, previous=previous, etag=_content_etag(payload))
            return payload

        self.documents.update(key, build)
        saved: Template = result["tpl"].model_copy(update={"etag": result["etag"]})
        if saved.blueprint is not None:
            self._record_version(saved, result["previous"])
        return saved

    def _history_entries(self, template_id: str) -> builtins.list[dict[str, Any]]:
        if self.history is None:
            return []
        try:
            found = self.history.get(self._path(template_id))
        except (FileNotFoundError, OSError):
            return []
        if found is None:
            return []
        try:
            versions = json.loads(found.data).get("versions", [])
        except (ValueError, AttributeError):
            log.warning("ignoring unreadable template history %s", template_id, exc_info=True)
            return []
        return [v for v in versions if isinstance(v, dict)]

    def _record_version(self, tpl: Template, previous: Template | None) -> None:
        if self.history is None:
            return
        snapshot = tpl.model_dump(mode="json", exclude_none=True, exclude={"etag"})
        before = (
            previous.model_dump(mode="json", exclude_none=True, exclude={"etag"})
            if previous is not None and previous.blueprint is not None
            else None
        )

        def add(current: bytes | None) -> bytes:
            versions: builtins.list[dict[str, Any]] = []
            if current is not None:
                try:
                    loaded = json.loads(current).get("versions", [])
                    versions = [v for v in loaded if isinstance(v, dict)]
                except (ValueError, AttributeError):
                    log.warning("resetting unreadable template history %s", tpl.id)
            # The replaced version may lack a history entry (a template file placed by hand,
            # or an earlier history write failed): record it before it is overwritten.
            if before is not None and not any(
                v.get("version") == before.get("version") for v in versions
            ):
                versions.append(before)
            versions = [v for v in versions if v.get("version") != tpl.version]
            versions.append(snapshot)
            return json.dumps({"versions": versions[-self.history_limit :]}).encode()

        try:
            self.history.update(self._path(tpl.id), add)
        except (OSError, ValueError):
            # The current version is saved; a missing history entry only hides an old version.
            log.warning("could not record version %s of template %s", tpl.version, tpl.id)

    def versions(self, template_id: str) -> builtins.list[TemplateVersionInfo] | None:
        """Stored versions of an operator template, newest first; ``None`` if unknown."""
        current = self.get(template_id)
        if current is None:
            return None
        entries = self._history_entries(template_id) if not current.builtin else []
        if not any(e.get("version") == current.version for e in entries):
            entries.append(current.model_dump(mode="json", exclude_none=True))
        out: builtins.list[TemplateVersionInfo] = []
        for entry in reversed(entries):
            out.append(
                TemplateVersionInfo(
                    version=str(entry.get("version") or ""),
                    title=str(entry.get("title") or template_id),
                    updated_at=entry.get("updated_at"),
                    updated_by=entry.get("updated_by"),
                    operator_class=entry.get("operator_class"),
                    current=entry.get("version") == current.version,
                )
            )
        return out

    def get_version(self, template_id: str, version: str) -> Template | None:
        """One stored version (the current one included); ``None`` if it is not kept."""
        current = self.get(template_id)
        if current is None:
            return None
        if current.version == version:
            return current
        for entry in self._history_entries(template_id):
            if entry.get("version") == version:
                try:
                    tpl = Template.model_validate(entry)
                except ValueError:
                    return None
                return tpl.model_copy(update={"builtin": False, "etag": None})
        return None

    def delete(self, template_id: str, *, if_match: str | None = None) -> bool:
        """Delete a user template and its history. ``if_match``: only if unchanged."""
        key = self._path(template_id)
        state = {"deleted": False}

        def drop(current: bytes | None) -> bytes | None:
            if current is None:
                return None
            if if_match is not None and _content_etag(current) != if_match:
                raise TemplateConflict(
                    f"template {template_id!r} was changed since you loaded it", _parse(current)
                )
            state["deleted"] = True
            return None

        self.documents.update(key, drop)
        if state["deleted"] and self.history is not None:
            try:
                self.history.delete(key)
            except OSError:
                log.warning("could not delete the history of template %s", template_id)
        return state["deleted"]
