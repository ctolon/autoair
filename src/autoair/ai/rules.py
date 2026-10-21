"""Deterministic checks behind the AI review / leak scan (they also run without an LLM).

* :func:`scan_leaks` - hard-coded secrets, credentials exposed through logs / rendered
  templates / XCom, unbounded resources and missing timeouts.
* :func:`review_rules` - reliability, idempotency and Airflow 3 idioms.

Every finding carries a stable id, a location (``node_id`` / ``field`` / JSON pointer) and,
where the fix is unambiguous, a JSON patch.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from autoair.ai.config import active_ai
from autoair.ai.findings import (
    Category,
    Finding,
    PatchOp,
    Severity,
    dedupe,
    finding_id,
    sort_findings,
    spec_document,
)
from autoair.catalog import OperatorCatalog, OperatorDetail
from autoair.spec import DagSpec, OperatorNode, PythonTaskNode

# --------------------------------------------------------------------------- secret patterns

SECRET_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("private-key", "Private key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
    ("aws-access-key", "AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "aws-secret-key",
        "AWS secret access key",
        re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}"),
    ),
    (
        "github-token",
        "GitHub token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_\w{40,})"),
    ),
    ("gitlab-token", "GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("slack-token", "Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    (
        "slack-webhook",
        "Slack webhook URL",
        re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"),
    ),
    ("google-api-key", "Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe-key", "Stripe live key", re.compile(r"\b[rs]k_live_[0-9a-zA-Z]{24,}")),
    ("anthropic-key", "Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", "OpenAI API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("sendgrid-key", "SendGrid API key", re.compile(r"\bSG\.[\w-]{22}\.[\w-]{43}\b")),
    ("azure-storage-key", "Azure storage key", re.compile(r"AccountKey=[A-Za-z0-9+/=]{40,}")),
    (
        "jwt",
        "JSON Web Token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    (
        "credential-url",
        "Credentials embedded in a URL",
        re.compile(r"\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@{}'\"]+:[^\s/@{}'\"$]{3,}@[^\s/'\"]+"),
    ),
    (
        "secret-assignment",
        "Hard-coded secret",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
            r"client[_-]?secret|private[_-]?key)\b['\"]?\s*[=:]\s*['\"]([^'\"\s{}$<>]{8,})['\"]"
        ),
    ),
)

SENSITIVE_NAME = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api_?key|apikey|access_?key|private_?key|"
    r"credential|auth|bearer|client_secret|conn_?str|connection_string)"
)
#: Parameter names that merely *reference* a secret (ids, files, flags) - not secrets.
SAFE_NAME = re.compile(
    r"(?i)(_conn_id|conn_id|_id$|_ids$|_file$|_path$|_name$|_key_name$|_type$|_field$|"
    r"_mode$|_enabled$|_url$|^auth_type$|token_uri|_var$|_env$|_header$)"
)
PLACEHOLDER = re.compile(
    r"(?i)^(x+|\*+|changeme|change_me|placeholder|your[_-].*|<.*>|\.\.\.|example.*|dummy|test|none|null)$"
)

#: Jinja expressions that render secrets into templated fields (stored and shown in the UI).
RENDERED_SECRET = re.compile(
    r"\{\{[^}]*\b(?:conn\.[\w.]+\.(?:password|extra|extra_dejson)\b|"
    r"var\.(?:value|json)\.\w*(?:secret|password|token|key|cred)\w*)[^}]*\}\}",
    re.IGNORECASE,
)

#: Template variables removed in Airflow 3.
REMOVED_TEMPLATE_VARS = (
    "execution_date",
    "next_execution_date",
    "prev_execution_date",
    "prev_execution_date_success",
    "next_ds",
    "next_ds_nodash",
    "prev_ds",
    "prev_ds_nodash",
    "tomorrow_ds",
    "yesterday_ds",
    "tomorrow_ds_nodash",
    "yesterday_ds_nodash",
)
_REMOVED_TEMPLATE_RE = re.compile(
    r"\{\{[^}]*(?<![\w.])(" + "|".join(REMOVED_TEMPLATE_VARS) + r")\b[^}]*\}\}"
)

SHELL_PARAMS = frozenset({"bash_command", "command", "cmds", "arguments", "ssh_command", "script"})
HTTP_TIMEOUT_CALLS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request"}
)
LOG_CALLS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})


def mask(value: str, keep: int = 4) -> str:
    """Show a short prefix of a secret only."""
    value = value.strip()
    if len(value) <= keep + 2:
        return "***"
    return value[:keep] + "…" + "*" * 6


_INLINE_CREDS = re.compile(r"((?:-u|--user)\s+['\"]?[^\s:'\"]+:)([^\s'\"]+)")


def redact(text: str) -> str:
    """Mask every secret-looking substring (used for all evidence snippets)."""
    for _, _, pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: mask(m.group(0)), text)
    return _INLINE_CREDS.sub(lambda m: m.group(1) + "***", text)


#: ``password=value`` / ``token: value`` / ``Authorization: Bearer x`` in logs and config
#: text (unquoted, so the quoted ``secret-assignment`` pattern does not see them).
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b((?:[a-z0-9]+[_-])*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret|authorization|credentials?))"
    r"(\s*[=:]\s*)((?:bearer|basic|token)\s+)?(['\"]?)(?![$<{*])([^\s'\",;&}]{4,})"
)


def scrub_secrets(text: str) -> str:
    """:func:`redact` plus unquoted ``key=value`` secrets (log lines, YAML/env snippets)."""
    text = _ASSIGNED_SECRET.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}{m.group(4)}***", text
    )
    return redact(text)


# --------------------------------------------------------------------------- traversal


@dataclass(frozen=True)
class Location:
    pointer: str  # stable form: /nodes/@id/...
    node_id: str | None
    field: str | None
    key: str | None  # name of the dict key holding the value


def _esc(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def iter_strings(document: Mapping[str, Any]) -> Iterator[tuple[Location, str]]:
    """Every string in the spec document with a reveal-able location."""
    skip_top = {"position", "spec_version"}

    def walk(
        value: Any, tokens: list[str], node_id: str | None, field: str | None, key: str | None
    ) -> Iterator[tuple[Location, str]]:
        if isinstance(value, str):
            yield Location("/" + "/".join(tokens), node_id, field, key), value
        elif isinstance(value, Mapping):
            for k, v in value.items():
                if k in ("position", "label", "icon"):
                    continue
                f = field
                in_params = field in ("params", "config") and len(tokens) == 3
                if node_id is not None and (field is None or in_params):
                    f = k
                yield from walk(v, [*tokens, _esc(str(k))], node_id, f, str(k))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                yield from walk(v, [*tokens, str(i)], node_id, field, key)

    for top, value in document.items():
        if top in skip_top:
            continue
        if top == "nodes" and isinstance(value, list):
            for node in value:
                if not isinstance(node, Mapping):
                    continue
                nid = str(node.get("id"))
                for k, v in node.items():
                    if k in ("position", "label", "icon", "id", "kind"):
                        continue
                    yield from walk(v, ["nodes", f"@{_esc(nid)}", _esc(k)], nid, k, k)
        else:
            yield from walk(value, [_esc(top)], None, top, top)


# --------------------------------------------------------------------------- helpers


class _Collector:
    def __init__(self) -> None:
        self.items: list[Finding] = []

    def add(
        self,
        rule: str,
        severity: Severity,
        category: Category,
        title: str,
        message: str,
        *,
        node_id: str | None = None,
        field: str | None = None,
        path: str | None = None,
        evidence: str | None = None,
        patch: list[PatchOp] | None = None,
    ) -> None:
        self.items.append(
            Finding(
                id=finding_id(rule, node_id, field, path, title),
                severity=severity,
                category=category,
                title=title,
                message=message,
                node_id=node_id,
                field=field,
                path=path,
                rule=rule,
                source="rule",
                evidence=redact(evidence) if evidence else None,
                patch=patch or [],
            )
        )


def _node_path(node_id: str, *parts: str) -> str:
    return "/".join(["", "nodes", f"@{_esc(node_id)}", *(_esc(p) for p in parts)])


def _python_sources(spec: DagSpec, document: Mapping[str, Any]) -> Iterator[tuple[Location, str]]:
    """Python code in the spec: python_task bodies, inline functions, .py file assets."""
    for node in spec.nodes:
        if isinstance(node, PythonTaskNode):
            yield Location(_node_path(node.id, "code"), node.id, "code", "code"), node.code
    for loc, value in iter_strings(document):
        inline_fn = loc.key == "source" and "def " in value
        py_file = loc.key == "content" and value.lstrip().startswith(("import ", "from ", "def "))
        if inline_fn or py_file:
            yield loc, value


def _shell_sources(document: Mapping[str, Any]) -> Iterator[tuple[Location, str]]:
    for loc, value in iter_strings(document):
        if loc.node_id is None:
            continue
        name = loc.key or ""
        script = loc.key == "content" and value.lstrip().startswith(("#!", "set -"))
        if name in SHELL_PARAMS or (loc.field and loc.field in SHELL_PARAMS) or script:
            yield loc, value


def _parse_python(code: str) -> ast.Module | None:
    """Parse a function body or a module."""
    for candidate in (
        code,
        "def __autoair_body__():\n" + "\n".join("    " + ln for ln in code.splitlines()),
    ):
        try:
            return ast.parse(candidate)
        except SyntaxError:
            continue
    return None


def _names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and len(sub.value) < 60:
            out.add(sub.value)
    return out


def _sensitive(names: set[str]) -> str | None:
    for n in sorted(names):
        if SENSITIVE_NAME.search(n) and not SAFE_NAME.search(n):
            return n
    return None


def _call_name(call: ast.Call) -> tuple[str | None, str | None]:
    fn = call.func
    if isinstance(fn, ast.Attribute):
        base = fn.value
        owner = (
            base.id
            if isinstance(base, ast.Name)
            else (base.attr if isinstance(base, ast.Attribute) else None)
        )
        return owner, fn.attr
    if isinstance(fn, ast.Name):
        return None, fn.id
    return None, None


# --------------------------------------------------------------------------- leak scan


def _scan_secrets(document: Mapping[str, Any], out: _Collector) -> None:
    for loc, value in iter_strings(document):
        if "{{" in value and "}}" in value and RENDERED_SECRET.search(value):
            out.add(
                "rendered-secret",
                "high",
                "secret",
                "Secret rendered into a templated field",
                "This Jinja expression renders a connection password/extra or a secret "
                "variable into the task's rendered template fields, which Airflow stores in "
                "the metadata DB and shows in the UI (masking is best-effort). Pass the "
                "connection id to the operator/hook instead, or read the secret inside the "
                "task at runtime.",
                node_id=loc.node_id,
                field=loc.field,
                path=loc.pointer,
                evidence=RENDERED_SECRET.search(value).group(0)[:120],  # type: ignore[union-attr]
            )
        for rule, label, pattern in SECRET_PATTERNS:
            match = pattern.search(value)
            if match is None:
                continue
            secret = match.group(1) if match.groups() else match.group(0)
            if PLACEHOLDER.match(secret) or "{{" in match.group(0):
                continue
            out.add(
                f"secret:{rule}",
                "critical",
                "secret",
                f"{label} hard-coded in the DAG",
                "Secrets in a DAG end up in git, the generated DAG file, the DAG source view "
                "and rendered fields. Revoke/rotate this credential, then store it in an "
                "Airflow connection or a secrets backend and reference it by conn_id / "
                "{{ var.value.<name> }} read at runtime.",
                node_id=loc.node_id,
                field=loc.field,
                path=loc.pointer,
                evidence=mask(match.group(0)),
            )
            break
        else:
            key = loc.key or ""
            if (
                SENSITIVE_NAME.search(key)
                and not SAFE_NAME.search(key)
                and len(value) >= 6
                and " " not in value.strip()
                and "{{" not in value
                and not PLACEHOLDER.match(value)
                and loc.field not in ("doc_md", "description", "task_display_name")
            ):
                out.add(
                    "secret:param",
                    "high",
                    "secret",
                    f"Literal value for sensitive field '{key}'",
                    f"'{key}' looks like a credential and holds a literal value. Use an "
                    "Airflow connection (conn_id parameter) or a secret variable read at "
                    "runtime instead.",
                    node_id=loc.node_id,
                    field=loc.field,
                    path=loc.pointer,
                    evidence=mask(value),
                )


def _scan_python(spec: DagSpec, document: Mapping[str, Any], out: _Collector) -> None:
    for loc, code in _python_sources(spec, document):
        tree = _parse_python(code)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                owner, name = _call_name(node)
                kwargs = {k.arg for k in node.keywords if k.arg}
                args_names: set[str] = set()
                for a in [*node.args, *(k.value for k in node.keywords)]:
                    args_names |= _names(a)
                is_log = name == "print" or (
                    name in LOG_CALLS and owner in ("log", "logger", "logging", "self", "LOG")
                )
                hit = _sensitive(args_names) if is_log else None
                if is_log and ("environ" in args_names or hit):
                    out.add(
                        "log-secret",
                        "high",
                        "secret",
                        "Credential written to the task log",
                        f"`{ast.unparse(node)[:100]}` logs a credential"
                        + (f" ({hit})" if hit else " (the whole environment)")
                        + ". Task logs are readable by every user with log access; remove "
                        "the call or log a redacted value.",
                        node_id=loc.node_id,
                        field=loc.field,
                        path=loc.pointer,
                        evidence=ast.unparse(node)[:120],
                    )
                if (
                    owner in ("requests", "session", "s", "http", "client")
                    and name in HTTP_TIMEOUT_CALLS
                    and owner != "client"
                    and "timeout" not in kwargs
                ) or (name == "urlopen" and "timeout" not in kwargs and len(node.args) < 3):
                    out.add(
                        "http-no-timeout",
                        "medium",
                        "reliability",
                        "HTTP call without a timeout",
                        f"`{ast.unparse(node)[:80]}` can hang forever and hold a worker slot. "
                        "Pass timeout=(connect, read), e.g. timeout=(10, 300).",
                        node_id=loc.node_id,
                        field=loc.field,
                        path=loc.pointer,
                        evidence=ast.unparse(node)[:120],
                    )
                if (
                    owner == "subprocess"
                    and name in ("run", "call", "check_call", "check_output")
                    and "timeout" not in kwargs
                ):
                    out.add(
                        "subprocess-no-timeout",
                        "low",
                        "reliability",
                        "Subprocess without a timeout",
                        "Pass timeout= so a stuck child process cannot block the task forever.",
                        node_id=loc.node_id,
                        field=loc.field,
                        path=loc.pointer,
                        evidence=ast.unparse(node)[:120],
                    )
            elif isinstance(node, ast.Return) and node.value is not None:
                hit = _sensitive(_names(node.value))
                if hit and not isinstance(node.value, ast.Constant):
                    out.add(
                        "xcom-secret",
                        "high",
                        "secret",
                        "Credential returned to XCom",
                        f"The return value contains '{hit}'. Returned values are pushed to XCom, "
                        "stored in the metadata DB and visible in the UI. Return a reference "
                        "(connection id, object key) instead.",
                        node_id=loc.node_id,
                        field=loc.field,
                        path=loc.pointer,
                        evidence=ast.unparse(node)[:120],
                    )
            elif isinstance(node, ast.While) and (
                isinstance(node.test, ast.Constant) and node.test.value is True
            ):
                has_exit = any(
                    isinstance(n, (ast.Break, ast.Return, ast.Raise)) for n in ast.walk(node)
                )
                if not has_exit:
                    out.add(
                        "unbounded-loop",
                        "medium",
                        "reliability",
                        "Infinite loop without an exit",
                        "`while True` without break/return never finishes; use a sensor or "
                        "a bounded retry loop.",
                        node_id=loc.node_id,
                        field=loc.field,
                        path=loc.pointer,
                    )


_CURL_TIMEOUT = re.compile(r"(--max-time|(?<!\S)-m\s|--connect-timeout)")
_WGET_TIMEOUT = re.compile(r"(--timeout|(?<!\S)-T\s)")
_SHELL_ECHO_SECRET = re.compile(
    r"\b(?:echo|printf|print)\b[^\n|;&]*\$\{?\w*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|"
    r"ACCESS_?KEY|PRIVATE_?KEY)\w*",
    re.IGNORECASE,
)
_CURL_USER = re.compile(r"(?:curl[^\n]*\s(?:-u|--user)\s+['\"]?[^\s:'\"]+:[^\s'\"$]{3,})")


def _scan_shell(document: Mapping[str, Any], out: _Collector) -> None:
    for loc, cmd in _shell_sources(document):
        if re.search(r"\bcurl\b", cmd) and not _CURL_TIMEOUT.search(cmd):
            out.add(
                "curl-no-timeout",
                "medium",
                "reliability",
                "curl without a timeout",
                "Add --max-time (and --retry) so a hanging endpoint cannot block the task.",
                node_id=loc.node_id,
                field=loc.field,
                path=loc.pointer,
                evidence=cmd[:120],
            )
        if re.search(r"\bwget\b", cmd) and not _WGET_TIMEOUT.search(cmd):
            out.add(
                "wget-no-timeout",
                "low",
                "reliability",
                "wget without a timeout",
                "Add --timeout=<seconds>.",
                node_id=loc.node_id,
                field=loc.field,
                path=loc.pointer,
                evidence=cmd[:120],
            )
        if _SHELL_ECHO_SECRET.search(cmd):
            out.add(
                "shell-echo-secret",
                "high",
                "secret",
                "Secret printed to the task log",
                "The command echoes a secret environment variable; task logs are readable "
                "by every user with log access.",
                node_id=loc.node_id,
                field=loc.field,
                path=loc.pointer,
                evidence=_SHELL_ECHO_SECRET.search(cmd).group(0)[:120],  # type: ignore[union-attr]
            )
        if re.search(r"(^|\n|;|&&)\s*set\s+-[a-z]*x", cmd) or re.search(
            r"\bbash\s+-[a-z]*x\b", cmd
        ):
            out.add(
                "shell-xtrace",
                "medium",
                "secret",
                "Shell tracing (set -x) can leak secrets",
                "xtrace prints every expanded command - including credentials passed as "
                "arguments or env vars - to the task log.",
                node_id=loc.node_id,
                field=loc.field,
                path=loc.pointer,
            )
        if _CURL_USER.search(cmd):
            out.add(
                "shell-inline-credentials",
                "critical",
                "secret",
                "Credentials passed inline to curl",
                "user:password on the command line is stored in the DAG and visible in "
                "process lists and logs; use a connection and a .netrc/header from a secret.",
                node_id=loc.node_id,
                field=loc.field,
                path=loc.pointer,
                evidence=mask(_CURL_USER.search(cmd).group(0), 12),  # type: ignore[union-attr]
            )


def _schema_props(detail: OperatorDetail | None) -> Mapping[str, Any]:
    if detail is None:
        return {}
    props = detail.params_schema.get("properties")
    return props if isinstance(props, Mapping) else {}


def _scan_resources(spec: DagSpec, catalog: OperatorCatalog | None, out: _Collector) -> None:
    tasks = spec.task_nodes(include_disabled=False)
    if not tasks:
        return
    dag_timeout = spec.default_args.execution_timeout_seconds
    task_timeout = active_ai().assist.default_task_timeout_seconds
    without = [t for t in tasks if t.execution_timeout_seconds is None]
    if dag_timeout is None and without:
        out.add(
            "no-execution-timeout",
            "medium",
            "reliability",
            "No execution timeout",
            f"{len(without)} task(s) can run forever (Airflow has no default "
            "execution_timeout): a hung task holds a worker slot and blocks the DAG run. "
            f"The patch sets default_args.execution_timeout to {task_timeout // 60} "
            "minutes; tune it per task (Optimise uses run history).",
            field="default_args.execution_timeout_seconds",
            path="/default_args/execution_timeout_seconds",
            patch=[
                PatchOp(
                    op="add",
                    path="/default_args/execution_timeout_seconds",
                    value=task_timeout,
                )
            ],
        )
    if spec.schedule.type != "none" and spec.dagrun_timeout_seconds is None and len(tasks) > 1:
        out.add(
            "no-dagrun-timeout",
            "low",
            "reliability",
            "No DAG run timeout",
            "Without dagrun_timeout a stuck run stays 'running' and, with max_active_runs, "
            "blocks later runs. Set it to a bit more than the expected run duration.",
            field="dagrun_timeout_seconds",
            path="/dagrun_timeout_seconds",
        )
    if spec.catchup and spec.max_active_runs is None:
        out.add(
            "catchup-unbounded",
            "medium",
            "performance",
            "catchup without max_active_runs",
            "catchup=True schedules every missed interval since start_date at once; cap "
            "concurrency with max_active_runs.",
            field="max_active_runs",
            path="/max_active_runs",
            patch=[PatchOp(op="add", path="/max_active_runs", value=1)],
        )
    if catalog is None:
        return
    for task in tasks:
        if task.mapping is not None and task.max_active_tis_per_dagrun is None:
            out.add(
                "mapped-unbounded",
                "low",
                "performance",
                "Unbounded dynamic task mapping",
                "A mapped task may fan out to thousands of instances at once; set "
                "max_active_tis_per_dagrun (or a pool) to protect the target system.",
                node_id=task.id,
                field="max_active_tis_per_dagrun",
                path=_node_path(task.id, "max_active_tis_per_dagrun"),
                patch=[
                    PatchOp(
                        op="add", path=_node_path(task.id, "max_active_tis_per_dagrun"), value=16
                    )
                ],
            )
        if not isinstance(task, OperatorNode):
            continue
        detail = catalog.get(task.operator)
        props = _schema_props(detail)
        if detail is not None and detail.is_sensor:
            if "timeout" in props and "timeout" not in task.params:
                out.add(
                    "sensor-timeout",
                    "medium",
                    "reliability",
                    "Sensor uses the 7-day default timeout",
                    "Sensors wait up to 7 days by default. Set a timeout that matches how long "
                    "the data can realistically be late.",
                    node_id=task.id,
                    field="timeout",
                    path=_node_path(task.id, "params", "timeout"),
                    patch=[
                        PatchOp(
                            op="add",
                            path=_node_path(task.id, "params", "timeout"),
                            value=active_ai().assist.default_sensor_timeout_seconds,
                        )
                    ],
                )
            if detail.deferrable_param and task.params.get("deferrable") is not True:
                out.add(
                    "sensor-deferrable",
                    "medium",
                    "performance",
                    "Sensor occupies a worker slot while waiting",
                    "This sensor supports deferrable mode: waiting moves to the triggerer and "
                    "frees the worker slot.",
                    node_id=task.id,
                    field="deferrable",
                    path=_node_path(task.id, "params", "deferrable"),
                    patch=[
                        PatchOp(
                            op="add", path=_node_path(task.id, "params", "deferrable"), value=True
                        )
                    ],
                )
            elif "mode" in props and task.params.get("mode") in (None, "poke"):
                poke = task.params.get("poke_interval")
                if poke is None or (isinstance(poke, (int, float)) and poke < 300):
                    out.add(
                        "sensor-poke",
                        "low",
                        "performance",
                        "Sensor in poke mode",
                        "Poke mode keeps a worker slot for the whole wait; use "
                        "mode='reschedule' for waits longer than a few minutes.",
                        node_id=task.id,
                        field="mode",
                        path=_node_path(task.id, "params", "mode"),
                        patch=[
                            PatchOp(
                                op="add",
                                path=_node_path(task.id, "params", "mode"),
                                value="reschedule",
                            )
                        ],
                    )
        if (
            task.operator.endswith("KubernetesPodOperator")
            and "container_resources" in props
            and "container_resources" not in task.params
        ):
            out.add(
                "k8s-no-resources",
                "medium",
                "performance",
                "Pod without resource requests/limits",
                "Without container_resources the pod can starve or be starved by other "
                "workloads; set CPU/memory requests and limits.",
                node_id=task.id,
                field="container_resources",
                path=_node_path(task.id, "params", "container_resources"),
            )


def resource_findings(spec: DagSpec, catalog: OperatorCatalog | None = None) -> list[Finding]:
    """Timeouts, sensor modes and concurrency limits only (used by Optimise)."""
    out = _Collector()
    _scan_resources(spec, catalog, out)
    return sort_findings(dedupe(out.items))


def scan_leaks(spec: DagSpec, catalog: OperatorCatalog | None = None) -> list[Finding]:
    """Secrets, credential exposure (logs, rendered templates, XCom), unbounded resources."""
    document = spec_document(spec)
    out = _Collector()
    _scan_secrets(document, out)
    _scan_python(spec, document, out)
    _scan_shell(document, out)
    _scan_resources(spec, catalog, out)
    return sort_findings(dedupe(out.items))


# --------------------------------------------------------------------------- review rules

_NOW_SQL = re.compile(r"(?i)\b(?:now\(\)|getdate\(\)|current_date\b|current_timestamp\b|sysdate\b)")
_NOW_SHELL = re.compile(r"\$\(date\b|`date\b")
_NOW_PY = re.compile(r"\b(datetime\.now|datetime\.utcnow|date\.today|time\.time)\(")
_LEGACY_IMPORTS = re.compile(
    r"^\s*(from|import)\s+airflow\.(operators|sensors|hooks)\.(bash|python|empty|dummy|"
    r"trigger_dagrun|datetime|weekday|time_delta|filesystem|external_task)\b",
    re.MULTILINE,
)
_DB_ACCESS = re.compile(
    r"\b(create_session|provide_session|settings\.Session|from airflow\.settings import Session)\b"
)
_MODELS_IMPORT = re.compile(r"from airflow\.models(\.\w+)? import .*\b(Variable|Connection)\b")


def review_rules(spec: DagSpec, catalog: OperatorCatalog | None = None) -> list[Finding]:
    """Reliability, idempotency, maintainability and Airflow 3 checks."""
    out = _Collector()
    document = spec_document(spec)
    tasks = spec.task_nodes(include_disabled=False)
    da = spec.default_args

    if tasks and da.retries is None and all(t.retries is None for t in tasks):
        out.add(
            "no-retries",
            "medium",
            "reliability",
            "No retries configured",
            "Transient failures (network blips, worker restarts, API rate limits) fail the "
            "whole run. Retry twice with a delay by default; turn retries off per task for "
            "non-idempotent steps.",
            field="default_args.retries",
            path="/default_args/retries",
            patch=[
                PatchOp(op="add", path="/default_args/retries", value=2),
                PatchOp(op="add", path="/default_args/retry_delay_seconds", value=300),
            ],
        )
    for task in tasks:
        if (
            (task.retries or 0) > 0
            and task.retry_delay_seconds is None
            and da.retry_delay_seconds is None
        ):
            out.add(
                "retry-no-delay",
                "info",
                "reliability",
                "Retries use the default 5 minute delay",
                "Consider retry_exponential_backoff for flaky external systems.",
                node_id=task.id,
                field="retry_delay_seconds",
                path=_node_path(task.id, "retry_delay_seconds"),
            )
        if task.pool_slots and not task.pool:
            out.add(
                "pool-slots-no-pool",
                "low",
                "correctness",
                "pool_slots without a pool",
                "pool_slots only matters together with pool.",
                node_id=task.id,
                field="pool",
                path=_node_path(task.id, "pool"),
            )
    if tasks and not da.owner and not any(t.owner for t in tasks):
        out.add(
            "no-owner",
            "low",
            "maintainability",
            "No owner",
            "Set default_args.owner (team or on-call alias) so failures reach someone.",
            field="default_args.owner",
            path="/default_args/owner",
        )
    if not spec.tags:
        out.add(
            "no-tags",
            "info",
            "maintainability",
            "No tags",
            "Tags make the DAG discoverable and filterable in the Airflow UI.",
            field="tags",
            path="/tags",
        )
    if not spec.description and not spec.doc_md:
        out.add(
            "no-docs",
            "low",
            "documentation",
            "Undocumented DAG",
            "Add a description and doc_md (AI > Write docs) so on-call engineers know what "
            "the DAG does and what to do when it fails.",
            field="doc_md",
            path="/doc_md",
        )
    if spec.schedule.type != "none" and not spec.deadlines and len(tasks) > 1:
        out.add(
            "no-deadline",
            "info",
            "airflow3",
            "No deadline alert",
            "Airflow 3 replaces SLAs with Deadline Alerts: alert when a run is not finished "
            "by a time relative to its logical date / queued time.",
            field="deadlines",
            path="/deadlines",
        )
    if da.email_on_failure and not da.email:
        out.add(
            "email-no-recipient",
            "low",
            "correctness",
            "email_on_failure without recipients",
            "Set default_args.email or use an on_failure notifier.",
            field="default_args.email",
            path="/default_args/email",
        )

    if catalog is not None:
        for task in tasks:
            if isinstance(task, OperatorNode):
                detail = catalog.get(task.operator)
                if detail is not None and detail.deprecated:
                    out.add(
                        "deprecated-operator",
                        "medium",
                        "airflow3",
                        f"Deprecated operator {detail.class_name}",
                        "The provider marks this operator as deprecated; it will be removed. "
                        "Check its docstring for the replacement.",
                        node_id=task.id,
                        field="operator",
                        path=_node_path(task.id, "operator"),
                    )
                if "sla" in task.params:
                    out.add(
                        "sla-removed",
                        "high",
                        "airflow3",
                        "SLA is removed in Airflow 3",
                        "Use DAG deadlines (Deadline Alerts) instead of the task-level sla.",
                        node_id=task.id,
                        field="sla",
                        path=_node_path(task.id, "params", "sla"),
                        patch=[PatchOp(op="remove", path=_node_path(task.id, "params", "sla"))],
                    )

    for loc, value in iter_strings(document):
        if loc.field in ("doc_md", "description"):
            continue
        removed = _REMOVED_TEMPLATE_RE.search(value)
        if removed:
            name = removed.group(1)
            hint = {
                "execution_date": "logical_date",
                "next_ds": "data_interval_end | ds",
                "prev_ds": "prev_data_interval_start_success | ds",
            }.get(name, "data_interval_start / data_interval_end")
            out.add(
                "airflow3-template",
                "high",
                "airflow3",
                f"Template variable '{name}' was removed in Airflow 3",
                f"Rendering fails at runtime. Use {hint} instead.",
                node_id=loc.node_id,
                field=loc.field,
                path=loc.pointer,
                evidence=removed.group(0)[:120],
            )
        if (
            loc.node_id is not None
            and loc.key in ("sql", "bash_command")
            and (_NOW_SQL.search(value) or _NOW_SHELL.search(value))
        ):
            out.add(
                "non-idempotent-time",
                "medium",
                "idempotency",
                "Uses the wall clock instead of the run's data interval",
                "Re-runs and backfills process 'now' instead of the run's interval, so results "
                "differ per attempt. Use {{ data_interval_start }} / {{ ds }} templates.",
                node_id=loc.node_id,
                field=loc.field,
                path=loc.pointer,
            )

    for loc, code in _python_sources(spec, document):
        if _NOW_PY.search(code):
            out.add(
                "non-idempotent-time-py",
                "medium",
                "idempotency",
                "Python task uses the wall clock",
                "Use the run's data interval (get_current_context()['data_interval_start'] or "
                "a templated argument) so re-runs and backfills are reproducible.",
                node_id=loc.node_id,
                field=loc.field,
                path=loc.pointer,
            )
        if _DB_ACCESS.search(code):
            out.add(
                "airflow3-db-access",
                "high",
                "airflow3",
                "Direct metadata DB access is not allowed in Airflow 3 tasks",
                "Airflow 3 workers have no database access; use the Task SDK / REST API.",
                node_id=loc.node_id,
                field=loc.field,
                path=loc.pointer,
            )
        if _MODELS_IMPORT.search(code):
            out.add(
                "airflow3-sdk-import",
                "medium",
                "airflow3",
                "Import Variable/Connection from airflow.sdk",
                "In Airflow 3 tasks use `from airflow.sdk import Variable, Connection`; "
                "airflow.models goes through the database.",
                node_id=loc.node_id,
                field=loc.field,
                path=loc.pointer,
            )
        if _LEGACY_IMPORTS.search(code):
            out.add(
                "airflow3-legacy-import",
                "medium",
                "airflow3",
                "Legacy core operator import",
                "Core operators moved to airflow.providers.standard in Airflow 3.",
                node_id=loc.node_id,
                field=loc.field,
                path=loc.pointer,
            )
    return sort_findings(dedupe(out.items))
