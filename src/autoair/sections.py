"""Configuration sections for operational limits, patterns and defaults.

The triage of every literal (made configurable or kept, with the reason) is in
scripts/hardcoded_decisions.json; ``scripts/scan_hardcoded.py --write`` renders it as a report.

Each section is a nested model of :class:`~autoair.settings.Settings`
(``AUTOAIR_<SECTION>__<FIELD>`` / ``[autoair.<section>]``). Policy *patterns* follow one rule:

* ``<name>_pattern`` (or a list such as ``skip_dirs``) holds the built-in value and can be
  **overridden** completely (an empty string disables a pattern);
* ``<name>_patterns_extra`` / ``<name>_extra`` **extends** it — extra patterns are combined
  with the built-in one as the field's description says (any match, or all must match).

Code that has no :class:`Settings` at hand (deep helpers, subprocess helpers) reads the
sections of the running process with :func:`active`; :func:`activate` is called when the API
services or a CLI command are built.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator

from autoair.policy_rules import OpaOptions, PolicyRule, unique_rule_ids

_S = TypeVar("_S", bound=BaseModel)


# --------------------------------------------------------------------------- patterns


def compile_patterns(
    base: str | None, extra: Iterable[str] = (), flags: int = 0
) -> tuple[re.Pattern[str], ...]:
    """The built-in/overridden pattern plus the extra ones (empty strings are skipped)."""
    return tuple(re.compile(p, flags) for p in (base, *extra) if p)


def any_search(patterns: Sequence[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def _check_regex(value: Any) -> Any:
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, str) and item:
            try:
                re.compile(item)
            except re.error as exc:
                raise ValueError(f"invalid regular expression {item!r}: {exc}") from exc
    return value


class _Section(BaseModel):
    """Base of the sections: unknown keys are errors; pattern fields must compile."""

    model_config = ConfigDict(extra="forbid")

    @field_validator("*")
    @classmethod
    def _valid_regex(cls, value: Any, info: ValidationInfo) -> Any:
        name = info.field_name or ""
        if name.endswith(("_pattern", "_patterns_extra")):
            return _check_regex(value)
        return value


# --------------------------------------------------------------------------- catalog discovery

DEFAULT_SKIP_DIRS = [
    "__pycache__",
    ".git",
    ".hg",
    ".venv",
    "venv",
    ".tox",
    ".nox",
    "node_modules",
    "site-packages",
    ".autoair",
    ".ipynb_checkpoints",
]
DEFAULT_SKIP_MODULE = (
    r"(^|\.)(tests?|example_dags|system|migrations|_vendor|decorators)(\.|$)|(^|\.)conftest$"
)
DEFAULT_SKIP_CLASS = r"Decorated(Sensor)?Operator$"
DEFAULT_CLASS_HINT = r"Operator|Sensor|Notifier"
DEFAULT_EXCLUDE_DISTRIBUTIONS = [
    "apache-airflow",
    "apache-airflow-core",
    "apache-airflow-task-sdk",
    "autoair",
]


class DiscoverySettings(_Section):
    """Operator discovery filters (catalog build). Changing them invalidates the catalog cache."""

    max_file_bytes: int = Field(
        default=2_000_000, ge=1, description="Python files larger than this are not scanned."
    )
    skip_dirs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SKIP_DIRS),
        description="Directory names never walked in custom operator folders/packages "
        "(dot-directories are always skipped). Overrides the built-in list.",
    )
    skip_dirs_extra: list[str] = Field(
        default_factory=list, description="Directory names skipped in addition to skip_dirs."
    )
    skip_module_pattern: str = Field(
        default=DEFAULT_SKIP_MODULE,
        description="Regex over dotted module names that are never scanned (tests, examples, "
        "vendored code…). Overrides the built-in pattern; empty disables it.",
    )
    skip_module_patterns_extra: list[str] = Field(
        default_factory=list,
        description="More module-name regexes; a module matching any pattern is skipped.",
    )
    skip_class_pattern: str = Field(
        default=DEFAULT_SKIP_CLASS,
        description="Regex over class names left out of the palette (internal TaskFlow "
        "implementation classes). Overrides the built-in pattern; empty disables it.",
    )
    skip_class_patterns_extra: list[str] = Field(
        default_factory=list,
        description="More class-name regexes; a class matching any pattern is left out.",
    )
    class_hint_pattern: str = Field(
        default=DEFAULT_CLASS_HINT,
        description="Pre-filter: a file is only parsed when it defines a class and its text "
        "matches this regex. Overrides the built-in pattern; empty disables the pre-filter "
        "(every file defining a class is parsed).",
    )
    class_hint_patterns_extra: list[str] = Field(
        default_factory=list,
        description="More pre-filter regexes (e.g. 'Task' for task classes named *Task); a "
        "file matching any pattern is parsed.",
    )
    exclude_distributions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_DISTRIBUTIONS),
        description="fnmatch patterns of distribution names (normalised, e.g. "
        "'apache-airflow-core') never scanned as operator libraries. Overrides the built-in "
        "list.",
    )
    exclude_distributions_extra: list[str] = Field(
        default_factory=list,
        description="More distribution name patterns never scanned (e.g. 'acme-airflow-*').",
    )
    parallel_threshold: int = Field(
        default=300, ge=1, description="Modules to parse before parsing in parallel processes."
    )
    workers: int | None = Field(
        default=None,
        ge=1,
        description="Parallel parser processes (default: min(4, CPU count)).",
    )
    cache_max_files: int = Field(
        default=8, ge=1, description="Catalog cache files kept in the cache directory."
    )
    probe_timeout: float = Field(
        default=120, gt=0, description="Seconds the import probe of operator modules may take."
    )

    def skip_dir_names(self) -> frozenset[str]:
        return frozenset([*self.skip_dirs, *self.skip_dirs_extra])

    def fingerprint(self) -> dict[str, Any]:
        """What changes the catalog content (part of the cache key)."""
        return self.model_dump(
            exclude={"parallel_threshold", "workers", "cache_max_files", "probe_timeout"}
        )


# --------------------------------------------------------------------------- catalog presentation

# A constructor parameter whose name matches is a connection id (connection picker).
DEFAULT_CONNECTION_PARAM_PATTERNS = [r"(^|_)conn(ection)?_id$"]
ParamWidgetName = Literal[
    "sql",
    "bash",
    "python",
    "json",
    "yaml",
    "markdown",
    "connection",
    "variable",
    "pool",
    "enum",
    "secret",
    "text",
]


def _compile_regex(value: str, where: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"{where}: invalid regular expression {value!r}: {exc}") from exc
    return value


class ConnectionParamPattern(_Section):
    """A connection-parameter name regex, optionally tied to a connection type."""

    pattern: str = Field(description="Regex searched in the parameter name.")
    conn_type: str | None = Field(
        default=None,
        description="Connection type offered by the picker for matching params (e.g. "
        "'postgres'); unset: inferred from the operator's hooks as usual.",
    )

    @field_validator("pattern")
    @classmethod
    def _regex(cls, value: str) -> str:
        return _compile_regex(value, "connection parameter pattern")


ConnectionPatternEntry = str | ConnectionParamPattern


class GroupRule(_Section):
    """Palette group of the operators matching a regex."""

    match: str = Field(
        description="Regex searched in the operator class path, its module and its "
        "distribution/provider name (any match)."
    )
    group: str = Field(min_length=1, max_length=80, description="Palette group title.")
    order: int = Field(
        default=0, description="Sort key of the group (lower first; ties sorted by title)."
    )

    @field_validator("match")
    @classmethod
    def _regex(cls, value: str) -> str:
        return _compile_regex(value, "catalog group match")


class ParamOverride(_Section):
    """How the builder shows one operator parameter (every field optional)."""

    widget: ParamWidgetName | None = Field(
        default=None,
        description="Editor: a code editor (sql, bash, python, json, yaml, markdown), an "
        "Airflow picker (connection, variable, pool), enum (a select of `enum`), secret "
        "(masked input) or text (plain input).",
    )
    enum: list[str | int | float | bool] | None = Field(
        default=None, description="Allowed values (implies the enum widget)."
    )
    conn_type: str | list[str] | None = Field(
        default=None, description="Connection type(s) the connection picker offers."
    )
    label: str | None = Field(default=None, max_length=120, description="Field label.")
    help: str | None = Field(default=None, max_length=2000, description="Help text.")
    required: bool | None = Field(default=None, description="Force the param (not) required.")
    hidden: bool | None = Field(
        default=None, description="Hide the param from the form (a set value is kept)."
    )
    default: Any = Field(
        default=None,
        description="Default shown as placeholder and used to prefill required params "
        "(only applied when given).",
    )


class CatalogSettings(_Section):
    """How catalog operators are presented: connection-parameter detection, palette groups and
    parameter widgets. Package metadata (``autoair.yaml`` / the ``autoair.operator_metadata``
    entry point) fills the same fields; these settings win over it."""

    connection_param_patterns: list[ConnectionPatternEntry] = Field(
        default_factory=lambda: list[ConnectionPatternEntry](DEFAULT_CONNECTION_PARAM_PATTERNS),
        description="Regexes over constructor parameter names that hold a connection id "
        "(connection picker). An entry is a regex or {pattern, conn_type}. Overrides the "
        "built-in list (the *_conn_id / *_connection_id naming); changing it rebuilds the "
        "catalog.",
    )
    connection_param_patterns_extra: list[ConnectionPatternEntry] = Field(
        default_factory=list,
        description="More connection-parameter regexes (e.g. '_dsn$' or {pattern: "
        "'^warehouse$', conn_type: snowflake}), added to connection_param_patterns.",
    )
    groups: list[GroupRule] = Field(
        default_factory=list,
        description="Palette groups: [{match: regex, group: 'Bifrost', order: 10}] (first "
        "match wins). Operators without a group are listed under their provider/package.",
    )
    default_group_order: int = Field(
        default=100,
        description="Sort key of the palette groups no rule names (provider groups).",
    )
    param_overrides: dict[str, dict[str, ParamOverride]] = Field(
        default_factory=dict,
        description="Parameter widgets per operator: {'<class path fnmatch pattern>': "
        "{'<param>': {widget, enum, conn_type, label, help, required, hidden, default}}}. "
        "Every matching pattern applies, more specific (longer) patterns last.",
    )
    package_metadata: bool = Field(
        default=True,
        description="Read operator metadata shipped by packages (autoair.yaml / autoair.json "
        "at the package root, the autoair.operator_metadata entry point group).",
    )
    metadata_max_bytes: int = Field(
        default=512 * 1024,
        ge=1024,
        le=16 * 1024 * 1024,
        description="Largest package metadata file read.",
    )
    package_icon_max_bytes: int = Field(
        default=64 * 1024,
        ge=1024,
        le=1024 * 1024,
        description="Largest SVG icon a package may ship (sanitised before it is served).",
    )

    @field_validator("connection_param_patterns", "connection_param_patterns_extra")
    @classmethod
    def _patterns(cls, value: list[ConnectionPatternEntry]) -> list[ConnectionPatternEntry]:
        for i, entry in enumerate(value):
            if isinstance(entry, str):
                _compile_regex(entry, f"connection_param_patterns[{i}]")
        return value

    def connection_patterns(self) -> list[ConnectionParamPattern]:
        """The effective connection-parameter patterns (built-in/overridden, then extra)."""
        out: list[ConnectionParamPattern] = []
        for entry in (*self.connection_param_patterns, *self.connection_param_patterns_extra):
            if isinstance(entry, str):
                if entry:
                    out.append(ConnectionParamPattern(pattern=entry))
            elif entry.pattern:
                out.append(entry)
        return out

    def build_fingerprint(self) -> dict[str, Any]:
        """What changes the built catalog content (part of the cache key)."""
        return {
            "connection_patterns": [p.model_dump() for p in self.connection_patterns()],
            "package_metadata": self.package_metadata,
            "metadata_max_bytes": self.metadata_max_bytes,
            "package_icon_max_bytes": self.package_icon_max_bytes,
        }


# --------------------------------------------------------------------------- DAG policy rules

DEFAULT_PRIVILEGED_OPERATORS = [
    "airflow.providers.standard.operators.bash.BashOperator",
    "airflow.providers.standard.sensors.bash.BashSensor",
]
DEFAULT_SENSITIVE_PARAM = (
    r"(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)"
)
DEFAULT_SENSITIVE_PARAM_EXCLUDE = r"(_conn_id|_id|_name|_path|_file|_field|_url|_ref)$|^token_url$"
# Not anchored to the line start: keyword arguments (``Connection(password="...")``) and
# statements after ``;`` carry secrets as often as a plain assignment. The lookbehind starts
# a match only at the beginning of a name, which keeps it linear (bounded quantifiers,
# horizontal whitespace only) and stops ``mypassword`` style names matching mid-word.
DEFAULT_SENSITIVE_ASSIGNMENT = (
    r"""(?i)(?<![\w.\[\]'"])[\w.\[\]'"]{0,120}?"""
    r"""(?:password|passwd|secret|token|api_?key|access_?key)[\w'"\]]{0,120}"""
    r"""[ \t]*=[ \t]*["'][^"'{}\s]{6,}["']"""
)


class PolicyRules(_Section):
    """DAG validation rules (organisation policy), on top of the flat policy switches."""

    privileged_operators: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PRIVILEGED_OPERATORS),
        description="Operator class paths (fnmatch) that execute arbitrary commands: they "
        "need allow_privileged_operators. Overrides the built-in list.",
    )
    privileged_operators_extra: list[str] = Field(
        default_factory=list,
        description="More privileged operators (fnmatch), e.g. "
        "'airflow.providers.ssh.operators.ssh.SSHOperator'.",
    )
    dag_id_pattern: str = Field(
        default="",
        description="Regex every dag_id must match (fullmatch), e.g. '[a-z][a-z0-9_]*'. "
        "Airflow's own rule ([A-Za-z0-9_.-]+, ≤ 250 chars) always applies too.",
    )
    dag_id_patterns_extra: list[str] = Field(
        default_factory=list, description="More regexes every dag_id must match (fullmatch)."
    )
    task_id_pattern: str = Field(
        default="",
        description="Regex every task_id must match (fullmatch). Airflow's own rule always "
        "applies too.",
    )
    task_id_patterns_extra: list[str] = Field(
        default_factory=list, description="More regexes every task_id must match (fullmatch)."
    )
    secret_param_pattern: str = Field(
        default=DEFAULT_SENSITIVE_PARAM,
        description="Regex (case-insensitive) over parameter names whose literal value is "
        "reported as a hard-coded secret. Overrides the built-in pattern; empty disables it.",
    )
    secret_param_patterns_extra: list[str] = Field(
        default_factory=list, description="More secret parameter-name regexes (any match)."
    )
    secret_param_exclude_pattern: str = Field(
        default=DEFAULT_SENSITIVE_PARAM_EXCLUDE,
        description="Regex (case-insensitive) over parameter names that are never secrets "
        "even when secret_param_pattern matches (token_url, *_conn_id…).",
    )
    secret_param_exclude_patterns_extra: list[str] = Field(
        default_factory=list, description="More never-a-secret parameter-name regexes."
    )
    secret_assignment_pattern: str = Field(
        default=DEFAULT_SENSITIVE_ASSIGNMENT,
        description="Regex over inline code / scripts finding secret-looking assignments. "
        "Overrides the built-in pattern; empty disables it.",
    )
    secret_assignment_patterns_extra: list[str] = Field(
        default_factory=list, description="More secret-assignment regexes (any match)."
    )
    jinja_names_extra: list[str] = Field(
        default_factory=list,
        description="Template variables known besides Airflow's context (user_defined_macros, "
        "plugin macros…): no 'unknown template variable' warning for them.",
    )
    rules: list[PolicyRule] = Field(
        default_factory=list,
        description="Declarative rules: a match (environments, teams, dag_ids, tags, schedule, "
        "operators, params) and a check (forbid, min_retries, require_owner, tags_include, "
        "dag_field, task_field, param…) with severity error or warning. See docs/policy.md.",
    )
    opa_url: str | None = Field(
        default=None,
        description="Open Policy Agent decision URL (e.g. http://opa:8181/v1/data/autoair/"
        "violations). Validation POSTs {input: {spec, context}} and reports the decision's "
        "violations. Unset: OPA is not asked.",
    )
    opa_timeout: float = Field(
        default=2.0, gt=0, le=60, description="Seconds to wait for the OPA decision."
    )
    opa_fail_mode: Literal["open", "closed"] = Field(
        default="open",
        description="When OPA cannot be asked: open reports a warning and lets the DAG pass, "
        "closed reports an error (blocks deploys).",
    )
    opa_token: SecretStr | None = Field(
        default=None, description="Bearer token sent to OPA (OPA's authentication=token)."
    )
    opa_headers: dict[str, str] = Field(
        default_factory=dict, description="Extra HTTP headers sent to OPA."
    )
    opa_cache_seconds: float = Field(
        default=5.0,
        ge=0,
        description="Seconds an OPA decision is reused for an identical input (0: always ask).",
    )

    @field_validator("rules")
    @classmethod
    def _unique_rules(cls, value: list[PolicyRule]) -> list[PolicyRule]:
        return unique_rule_ids(value)

    def privileged(self) -> tuple[str, ...]:
        return (*self.privileged_operators, *self.privileged_operators_extra)

    def opa(self) -> OpaOptions | None:
        if not self.opa_url:
            return None
        return OpaOptions(
            url=self.opa_url,
            timeout=self.opa_timeout,
            fail_mode=self.opa_fail_mode,
            token=self.opa_token,
            headers=dict(self.opa_headers),
            cache_seconds=self.opa_cache_seconds,
        )


# --------------------------------------------------------------------------- subprocess env

DEFAULT_SENSITIVE_ENV = r"TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|CREDENTIAL|EXTRA_HEADERS"
DEFAULT_SCRUB_ENV = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"]
# Any variable name (any prefix): Airflow's own secrets (metadata DB URL, Fernet key, JWT and
# API secrets, broker URLs, *_CMD / *_SECRET indirections, connections and variables from the
# environment) and common cloud / tool credentials. Non-secret Airflow configuration
# (AIRFLOW_HOME, AIRFLOW__CORE__DAGS_FOLDER, ...) stays, DagBag needs it.
DEFAULT_CREDENTIAL_ENV = (
    r"^AIRFLOW_(CONN|VAR)_"
    r"|^AIRFLOW__.*(SECRET|FERNET|SQL_ALCHEMY_CONN|PASSWORD|PASSWD|TOKEN|PRIVATE_KEY|"
    r"BROKER_URL|RESULT_BACKEND|_KWARGS$|ENGINE_ARGS|BASIC_AUTH|_DSN$|_CMD$)"
    r"|(^|_)(SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIALS?|"
    r"CONNECTION_STRING|SAS_TOKEN|ACCOUNT_KEY|SESSION_TOKEN|SECURITY_TOKEN|DSN|BASIC_AUTH|"
    r"WEBHOOK|WEBHOOK_URL|BROKER_URL)(_|$)"
    r"|_(PASS|PWD)$"
    r"|^AWS_(ACCESS_KEY_ID|WEB_IDENTITY_TOKEN_FILE|CONTAINER_AUTHORIZATION_TOKEN.*)$"
    r"|^AZURE_(CLIENT_SECRET|CLIENT_CERTIFICATE_.*|FEDERATED_TOKEN_FILE|USERNAME|STORAGE_KEY)$"
    r"|^(GOOGLE_APPLICATION_CREDENTIALS|CLOUDSDK_AUTH_.*|PGPASSWORD|PGPASSFILE|MYSQL_PWD|"
    r"DATABASE_URL)$"
)


class SandboxSettings(_Section):
    """Environment of processes that run DAG code (import checks, Python imports, test runs)."""

    secret_env_pattern: str = Field(
        default=DEFAULT_SENSITIVE_ENV,
        description="Regex (case-insensitive) over AUTOAIR_* variable names removed from the "
        "environment of DAG-code processes. Overrides the built-in pattern.",
    )
    secret_env_patterns_extra: list[str] = Field(
        default_factory=list, description="More AUTOAIR_* name regexes to remove (any match)."
    )
    scrub_env: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SCRUB_ENV),
        description="Variables (any prefix) always removed from DAG-code processes. "
        "Overrides the built-in list.",
    )
    scrub_env_extra: list[str] = Field(
        default_factory=list,
        description="More variables removed, e.g. ['AWS_SECRET_ACCESS_KEY', 'VAULT_TOKEN'].",
    )
    credential_env_pattern: str = Field(
        default=DEFAULT_CREDENTIAL_ENV,
        description="Regex (case-insensitive) over every variable name (any prefix) removed "
        "from DAG-code processes: Airflow's secrets (AIRFLOW__DATABASE__SQL_ALCHEMY_CONN, "
        "AIRFLOW__CORE__FERNET_KEY, AIRFLOW__API_AUTH__JWT_SECRET, any *_CMD or *_SECRET "
        "indirection, AIRFLOW_CONN_*, AIRFLOW_VAR_*) and common cloud credentials (AWS_*, "
        "GOOGLE_APPLICATION_CREDENTIALS, AZURE_*). Non-secret Airflow configuration stays. "
        "Overrides the built-in pattern; empty disables this rule.",
    )
    credential_env_patterns_extra: list[str] = Field(
        default_factory=list,
        description="More regexes over any variable name to remove (any match).",
    )
    env_passthrough: list[str] = Field(
        default_factory=list,
        description="Variable names (fnmatch patterns, case-insensitive) kept for DAG-code "
        "processes although credential_env_pattern matches them, e.g. "
        "['AIRFLOW_VAR_ENVIRONMENT', 'AIRFLOW_CONN_TEST_*'] for DAGs that read a variable at "
        "parse time or task tests that need a connection. AutoAir's own secrets and the "
        "validator key connection are removed regardless.",
    )


# --------------------------------------------------------------------------- task runner


NetworkIsolation = Literal["auto", "required", "off"]


class RunnerSettings(_Section):
    """Test runs started from the builder (task/DAG test)."""

    default_task_timeout: float = Field(
        default=120, gt=0, description="Seconds a task test may run when the request sets none."
    )
    default_dag_timeout: float = Field(
        default=300, gt=0, description="Seconds a DAG test may run when the request sets none."
    )
    max_timeout: float = Field(
        default=600, gt=0, description="Largest timeout (s) a test-run request may ask for."
    )
    max_log_chars: int = Field(
        default=64_000, ge=1_000, description="Characters of task log returned per test run."
    )
    max_value_chars: int = Field(
        default=4_000, ge=100, description="Characters of a returned value/XCom repr shown."
    )
    allow_db_mode: bool = Field(
        default=False,
        description="Allow DAG test runs with mode=db (dag.test()): the run writes a DAG run "
        "to the metadata DB, so the DAG code gets the metadata DB connection and the Fernet "
        "key, i.e. every Airflow connection and variable. Even then only callers holding "
        "admin:config (besides run:test and code:execute) may use it, and mode=auto picks db "
        "only for them; for everyone else auto runs in memory. Read for every request.",
    )
    process_isolation: NetworkIsolation = Field(
        default="auto",
        description="Run each local test run as PID 1 of its own PID, user and mount "
        "namespaces with a fresh /proc, so its code (Python or a BashOperator command) cannot "
        "see other processes, the AutoAir or Airflow server that started it included (Linux "
        "unprivileged user namespaces); the namespace also hides Airflow's secret files and "
        "mounts the DAG folders read-only. auto (default, every mode): when the kernel allows "
        "it (Ubuntu 24.04+ restricts it by default through AppArmor); otherwise the run still "
        "executes with the audit hook (audit_hook), read-only DAG folders and a secret-free "
        "copy of airflow.cfg, its result carries a 'not fully isolated' notice and the "
        "server logs one warning. required: test runs fail without namespaces. off: never. "
        "For isolated test runs on hosts without namespaces, configure validator.url. Runs on "
        "the validator service use validator.run_process_isolation.",
    )
    audit_hook: bool = Field(
        default=True,
        description="A Python audit hook in each local test-run child denies reading other "
        "processes' /proc/<pid> entries (environment, command line, memory) and other test "
        "runs' folders. It does not restrict subprocesses, network or writes (task code needs "
        "them), and it does not see what a subprocess does: process_isolation does.",
    )


# --------------------------------------------------------------------------- validator service

ValidatorFailMode = Literal["closed", "open", "local"]


class ValidatorSettings(_Section):
    """DagBag import checks in a separate validator service (``autoair validator-serve``):
    how the main server reaches it (``url``, key, retries, fail mode) and the limits the
    service applies to the worker processes that import user code."""

    url: str = Field(
        default="",
        description="Base URL of the validator service, e.g. http://autoair-validator:8791. "
        "Empty: import checks run in this process's own warm DagBag pool (dagbag_pool*).",
    )
    secret_conn_id: str = Field(
        default="autoair_validator",
        description="Airflow connection whose password (at least 16 bytes) is the HMAC key "
        "shared by the main server and the validator; it is removed from the environment of "
        "processes running DAG code on both sides. The main server reads it like any Airflow "
        "connection; the "
        "validator reads only the environment variable AIRFLOW_CONN_<CONN_ID> (JSON or URI "
        "form) and never passes it to its workers.",
    )
    key_cache_seconds: float = Field(
        default=60,
        ge=0,
        description="Seconds the HMAC key read from the connection is reused before it is "
        "read again (rotation delay); 0 reads it for every check.",
    )
    request_overhead_seconds: float = Field(
        default=30,
        gt=0,
        description="Seconds added to the import check timeout for the HTTP request to the "
        "validator (queueing, worker start-up, transfer).",
    )
    retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Extra attempts after a connection error or HTTP 429/502/503/504 from the "
        "validator. A request that timed out is not retried.",
    )
    retry_backoff_seconds: float = Field(
        default=0.5,
        ge=0,
        description="Wait before the first retry (s); doubled for every further retry.",
    )
    health_check_interval_seconds: float = Field(
        default=10,
        gt=0,
        description="After an outage (connection error, HTTP 401/403/502/504, invalid answer) "
        "the validator counts as down: checks fail at once and /ready is probed at most this "
        "often (s) until it answers again. A busy validator (429/503) or a timed-out check "
        "fails only that check.",
    )
    health_timeout_seconds: float = Field(
        default=3, gt=0, description="Timeout (s) of a /ready probe of the validator."
    )
    fail_mode: ValidatorFailMode = Field(
        default="open",
        description="When the validator cannot be asked: open (default) reports a warning and "
        "skips the import check, so deploys go ahead without it; closed reports an error "
        "(deploys are blocked until the validator answers); local runs the check in this "
        "process's warm DagBag pool instead (user code then runs here again).",
    )
    # --- the service side (autoair validator-serve)
    pool_size: int = Field(
        default=2, ge=1, le=64, description="Validator: warm DagBag worker processes."
    )
    max_tasks_per_child: int = Field(
        default=1,
        ge=1,
        description="Validator: checks per worker before it is replaced. 1 (default) gives "
        "every check a fresh interpreter, so code of one check cannot observe or alter the "
        "next one; a spare worker is started in advance.",
    )
    start_timeout_seconds: float = Field(
        default=120,
        gt=0,
        description="Validator: seconds a worker may take to start (import Airflow).",
    )
    max_check_seconds: float = Field(
        default=120,
        gt=0,
        description="Validator: largest import check timeout (s) a request may ask for.",
    )
    max_request_bytes: int = Field(
        default=4 * 1024 * 1024,
        ge=1024,
        description="Validator: largest request body (bytes, the JSON-encoded DAG source); "
        "larger requests get HTTP 413.",
    )
    max_queue: int = Field(
        default=8,
        ge=0,
        description="Validator: checks that may wait for a worker on top of pool_size; more "
        "concurrent requests get HTTP 503 (the main server retries).",
    )
    memory_limit_mb: int = Field(
        default=1024,
        ge=0,
        description="Validator: address-space limit of a worker process (MiB, RLIMIT_AS); "
        "an import that allocates more fails with MemoryError. 0: no limit.",
    )
    cpu_limit_seconds: int = Field(
        default=120,
        ge=0,
        description="Validator: CPU seconds a worker process may use over its lifetime "
        "(RLIMIT_CPU, includes about 2 s for importing Airflow); the worker is then killed. "
        "0: no limit.",
    )
    file_size_limit_mb: int = Field(
        default=16,
        ge=0,
        description="Validator: largest file a worker may write (MiB, RLIMIT_FSIZE). 0: no limit.",
    )
    open_files_limit: int = Field(
        default=1024,
        ge=0,
        description="Validator: open file descriptors per worker (RLIMIT_NOFILE). 0: inherited.",
    )
    process_limit: int = Field(
        default=0,
        ge=0,
        description="Validator: RLIMIT_NPROC of a worker. It counts every process of the "
        "user, so set it only when the validator runs as its own user. 0: inherited.",
    )
    audit_hook: bool = Field(
        default=True,
        description="Validator: a Python audit hook in each worker denies subprocesses, "
        "fork/exec, signals, ctypes, network connections and DNS, writes outside the "
        "worker's scratch folder, and reads of other processes' /proc entries. Defence in "
        "depth: the container (no egress, read-only root, non-root) is the boundary.",
    )
    network_isolation: NetworkIsolation = Field(
        default="auto",
        description="Validator: move each worker into its own empty network namespace "
        "(Linux unprivileged user namespaces). auto: when the kernel allows it, required: "
        "workers fail to start otherwise, off: never.",
    )
    metrics_enabled: bool = Field(
        default=True, description="Validator: serve Prometheus metrics at /metrics."
    )
    # --- test runs on the validator (POST /v1/run)
    test_runs: bool = Field(
        default=True,
        description="Send task and DAG test runs to the validator (POST /v1/run on run_url, "
        "else url) when one is set, so their code runs in the validator's sandbox instead of "
        "this host. "
        "When the validator cannot be asked, fail_mode local runs the test here and both open "
        "and closed fail it (a test run never silently runs locally). mode=db runs always "
        "run here: the validator has no metadata DB. false: test runs always run here.",
    )
    run_url: str = Field(
        default="",
        description="Base URL of the validator that executes test runs; empty: url. Point it "
        "at a second validator deployment whose NetworkPolicy allows the egress task tests "
        "need, so the import-check validator keeps no network at all (a NetworkPolicy applies "
        "to a whole pod and cannot tell run processes from check workers).",
    )
    runs_enabled: bool = Field(
        default=True,
        description="Validator: serve test runs (POST /v1/run). Set false on an import-check-only "
        "deployment (when the server's run_url points at a separate one): /v1/run then "
        "answers 404.",
    )
    run_network: bool = Field(
        default=True,
        description="Validator: test-run processes may open network connections (task tests "
        "often call services). The pod then needs a NetworkPolicy egress rule; import checks "
        "keep no network either way. false: every run gets an empty network namespace "
        "(network_isolation decides whether that is required).",
    )
    run_process_isolation: NetworkIsolation = Field(
        default="auto",
        description="Validator: run each test run as PID 1 of its own PID, user and mount "
        "namespaces with a fresh /proc (see runner.process_isolation). auto: when the "
        "kernel or container allows it, required: runs fail otherwise, off: never.",
    )
    max_run_seconds: float = Field(
        default=600,
        gt=0,
        description="Validator: largest test-run timeout (s) a request may ask for.",
    )
    max_concurrent_runs: int = Field(
        default=2,
        ge=1,
        le=64,
        description="Validator: test runs executing at the same time (one process each); more "
        "concurrent requests get HTTP 503 (the main server retries).",
    )
    run_memory_limit_mb: int = Field(
        default=2048,
        ge=0,
        description="Validator: address-space limit of a test-run process (MiB, RLIMIT_AS); "
        "file_size_limit_mb, open_files_limit and process_limit apply to runs too, the CPU "
        "time is bounded by the run's timeout. 0: no limit.",
    )

    @field_validator("url", "run_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("validator.url and validator.run_url must be http(s) URLs")
        return value

    @property
    def runs_url(self) -> str:
        """Where test runs go (``run_url``, else ``url``; empty: nowhere)."""
        return self.run_url or self.url


# --------------------------------------------------------------------------- editor services

DEFAULT_SHELLCHECK_EXCLUDE = ["SC1091", "SC2034", "SC2154"]
DEFAULT_LINT_SELECT = ["F", "E9", "B006", "B008", "B012", "B018", "PLE"]
DEFAULT_LINT_IGNORE = ["F401"]


class EditorSettings(_Section):
    """Python/shell editor services: completion, ShellCheck and the ruff lint of task code."""

    max_source_chars: int = Field(
        default=200_000, ge=1_000, description="Largest code accepted by completion and lint."
    )
    max_completions: int = Field(
        default=100, ge=1, description="Completion items returned per request."
    )
    max_doc_chars: int = Field(
        default=4_000, ge=100, description="Characters of a docstring shown in hover/completion."
    )
    shellcheck_timeout: float = Field(
        default=5,
        gt=0,
        description="Seconds per ShellCheck run (DAG validation checks the scripts that "
        "changed since the last validation in runs of up to 50 scripts).",
    )
    shellcheck_exclude: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SHELLCHECK_EXCLUDE),
        description="ShellCheck codes never reported (noise for Airflow commands: masked "
        "Jinja, sourced env files). Overrides the built-in list.",
    )
    shellcheck_exclude_extra: list[str] = Field(
        default_factory=list, description="More ShellCheck codes never reported."
    )
    shellcheck_in_validation: bool = Field(
        default=True,
        description="Report ShellCheck findings of bash_command, @task.bash and .sh file "
        "assets as warnings in DAG validation (Problems tab). Needs ShellCheck.",
    )
    python_lint_select: list[str] = Field(
        default_factory=lambda: list(DEFAULT_LINT_SELECT),
        description="ruff rules checked in Python task code. Overrides the built-in list.",
    )
    python_lint_select_extra: list[str] = Field(
        default_factory=list, description="More ruff rules checked (e.g. 'S' for bandit)."
    )
    python_lint_ignore: list[str] = Field(
        default_factory=lambda: list(DEFAULT_LINT_IGNORE),
        description="ruff rules never reported in task code. Overrides the built-in list.",
    )
    python_lint_ignore_extra: list[str] = Field(
        default_factory=list, description="More ruff rules never reported."
    )
    python_lint_timeout: float = Field(
        default=15,
        gt=0,
        description="Seconds per ruff lint run of task code (DAG validation lints all "
        "Python tasks that changed since the last validation in one run).",
    )
    validate_timeout: float = Field(
        default=15,
        gt=0,
        description="Seconds each external checker (bash -n, gofmt, javac, tsc…) may run when "
        "a code field is validated (POST /api/lsp/validate).",
    )
    validate_commands: dict[str, list[str]] = Field(
        default_factory=dict,
        description='Checker commands overriding the PATH lookup, by tool name, e.g. {"tsc": '
        '["/opt/node/bin/tsc"], "javac": ["/usr/lib/jvm/java-21/bin/javac"]}.',
    )


# --------------------------------------------------------------------------- code generation


class CodegenSettings(_Section):
    """Generated DAG files."""

    line_length: int = Field(
        default=100, ge=40, le=320, description="ruff line length of generated DAG files."
    )
    format_timeout: float = Field(
        default=30, gt=0, description="Seconds the ruff formatting pass may take."
    )


# --------------------------------------------------------------------------- imports


class InterchangeSettings(_Section):
    """Importing Python/YAML DAGs and spec JSON."""

    max_source_chars: int = Field(
        default=2_000_000, ge=1_000, description="Largest DAG source accepted by an import."
    )
    yaml_max_nodes: int = Field(
        default=200_000,
        ge=100,
        description="Largest YAML document (values after alias expansion) accepted; guards "
        "against 'billion laughs' documents.",
    )
    bulk_max_files: int = Field(
        default=500,
        ge=1,
        description="Bulk import of a DAG repository: most Python files imported per request "
        "(more are reported as not scanned).",
    )
    bulk_workers: int = Field(
        default=4,
        ge=1,
        le=64,
        description="Bulk import: files imported concurrently (one isolated subprocess each).",
    )
    bulk_exclude: list[str] = Field(
        default_factory=lambda: [
            ".*",
            "*/.*",
            "tests/*",
            "*/tests/*",
            "test_*.py",
            "*/test_*.py",
            "*/__pycache__/*",
            "*/node_modules/*",
        ],
        description="Bulk import: fnmatch patterns over repository-relative paths that are "
        "never imported (tests, hidden folders…). Files without both 'airflow' and 'dag' in "
        "their text are skipped as well (Airflow's safe mode heuristic).",
    )
    bulk_git_allowed_hosts: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Bulk import from git: fnmatch patterns of the hosts that may be cloned "
        "(https only), e.g. ['github.com', 'gitlab.acme.internal']. Empty: git sources are "
        "disabled.",
    )
    bulk_git_timeout: float = Field(
        default=120, gt=0, description="Bulk import: seconds a shallow git clone may take."
    )


# --------------------------------------------------------------------------- icons


class IconRule(_Section):
    """``icons.rules_extra`` entry."""

    match: str = Field(description="Regex searched in the class path and the distribution.")
    icon: str = Field(description="builtin:<logo>, a glyph name (agent, clock…) or custom:<name>.")

    @field_validator("match")
    @classmethod
    def _regex(cls, value: str) -> str:
        return _compile_regex(value, "icons.rules_extra match")

    @field_validator("icon")
    @classmethod
    def _icon(cls, value: str) -> str:
        from autoair.icons import normalize_icon_ref

        return normalize_icon_ref(value)


class IconSettings(_Section):
    """Custom node icons."""

    max_upload_bytes: int = Field(
        default=512 * 1024, ge=1024, description="Largest icon upload accepted."
    )
    max_side: int = Field(
        default=128, ge=16, le=1024, description="Stored icons are scaled to fit this (px)."
    )
    max_pixels: int = Field(
        default=4096 * 4096,
        ge=1024,
        description="Uploads with more pixels are refused (decompression-bomb guard).",
    )
    rules_extra: list[IconRule] = Field(
        default_factory=list,
        description="Icon rules evaluated before the built-in inference: [{match: regex over "
        "the operator class path or its distribution/provider, icon: 'builtin:<logo>', a "
        "glyph name such as 'agent', or 'custom:<uploaded>'}] (first match wins).",
    )


# --------------------------------------------------------------------------- HTTP server


class ServerSettings(_Section):
    """HTTP server (standalone ``autoair serve`` and the API inside Airflow)."""

    host: str = Field(default="127.0.0.1", description="`autoair serve` bind address.")
    port: int = Field(default=8765, ge=1, le=65535, description="`autoair serve` port.")
    gzip_min_size: int = Field(
        default=2048, ge=0, description="Responses larger than this (bytes) are gzip-compressed."
    )
    gzip_level: int = Field(default=6, ge=1, le=9, description="gzip compression level.")
    otel_excluded_urls: list[str] = Field(
        default_factory=lambda: ["health", "ready", "metrics"],
        description="URL patterns OpenTelemetry does not trace.",
    )
    airflow_fetch_concurrency: int = Field(
        default=8, ge=1, le=64, description="Parallel requests to Airflow's API per fan-out call."
    )
    airflow_page_size: int = Field(
        default=1000,
        ge=1,
        le=100_000,
        description="Entries requested per list call to Airflow (connections, pools, "
        "variables, task instances of a run…).",
    )

    # --- public address
    public_url: str | None = Field(
        default=None,
        description="Public base URL of the AutoAir UI as users reach it (e.g. "
        "https://autoair.example.com or https://airflow.example.com/autoair). Used for links "
        "back to a DAG in commits/pull requests and for the 'Sign in with Airflow' return URL. "
        "Unset: no links.",
    )

    # --- process (`autoair serve` / `autoair local`)
    workers: int = Field(
        default=1,
        ge=1,
        le=64,
        description="`autoair serve`: worker processes. More than one needs shared state "
        "(collab_secret, shared proposals/audit URLs) and disables in-memory caches sharing.",
    )
    graceful_shutdown_timeout: float = Field(
        default=30,
        ge=0,
        description="`autoair serve`: seconds in-flight requests get to finish on shutdown.",
    )
    keep_alive_timeout: float = Field(
        default=5, gt=0, description="`autoair serve`: idle seconds before a keep-alive closes."
    )
    limit_concurrency: int | None = Field(
        default=None,
        ge=1,
        description="`autoair serve`: most concurrent connections before answering 503.",
    )
    access_log: bool = Field(default=True, description="`autoair serve`: log every request.")
    harden_process: bool = Field(
        default=True,
        description="Make AutoAir's own server processes (`autoair serve`, `autoair local`, "
        "their uvicorn workers, `mcp-serve --http`, `ai-serve`) non-dumpable at start-up "
        "(Linux prctl PR_SET_DUMPABLE=0), so child processes running user DAG code as the "
        "same user cannot read the server's environment or memory through /proc. Turn it "
        "off only for debugging with py-spy, gdb or core dumps. Never applied inside the "
        "Airflow plugin.",
    )

    # --- TLS (`autoair serve`; inside Airflow the api-server terminates TLS)
    ssl_certfile: Path | None = Field(
        default=None, description="`autoair serve`: TLS certificate chain (PEM); enables HTTPS."
    )
    ssl_keyfile: Path | None = Field(
        default=None, description="`autoair serve`: TLS private key (PEM)."
    )
    ssl_keyfile_password: SecretStr | None = Field(
        default=None, description="`autoair serve`: password of an encrypted ssl_keyfile."
    )
    ssl_ca_certs: Path | None = Field(
        default=None,
        description="`autoair serve`: CA bundle client certificates are verified against "
        "(mutual TLS, with ssl_cert_reqs).",
    )
    ssl_cert_reqs: Literal["none", "optional", "required"] = Field(
        default="none",
        description="`autoair serve`: client certificates (mutual TLS): none, optional, "
        "required (needs ssl_ca_certs).",
    )
    ssl_ciphers: str | None = Field(
        default=None,
        description="`autoair serve`: OpenSSL cipher string (default: Python's secure set).",
    )
    ssl_min_version: Literal["TLSv1.2", "TLSv1.3"] = Field(
        default="TLSv1.2", description="`autoair serve`: lowest TLS version accepted."
    )

    # --- reverse proxy
    proxy_headers: bool = Field(
        default=True,
        description="`autoair serve`: trust X-Forwarded-Proto/For from forwarded_allow_ips "
        "(scheme and client address behind a reverse proxy).",
    )
    forwarded_allow_ips: list[str] = Field(
        default_factory=lambda: ["127.0.0.1"],
        description="`autoair serve`: proxy addresses whose X-Forwarded-* headers are trusted "
        "('*' trusts every peer: only behind a proxy that strips them).",
    )

    # --- HTTP security (standalone server)
    security_headers: bool = Field(
        default=True,
        description="Standalone: send Content-Security-Policy, X-Content-Type-Options, "
        "Referrer-Policy, X-Frame-Options/frame-ancestors (and HSTS when hsts_seconds > 0).",
    )
    content_security_policy: str | None = Field(
        default=None,
        description="Standalone: the whole Content-Security-Policy header, replacing the "
        "built-in policy for the single-page app (frame_ancestors is then ignored).",
    )
    frame_ancestors: list[str] = Field(
        default_factory=lambda: ["'self'"],
        description="Standalone: who may embed the UI in a frame (CSP frame-ancestors), e.g. "
        '["\'self\'", "https://portal.example.com"]; ["\'none\'"] forbids embedding.',
    )
    referrer_policy: str = Field(
        default="strict-origin-when-cross-origin",
        description="Standalone: Referrer-Policy header (empty: not sent).",
    )
    hsts_seconds: int = Field(
        default=0,
        ge=0,
        description="Standalone: Strict-Transport-Security max-age sent on HTTPS responses "
        "(0: no HSTS header). Only enable when every client reaches AutoAir over HTTPS.",
    )
    hsts_include_subdomains: bool = Field(
        default=False, description="Standalone: HSTS includeSubDomains."
    )
    hsts_preload: bool = Field(default=False, description="Standalone: HSTS preload.")
    cookie_secure: Literal["auto", "always", "never"] = Field(
        default="auto",
        description="Secure flag of cookies AutoAir sets (sign-in state, refreshed Airflow "
        "session): auto = when the request came over HTTPS.",
    )
    cookie_samesite: Literal["lax", "strict"] = Field(
        default="lax", description="SameSite attribute of cookies AutoAir sets."
    )

    # --- requests and responses
    max_body_bytes: int = Field(
        default=16 * 1024 * 1024,
        ge=1024,
        description="Largest request body accepted (413 above it); per-feature limits such "
        "as interchange.max_source_chars still apply.",
    )
    static_max_age: int = Field(
        default=31_536_000,
        ge=0,
        description="Cache-Control max-age (s) of content-hashed UI assets (immutable); entry "
        "files are always revalidated.",
    )
    metrics_enabled: bool = Field(
        default=True, description="Serve Prometheus metrics at /api/metrics."
    )
    ready_check_storage: bool = Field(
        default=True, description="/api/ready also checks the deploy target is reachable."
    )
    ready_check_airflow: bool = Field(
        default=False,
        description="/api/ready also checks Airflow's /api/v2/monitor/health answers.",
    )
    ready_timeout: float = Field(
        default=5, gt=0, description="Seconds each /api/ready check may take."
    )


# --------------------------------------------------------------------------- Airflow plugin


class PluginSettings(_Section):
    """How the builder appears inside Airflow's UI (plugin mode, Airflow >= 3.1)."""

    nav_name: str = Field(
        default="DAG Builder", min_length=1, description="Name of the navigation entry."
    )
    nav_category: str = Field(
        default="browse",
        description="Airflow navigation menu holding the entry (browse, admin, docs, user…).",
    )
    dag_tab_name: str = Field(
        default="AutoAir", min_length=1, description="Name of the tab on every DAG page."
    )
    dag_tab: bool = Field(default=True, description="Show the tab on DAG pages.")
    dag_tab_tags: list[str] = Field(
        default_factory=list,
        description="Airflow >= 3.4: show the DAG tab only on DAGs carrying at least one of "
        "these tags (the plugin's applies_to scoping, evaluated by Airflow's UI). Empty: the "
        "tab is on every DAG page, so a hand-written DAG can be imported from it. Ignored "
        "before Airflow 3.4, which cannot scope plugin tabs. JSON list in environment "
        "variables, e.g. '[\"autoair\"]'.",
    )


# --------------------------------------------------------------------------- outbound HTTP/TLS

OutboundClient = Literal[
    "airflow", "git", "marketplace", "ai", "research", "login", "mcp", "opa", "kb", "validator"
]


class OutboundOverride(_Section):
    """Per-client override of :class:`OutboundSettings` (unset fields inherit)."""

    verify: bool | None = Field(default=None, description="Verify TLS certificates.")
    ca_bundle: Path | None = Field(default=None, description="CA bundle (PEM file or folder).")
    client_cert: Path | None = Field(default=None, description="Client certificate (PEM).")
    client_key: Path | None = Field(default=None, description="Client certificate key (PEM).")
    proxy: str | None = Field(default=None, description="Proxy URL for this client.")


class OutboundSettings(_Section):
    """TLS and proxies of every outgoing connection: the Airflow REST client and token checks,
    git hosts (REST and the git CLI), marketplaces, AI providers, online research, the
    sign-in proxy. One place, applied by :mod:`autoair.net.tls`."""

    verify: bool = Field(
        default=True,
        description="Verify TLS certificates. false is for throw-away test setups only: "
        "AutoAir logs a warning for every client built without verification.",
    )
    ca_bundle: Path | None = Field(
        default=None,
        description="CA bundle (PEM file, or a folder of hashed certificates) used instead of "
        "the default trust store, like REQUESTS_CA_BUNDLE (e.g. a corporate root CA); unset: "
        "certifi's store (SSL_CERT_FILE is honoured when trust_env is true).",
    )
    client_cert: Path | None = Field(
        default=None, description="Client certificate (PEM) for mutual TLS with servers."
    )
    client_key: Path | None = Field(
        default=None, description="Private key of client_cert (PEM; default: inside client_cert)."
    )
    client_key_password: SecretStr | None = Field(
        default=None, description="Password of an encrypted client_key."
    )
    http_proxy: str | None = Field(
        default=None, description="Proxy for http:// URLs (e.g. http://proxy:3128)."
    )
    https_proxy: str | None = Field(
        default=None, description="Proxy for https:// URLs (e.g. http://proxy:3128)."
    )
    no_proxy: list[str] = Field(
        default_factory=list,
        description="Hosts reached without the proxy: names (sub-domains match), IPs, "
        "'host:port'; e.g. ['localhost', '127.0.0.1', '.internal'].",
    )
    trust_env: bool = Field(
        default=True,
        description="Also honour the standard environment variables (HTTP(S)_PROXY, NO_PROXY, "
        "SSL_CERT_FILE…) when the settings above leave them unset.",
    )
    clients: dict[OutboundClient, OutboundOverride] = Field(
        default_factory=dict,
        description="Per-client overrides, e.g. `{'git': {'ca_bundle': '/etc/ssl/gitlab.pem'}, "
        "'ai': {'proxy': 'http://egress:3128'}}`. Clients: airflow, git, marketplace, ai, "
        "research, login, mcp (external MCP servers of the agents), opa (policy.opa_url), kb "
        "(documentation fetches and the Elasticsearch store of the knowledge base), validator "
        "(validator.url).",
    )


# --------------------------------------------------------------------------- audit

AuditSink = Literal["file", "db", "log", "otel"]


class AuditSettings(_Section):
    """Where the audit trail of builder actions (deploy, delete, approvals, template and
    admin changes…) is written. Airflow 3's REST API cannot write event-log entries (only
    ``GET /eventLogs``), so AutoAir keeps its own trail and can emit it to log pipelines and
    OpenTelemetry."""

    sinks: list[AuditSink] | None = Field(
        default=None,
        description="Audit sinks: file (JSON lines at audit_url), db (the SQL database of "
        "database.backend), log (one structured line on the 'autoair.audit' logger), otel (an "
        "OpenTelemetry span per record; needs autoair[otel] and a tracer provider). Unset: "
        "the state store (db with a database, else file) plus log. The audit view of the UI "
        "reads db, else file.",
    )

    @field_validator("sinks")
    @classmethod
    def _unique(cls, value: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(value)) if value is not None else None

    def effective(self, has_database: bool) -> list[str]:
        if self.sinks is not None:
            return list(self.sinks)
        return ["db" if has_database else "file", "log"]


# --------------------------------------------------------------------------- logging


class LoggingSettings(_Section):
    """Logging of `autoair serve` / `autoair local` (inside Airflow, Airflow's logging
    configuration applies; only ``levels`` is honoured there)."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Root log level."
    )
    format: Literal["text", "json"] = Field(
        default="text",
        description="text: human readable lines; json: one JSON object per line (timestamp, "
        "level, logger, message, exception) for log shippers.",
    )
    levels: dict[str, str] = Field(
        default_factory=dict,
        description="Per-logger levels, e.g. {'autoair.storage': 'DEBUG', 'httpx2': 'WARNING'}.",
    )

    @field_validator("levels")
    @classmethod
    def _known_levels(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "NOTSET"}
        out = {}
        for name, level in value.items():
            if level.upper() not in allowed:
                raise ValueError(f"unknown log level {level!r} for {name!r}")
            out[name] = level.upper()
        return out


# --------------------------------------------------------------------------- rate limits

RateLimitGroup = Literal["validate", "import", "ai", "test_runs", "deploy", "catalog_refresh"]


def _default_rate_limits() -> dict[RateLimitGroup, int]:
    return {
        "validate": 240,
        "import": 60,
        "ai": 60,
        "test_runs": 30,
        "deploy": 60,
        "catalog_refresh": 6,
    }


class LimitsSettings(_Section):
    """Per-user rate limits of expensive endpoints (sliding window, in memory per process)."""

    rate_limits_enabled: bool = Field(
        default=True, description="Enforce rate_limits (429 with Retry-After above them)."
    )
    window_seconds: float = Field(
        default=60, gt=0, description="Length of the rate-limit window (s)."
    )
    rate_limits: dict[RateLimitGroup, int] = Field(
        default_factory=_default_rate_limits,
        description="Requests per user and window per endpoint group (0: unlimited): "
        "validate (validation, code preview lint), import (Python/YAML/Airflow imports), ai "
        "(every AI call), test_runs (task/DAG tests), deploy (deploys, deletes, promotions), "
        "catalog_refresh.",
    )
    role_rate_limits: dict[str, dict[RateLimitGroup, int]] = Field(
        default_factory=dict,
        description="Per AutoAir role overrides, e.g. {'Admin': {'ai': 0}, 'Viewer': "
        "{'validate': 30}}; a user gets the most generous limit of their roles (0 wins).",
    )
    max_tracked_users: int = Field(
        default=10_000, ge=1, description="Users tracked by the limiter (least recent dropped)."
    )


# --------------------------------------------------------------------------- UI shell


class UiSettings(_Section):
    """Branding and defaults of the web UI (sent in /api/meta and /api/auth/config)."""

    title: str = Field(
        default="AutoAir", min_length=1, max_length=80, description="Product name in the UI."
    )
    logo_url: str | None = Field(
        default=None,
        description="Logo shown in the UI (https URL, or a path served by your proxy).",
    )
    accent_color: str | None = Field(
        default=None,
        pattern=r"^(#[0-9A-Fa-f]{3,8}|[a-z]{3,20})$",
        description="Accent colour of the banner/brand strip: a CSS hex colour or a Chakra "
        "palette name (blue, teal, red…).",
    )
    banner_text: str | None = Field(
        default=None,
        max_length=300,
        description="Banner shown above the builder, e.g. 'PRODUCTION' or a maintenance "
        "notice (empty: none).",
    )
    banner_color: str = Field(
        default="orange",
        pattern=r"^(#[0-9A-Fa-f]{3,8}|[a-z]{3,20})$",
        description="Banner colour: a Chakra palette name (red, orange, green…) or a hex colour.",
    )
    default_language: Literal["en", "tr"] | None = Field(
        default=None,
        description="UI language for users who never chose one (default: the browser's).",
    )
    default_timezone: str | None = Field(
        default=None,
        max_length=64,
        description="IANA time zone used to show times (e.g. 'Europe/Istanbul', 'UTC'); "
        "default: the browser's.",
    )
    date_locale: str | None = Field(
        default=None,
        max_length=35,
        description="BCP 47 locale for dates and numbers (e.g. 'en-GB'); default: the UI language.",
    )


# --------------------------------------------------------------------------- Airflow SSO


class SsoSettings(_Section):
    """Sign-in with the Airflow UI session (see docs/sso.md)."""

    enabled: bool = Field(
        default=True,
        description="Standalone: accept the Airflow UI session cookie (when the browser sends "
        "it: same host, or a shared parent domain set up at the proxy) and offer 'Sign in "
        "with Airflow'. Plugin mode always uses Airflow's session.",
    )
    cookie_name: str = Field(
        default="_token",
        min_length=1,
        max_length=128,
        pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$",
        description="Name of Airflow's session (JWT) cookie.",
    )
    airflow_public_url: str | None = Field(
        default=None,
        description="Airflow UI URL as browsers reach it (default: airflow_url). 'Sign in "
        "with Airflow' opens <airflow_public_url>/auth/login.",
    )
    require_csrf_header: bool = Field(
        default=True,
        description="Standalone with SSO, and plugin mode (Airflow's _token cookie): "
        "state-changing requests authenticated by the cookie (not a bearer token) must carry "
        "the X-AutoAir-CSRF header and a same-origin (or allowed CORS) Origin — the cookie "
        "is SameSite=Lax, which still lets sibling sites on the same registrable domain send "
        "it.",
    )
    refresh_margin_seconds: int = Field(
        default=300,
        ge=0,
        description="The UI refreshes the session this many seconds before the token "
        "expires (cookie sessions: through Airflow's refresh middleware).",
    )
    session_check_seconds: int = Field(
        default=60,
        ge=0,
        description="The UI re-checks the session this often (s) so a sign-out in Airflow "
        "signs AutoAir out too (0: only on API errors).",
    )
    propagate_logout: bool = Field(
        default=True,
        description="Signing out of AutoAir also revokes the Airflow token (Airflow >= 3.2) "
        "and clears the shared session cookie (signs the Airflow UI out when it shares it).",
    )


# --------------------------------------------------------------------------- local mode


class LocalModeSettings(_Section):
    """`autoair local`: the builder on a laptop, without an Airflow server."""

    token: SecretStr | None = Field(
        default=None,
        description="Access token for `autoair local` (Bearer header or pasted in the UI). "
        "Unset: `autoair local` creates a random token per run and opens the UI with it "
        "(`--no-token` serves without authentication on loopback, with "
        "iam.anonymous_roles).",
    )
    roles: list[str] = Field(
        default_factory=lambda: ["Admin"],
        description="AutoAir roles of the `autoair local` token holder (the local user); "
        "never direct-push rights through storage.git.direct_push_roles.",
    )
    open_browser: bool = Field(
        default=False, description="Open the UI in the default browser once started."
    )


# --------------------------------------------------------------------------- admin panel

#: Hard upper bounds of the admin limits (memory guard: records are held in memory).
ADMIN_EXPORT_MAX = 1_000_000
ADMIN_SCAN_MAX = 1_000_000


class AdminSettings(_Section):
    """Admin panel (``/api/admin``): export and scan limits."""

    export_limit: int = Field(
        default=50_000,
        ge=1,
        le=ADMIN_EXPORT_MAX,
        description="Most audit records one CSV export may return (larger `limit` "
        "requests are refused).",
    )
    export_default: int = Field(
        default=10_000,
        ge=1,
        le=ADMIN_EXPORT_MAX,
        description="Audit records a CSV export returns without an explicit `limit` "
        "(capped at export_limit).",
    )
    scan_limit: int = Field(
        default=5000,
        ge=1,
        le=ADMIN_SCAN_MAX,
        description="Most recent audit records scanned to list the users seen.",
    )
    usage_scan_limit: int = Field(
        default=20_000,
        ge=1,
        le=ADMIN_SCAN_MAX,
        description="AI audit records aggregated for the AI usage view on the file backend "
        "(SQL backends aggregate in the database).",
    )


# --------------------------------------------------------------------------- active sections

SECTION_TYPES: tuple[type[BaseModel], ...] = (
    DiscoverySettings,
    CatalogSettings,
    PolicyRules,
    SandboxSettings,
    RunnerSettings,
    EditorSettings,
    CodegenSettings,
    InterchangeSettings,
    IconSettings,
    ServerSettings,
    PluginSettings,
    OutboundSettings,
    LoggingSettings,
    LimitsSettings,
    UiSettings,
    SsoSettings,
    LocalModeSettings,
    AdminSettings,
    ValidatorSettings,
)
_ACTIVE: dict[type[BaseModel], BaseModel] = {}


def activate(settings: BaseModel) -> None:
    """Make ``settings``' sections the ones :func:`active` returns in this process."""
    for name in type(settings).model_fields:
        value = getattr(settings, name, None)
        for cls in SECTION_TYPES:
            if isinstance(value, cls):
                _ACTIVE[cls] = value


def active(cls: type[_S]) -> _S:
    """The active instance of a section (defaults until :func:`activate` is called)."""
    value = _ACTIVE.get(cls)
    if value is None:
        value = _ACTIVE.setdefault(cls, cls())
    assert isinstance(value, cls)
    return value


def reset() -> None:
    """Forget activated sections (tests)."""
    _ACTIVE.clear()


def reset_all() -> None:
    """Forget every process-wide configuration state (tests): activated sections, the
    catalog discovery filters and the active AI section."""
    import importlib

    reset()
    importlib.import_module("autoair.catalog.discovery").reset()
    importlib.import_module("autoair.net.tls").reset()
    importlib.import_module("autoair.auth").configure_session_cookie("_token", enabled=True)
    importlib.import_module("autoair.ai.prompts").set_rules_provider(None)
    ai_config = importlib.import_module("autoair.ai.config")
    reset_ai = getattr(ai_config, "reset_ai", None) or getattr(ai_config, "reset", None)
    if callable(reset_ai):
        reset_ai()
    else:
        active_ai = getattr(ai_config, "_ACTIVE", None)
        if isinstance(active_ai, list):
            active_ai.clear()


__all__ = [
    "SECTION_TYPES",
    "AdminSettings",
    "AuditSettings",
    "CatalogSettings",
    "CodegenSettings",
    "ConnectionParamPattern",
    "DiscoverySettings",
    "EditorSettings",
    "GroupRule",
    "IconRule",
    "IconSettings",
    "InterchangeSettings",
    "LimitsSettings",
    "LocalModeSettings",
    "LoggingSettings",
    "OutboundOverride",
    "OutboundSettings",
    "ParamOverride",
    "PluginSettings",
    "PolicyRules",
    "RunnerSettings",
    "SandboxSettings",
    "ServerSettings",
    "SsoSettings",
    "UiSettings",
    "ValidatorSettings",
    "activate",
    "active",
    "any_search",
    "compile_patterns",
    "reset_all",
]
