"""Source plans of deploys (:mod:`autoair.preserve.service`) with AutoAir's services: the
file now in the deploy target, the stored spec, the admin switches and the caller's rights.

Shared by the deploy route (``PUT /specs/{dag_id}``), the source preview route and the MCP
tools, so all three decide the same way what is written.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from autoair.codegen import render
from autoair.iam import model as P
from autoair.iam.deps import iam_settings
from autoair.iam.model import Principal
from autoair.preserve.service import (
    PlanContext,
    Resolution,
    SourceIssue,
    SourcePlan,
    classify,
    folder_importer,
    keeps_hand_written_file,
    plan_source,
)
from autoair.spec import DagSpec
from autoair.storage import DeployTarget, StorageError
from autoair.storage.base import ExistingFile, sha256_text
from autoair.storage.git import GitMode, GitTarget
from autoair.storage.local import LocalFolderTarget

log = logging.getLogger(__name__)


def can_execute(services: Any, principal: Principal | None) -> bool:
    """The round trip imports the DAG file in a sandboxed subprocess: it needs Python code
    (features.python_code) and, like the deploy import check, 'code:execute' unless
    iam.import_check_requires_code_execute is off."""
    if not services.settings.feature("python_code"):
        return False
    if principal is None:
        return True  # MCP over stdio: the user's own process
    if not iam_settings(services).import_check_requires_code_execute:
        return True
    return principal.has(P.CODE_EXECUTE)


def plan_context(
    services: Any, principal: Principal | None, target: DeployTarget | None = None
) -> PlanContext:
    settings = services.settings
    registry = getattr(services, "registry", None)
    ctx = PlanContext(
        cfg=settings.preserve,
        preserve_enabled=settings.feature("source_preserve"),
        force_strict=settings.feature("strict_source_preserve"),
        render=lambda spec: render(spec, registry),
        can_execute=can_execute(services, principal),
        can_overwrite=principal is None or principal.has(P.DEPLOY_OVERWRITE_EJECTED),
    )
    root = getattr(target, "root", None) if isinstance(target, LocalFolderTarget) else None
    if isinstance(root, Path):
        # Like Airflow, the DAG file may import helper modules of its dags folder.
        ctx.importer = folder_importer([root])
        ctx.import_scope = str(root)
    return ctx


def existing_file(
    target: DeployTarget, dag_id: str, env: str | None, push_mode: GitMode | None
) -> ExistingFile | None:
    reader = getattr(target, "current_file", None)
    if reader is None:
        return None
    if isinstance(target, GitTarget):
        found: ExistingFile | None = reader(dag_id, environment=env, mode=push_mode)
    else:
        found = reader(dag_id)
    return found


def stored_spec(target: DeployTarget, dag_id: str, env: str | None) -> DagSpec | None:
    try:
        if isinstance(target, GitTarget):
            stored = target.get(dag_id, environment=env)
        else:
            stored = target.get(dag_id)
    except StorageError:
        log.info("cannot read the stored spec of %s", dag_id, exc_info=True)
        return None
    return stored.spec if stored is not None else None


def plan_for(
    services: Any,
    principal: Principal | None,
    spec: DagSpec,
    *,
    target: DeployTarget,
    environment: str | None,
    push_mode: GitMode | None = None,
    resolution: Resolution | None = None,
) -> SourcePlan:
    """The source plan of deploying ``spec`` to ``target`` (blocking)."""
    effective = (
        spec.with_environment(environment)
        if environment and environment in spec.environments
        else spec
    )
    existing = existing_file(target, spec.dag_id, environment, push_mode)
    stored = stored_spec(target, spec.dag_id, environment)
    plan = plan_source(
        spec,
        effective=effective,
        existing=existing,
        stored=stored,
        ctx=plan_context(services, principal, target),
        resolution=resolution,
        environment=environment,
    )
    if (
        plan.uses_source_code
        and plan.action in ("preserve", "unchanged")
        and principal is not None
        and not principal.has(P.CODE_RAW)
    ):
        # The file would be the hand-written code sent with the spec: arbitrary Python,
        # like a raw code DAG.
        plan.action = "refused"
        plan.source = None
        plan.options = ["generate"]
        plan.message = (
            "this deploy would write the DAG's hand-written Python file (imported or edited "
            "in the code mode), which needs the AutoAir permission 'code:raw' (for example the "
            "CodeDeployer role); switch the source policy to 'generate' to deploy the generated "
            "file instead"
        )
        plan.refusals.append(SourceIssue(code="permission", message=plan.message))
    return plan


def writes_client_code(
    services: Any, spec: DagSpec, target: DeployTarget, environment: str | None
) -> bool:
    """Would a deploy of ``spec`` write its hand-written ``source.code`` as the DAG file
    (``code:raw``)? Decided like the plan's base selection, without imports; a target that
    cannot be read counts as yes."""
    if not keeps_hand_written_file(spec, getattr(services, "settings", None)):
        return False
    code = spec.source.code if spec.source else None
    if code is None:
        return False
    try:
        existing = existing_file(target, spec.dag_id, environment, None)
    except StorageError:
        return True
    if existing is None:
        return True
    if classify(existing, spec) in ("ejected", "foreign"):
        return False  # the target's file is patched
    return sha256_text(code) not in (existing.source_sha, sha256_text(existing.content))


__all__ = [
    "can_execute",
    "existing_file",
    "plan_context",
    "plan_for",
    "stored_spec",
    "writes_client_code",
]
