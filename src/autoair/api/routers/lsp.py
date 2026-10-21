"""Language services: Python (jedi; static analysis only) and shell scripts (ShellCheck)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from autoair.api.deps import ServicesDep
from autoair.lsp import (
    CompletionList,
    HoverInfo,
    PythonContext,
    SignatureList,
    complete,
    hover,
    signatures,
)
from autoair.lsp.shell import ShellLintRequest, ShellLintResult, lint_shell
from autoair.lsp.validate import ValidateRequest, ValidateResult, validate_code

router = APIRouter(prefix="/lsp", tags=["lsp"])


def _check_size(code: str, services: ServicesDep) -> None:
    limit = services.settings.editor.max_source_chars
    if len(code) > limit:
        raise HTTPException(
            status_code=422,
            detail=f"code is larger than {limit} characters (AUTOAIR_EDITOR__MAX_SOURCE_CHARS)",
        )


@router.post("/python/complete", operation_id="pythonComplete")
async def python_complete(body: PythonContext, services: ServicesDep) -> CompletionList:
    _check_size(body.code, services)
    return await run_in_threadpool(complete, body, services.settings.editor.max_completions)


@router.post("/python/hover", operation_id="pythonHover")
async def python_hover(body: PythonContext, services: ServicesDep) -> HoverInfo:
    _check_size(body.code, services)
    return await run_in_threadpool(hover, body)


@router.post("/python/signatures", operation_id="pythonSignatures")
async def python_signatures(body: PythonContext, services: ServicesDep) -> SignatureList:
    _check_size(body.code, services)
    return await run_in_threadpool(signatures, body)


@router.post("/shell/lint", operation_id="shellLint")
async def shell_lint(body: ShellLintRequest, services: ServicesDep) -> ShellLintResult:
    """ShellCheck diagnostics for a bash command/script (Jinja masked). Optional dependency."""
    _check_size(body.code, services)
    return await run_in_threadpool(lint_shell, body)


@router.post("/validate", operation_id="validateCode")
async def validate_code_field(body: ValidateRequest, services: ServicesDep) -> ValidateResult:
    """Syntax/lint diagnostics of a code field with the best checker installed (ast + ruff,
    strict JSON, YAML, bash -n + ShellCheck, sqlglot, gofmt, javac, tsc/esbuild). Nothing
    is executed; the response says which tools ran or are missing."""
    _check_size(body.code, services)
    return await run_in_threadpool(validate_code, body)
