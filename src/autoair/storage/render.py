"""Turn a spec into the generated file(s) a deploy target persists next to it."""

from __future__ import annotations

from pathlib import Path

from autoair.loader import render_stub
from autoair.registry import HandlerRegistry
from autoair.spec import DagSpec
from autoair.storage.base import DeployTarget, SpecSummary, StorageError


def render_dag_files(
    spec: DagSpec,
    *,
    environment: str | None = None,
    registry: HandlerRegistry | None = None,
    python_source: str | None = None,
    strict: bool = False,
    fingerprint: bool = True,
) -> dict[str, str]:
    """``{file name: content}`` for ``spec`` (python and loader output modes).

    The ``environment`` overlay is applied when the spec defines one (an unknown
    environment is an error only with ``strict=True``). ``python_source``
    short-circuits rendering when the caller already rendered it. Generated Python files
    carry the generator fingerprint (:mod:`autoair.preserve.fingerprint`) unless
    ``fingerprint=False``; raw code DAGs never do (they are written as typed).
    Other output modes need a renderer supplied by the caller.
    """
    if environment and environment not in spec.environments and not strict:
        environment = None
    try:
        effective = spec.with_environment(environment)
    except KeyError as exc:
        raise StorageError(str(exc.args[0]) if exc.args else str(exc)) from exc
    if spec.output_mode == "python":
        if python_source is None:
            from autoair.codegen import render

            python_source = render(effective, registry)
        if fingerprint and spec.raw_code is None:
            from autoair.preserve.fingerprint import add

            python_source = add(python_source)
        return {f"{spec.dag_id}.py": python_source}
    if spec.output_mode == "loader":
        return {f"{spec.dag_id}.py": render_stub(spec.dag_id)}
    raise StorageError(f"output mode {spec.output_mode!r} needs a custom file renderer")


class TargetRepository:
    """Adapts any :class:`DeployTarget` to the spec-level ``DagRepository`` interface."""

    def __init__(self, target: DeployTarget) -> None:
        self.target = target

    def list(self) -> list[SpecSummary]:
        return self.target.list()

    def get(self, dag_id: str) -> DagSpec | None:
        stored = self.target.get(dag_id)
        return stored.spec if stored else None

    def save(self, spec: DagSpec, python_source: str | None) -> Path | str:
        if spec.output_mode == "python" and python_source is None:
            raise StorageError("python output mode requires generated source")
        files = render_dag_files(spec, python_source=python_source)
        return self.target.save(spec, files).path

    def delete(self, dag_id: str) -> bool:
        return self.target.delete(dag_id)
