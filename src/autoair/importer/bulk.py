"""Bulk import of a DAG repository (a git repository or an uploaded folder).

Every candidate Python file is imported with the single-file importer
(:func:`autoair.importer.python_dag.import_python_file`: Airflow's DagBag in an isolated
subprocess without AutoAir's secrets), with the repository root on ``PYTHONPATH`` so shared
helper modules resolve like they do in a dags folder. Nothing is saved here: the result is a
per-file summary with the converted specs; the caller deploys the ones the user selects (an
existing DAG is only overwritten after explicit confirmation).
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr

from autoair.importer.python_dag import ImportedDag, import_python_file
from autoair.net import tls
from autoair.preserve.service import mark_hand_written

FileStatus = Literal["imported", "no_dag", "excluded", "error", "not_scanned"]
_REF = re.compile(r"^[A-Za-z0-9][\w./-]{0,200}$")


class BulkImportError(ValueError):
    """The source cannot be imported (bad URL/path, clone failed...)."""


class UploadedFile(BaseModel):
    path: str = Field(min_length=1, max_length=1000, description="Repository-relative path.")
    source: str = Field(description="File content.")


class GitSource(BaseModel):
    url: str = Field(max_length=2000, description="https:// URL of the repository.")
    ref: str | None = Field(default=None, max_length=200, description="Branch or tag.")
    subdir: str | None = Field(
        default=None, max_length=1000, description="Only import files below this folder."
    )
    token: SecretStr | None = Field(
        default=None,
        description="Access token of a private repository (sent as an HTTP header, never "
        "stored or logged).",
    )


class BulkDag(ImportedDag):
    exists: bool = Field(default=False, description="A DAG with this id is already stored.")
    stored_etag: str | None = Field(
        default=None, description="Version of the stored DAG (to overwrite it, confirmed)."
    )


class BulkFileResult(BaseModel):
    path: str
    status: FileStatus
    dags: list[BulkDag] = Field(default_factory=list)
    import_errors: list[str] = Field(default_factory=list)
    message: str | None = None


class BulkSummary(BaseModel):
    files: int = 0
    imported_files: int = 0
    dags: int = 0
    convertible: int = 0
    existing: int = 0
    errors: int = 0


class BulkImportResult(BaseModel):
    source: str = Field(description="What was imported (URL@ref or 'upload').")
    files: list[BulkFileResult]
    summary: BulkSummary


def safe_relative(path: str) -> PurePosixPath:
    """A repository-relative path that cannot leave the repository."""
    rel = PurePosixPath(path.replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or any(p in ("", "..") for p in rel.parts):
        raise BulkImportError(f"invalid path {path!r}")
    return rel


def excluded(rel: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(rel, p) for p in patterns)


def looks_like_dag(text: str) -> bool:
    """Airflow's safe-mode heuristic: the file mentions both 'airflow' and 'dag'."""
    low = text.lower()
    return "airflow" in low and "dag" in low


def check_git_url(url: str, allowed_hosts: Sequence[str]) -> str:
    if not allowed_hosts:
        raise BulkImportError("importing from git is disabled (interchange.bulk_git_allowed_hosts)")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise BulkImportError("only https:// git URLs can be imported")
    if parts.username or parts.password:
        raise BulkImportError("put credentials in the token field, not in the URL")
    host = parts.hostname.lower()
    if not any(fnmatch.fnmatchcase(host, p.lower()) for p in allowed_hosts):
        raise BulkImportError(
            f"git host {host!r} is not allowed (interchange.bulk_git_allowed_hosts)"
        )
    return url


@contextmanager
def cloned(source: GitSource, *, allowed_hosts: Sequence[str], timeout: float) -> Iterator[Path]:
    """A shallow clone of ``source`` in a temporary folder (removed afterwards)."""
    url = check_git_url(source.url, allowed_hosts)
    if source.ref is not None and not _REF.match(source.ref):
        raise BulkImportError(f"invalid git ref {source.ref!r}")
    tmp = Path(tempfile.mkdtemp(prefix="autoair-bulk-"))
    try:
        env = dict(os.environ)
        env.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS="echo",
            GIT_ALLOW_PROTOCOL="https",  # also for submodules / redirects
            LC_ALL="C",
        )
        if not tls.trust_env("git"):
            tls.strip_proxy_env(env)
        env.update(tls.git_env())
        config = tls.git_config()
        if source.token is not None:
            config.append(
                ("http.extraHeader", f"Authorization: Bearer {source.token.get_secret_value()}")
            )
        env["GIT_CONFIG_COUNT"] = str(len(config))
        for i, (key, value) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{i}"] = key
            env[f"GIT_CONFIG_VALUE_{i}"] = value
        cmd = ["git", "clone", "--depth", "1", "--single-branch", "--no-tags"]
        if source.ref:
            cmd += ["--branch", source.ref]
        cmd += ["--", url, str(tmp / "repo")]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise BulkImportError(f"git clone timed out after {timeout:g}s") from exc
        except FileNotFoundError as exc:
            raise BulkImportError("git is not installed on the AutoAir server") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            text = " ".join(detail)
            if source.token is not None:
                text = text.replace(source.token.get_secret_value(), "***")
            raise BulkImportError(f"git clone failed: {text}")
        yield tmp / "repo"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def materialized(files: Sequence[UploadedFile], max_chars: int) -> Iterator[Path]:
    """Uploaded files written below a temporary root (paths checked)."""
    tmp = Path(tempfile.mkdtemp(prefix="autoair-bulk-"))
    try:
        for f in files:
            rel = safe_relative(f.path)
            if len(f.source) > max_chars:
                raise BulkImportError(f"{f.path}: larger than {max_chars} characters")
            target = tmp.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.source, encoding="utf-8")
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _candidates(root: Path, subdir: str | None) -> tuple[Path, list[str]]:
    base = root
    if subdir:
        base = root.joinpath(*safe_relative(subdir).parts)
        if not base.is_dir():
            raise BulkImportError(f"folder {subdir!r} does not exist in the repository")
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            full = Path(dirpath) / name
            if name.endswith(".py") and not full.is_symlink():
                out.append(full.relative_to(root).as_posix())
    return base, out


def scan(
    root: Path,
    *,
    label: str,
    subdir: str | None = None,
    exclude: Sequence[str] = (),
    max_files: int = 500,
    max_chars: int = 2_000_000,
    workers: int = 4,
    timeout: float = 90,
    lookup: Callable[[BulkDag], None] | None = None,
) -> BulkImportResult:
    """Import every candidate file below ``root`` (``lookup`` fills ``exists``)."""
    _, paths = _candidates(root, subdir)
    results: dict[str, BulkFileResult] = {}
    todo: list[str] = []
    for rel in paths:
        if excluded(rel, exclude):
            results[rel] = BulkFileResult(path=rel, status="excluded")
            continue
        full = root / rel
        if full.stat().st_size > max_chars * 4:
            results[rel] = BulkFileResult(path=rel, status="error", message="file too large")
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            results[rel] = BulkFileResult(path=rel, status="error", message=str(exc))
            continue
        if len(text) > max_chars:
            results[rel] = BulkFileResult(path=rel, status="error", message="file too large")
        elif not looks_like_dag(text):
            results[rel] = BulkFileResult(path=rel, status="no_dag")
        elif len(todo) >= max_files:
            results[rel] = BulkFileResult(
                path=rel, status="not_scanned", message=f"more than {max_files} files"
            )
        else:
            todo.append(rel)

    def one(rel: str) -> BulkFileResult:
        res = import_python_file(root / rel, timeout=timeout, pythonpath=[root])
        dags = [BulkDag(**d.model_dump()) for d in res.dags]
        if len(dags) == 1 and dags[0].spec is not None:
            # The file is the DAG's hand-written source: deploys preserve it.
            text = (root / rel).read_text(encoding="utf-8")
            dags[0].spec = mark_hand_written(dags[0].spec, text, dags[0].warnings)
        if lookup is not None:
            for d in dags:
                if d.spec is not None:
                    lookup(d)
        status: FileStatus = "imported" if dags else ("error" if res.import_errors else "no_dag")
        return BulkFileResult(path=rel, status=status, dags=dags, import_errors=res.import_errors)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for rel, result in zip(todo, pool.map(one, todo), strict=True):
            results[rel] = result

    files = [results[rel] for rel in sorted(results)]
    summary = BulkSummary(files=len(files))
    for f in files:
        summary.imported_files += f.status == "imported"
        summary.errors += f.status == "error"
        summary.dags += len(f.dags)
        summary.convertible += sum(d.spec is not None for d in f.dags)
        summary.existing += sum(d.exists for d in f.dags)
    return BulkImportResult(source=label, files=files, summary=summary)


__all__ = [
    "BulkDag",
    "BulkFileResult",
    "BulkImportError",
    "BulkImportResult",
    "GitSource",
    "UploadedFile",
    "check_git_url",
    "cloned",
    "materialized",
    "scan",
]
