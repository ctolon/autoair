"""Runtime configuration (``AUTOAIR_*`` environment variables and config files).

Conventions (keep them when adding settings):

* every field has a ``description`` (the configuration reference is generated from it) and a
  default; secrets are ``SecretStr`` (masked when the configuration is exposed);
* a feature area owns a nested *section* model in its own module (``IamSettings`` in
  ``autoair/iam/config.py``, ``AiSettings`` in ``autoair/ai/config.py`` …) registered here as
  one field — environment variables then read ``AUTOAIR_<SECTION>__<FIELD>``;
* user-facing features can be switched off with :class:`FeatureFlags` (``AUTOAIR_FEATURES__AI``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import NoDecode, SettingsConfigDict

from autoair.ai.config import AiSettings
from autoair.blueprints.config import BlueprintSettings
from autoair.collab import CollabSettings
from autoair.config_sources import AutoAirBaseSettings, parse_file_list
from autoair.iam.config import IamSettings
from autoair.kb.config import KbSettings
from autoair.lsp.config import LspSettings
from autoair.marketplace import MarketplaceSource
from autoair.persistence.config import DatabaseSettings
from autoair.preserve.config import PreserveSettings
from autoair.sections import (
    AdminSettings,
    AuditSettings,
    CatalogSettings,
    CodegenSettings,
    DiscoverySettings,
    EditorSettings,
    IconSettings,
    InterchangeSettings,
    LimitsSettings,
    LocalModeSettings,
    LoggingSettings,
    OutboundSettings,
    PluginSettings,
    PolicyRules,
    RunnerSettings,
    SandboxSettings,
    ServerSettings,
    SsoSettings,
    UiSettings,
    ValidatorSettings,
)
from autoair.storage.config import StorageSettings

ExposeConfig = Literal["false", "true", "non-sensitive-only"]


def _default_dags_folder() -> Path:
    try:
        from airflow.configuration import conf

        return Path(conf.get("core", "dags_folder")).expanduser()
    except Exception:
        return Path("./dags")


def _default_airflow_url() -> str:
    try:
        from airflow.configuration import conf

        url = conf.get("api", "base_url", fallback="") or ""
        if url:
            return url.rstrip("/")
        port = conf.get("api", "port", fallback="8080")
        return f"http://localhost:{port}"
    except Exception:
        return "http://localhost:8080"


class FeatureFlags(BaseModel):
    """Switch whole features on/off (hidden in the UI, routes answer 404)."""

    model_config = ConfigDict(extra="forbid")

    ai: bool = Field(default=True, description="AI assistance (generate, review, fix, explain…).")
    mcp: bool = Field(default=True, description="Model Context Protocol endpoint for agents.")
    collab: bool = Field(default=True, description="Real-time co-editing.")
    marketplace: bool = Field(default=True, description="Template marketplaces.")
    templates: bool = Field(default=True, description="Template gallery and saving templates.")
    imports: bool = Field(default=True, description="Import Python/YAML/Airflow DAGs.")
    test_runs: bool = Field(default=True, description="Test a task/DAG from the builder.")
    runs: bool = Field(default=True, description="Trigger, backfill, clear and pause from the UI.")
    deploy: bool = Field(default=True, description="Deploy (write/push DAG files).")
    custom_icons: bool = Field(default=True, description="Upload custom node icons.")
    python_code: bool = Field(
        default=True,
        description="Python code in DAGs (@task bodies, inline functions, script files, raw "
        "code DAGs).",
    )
    airflow: bool = Field(
        default=True,
        description="Features that call an Airflow server: connection/pool/variable pickers, "
        "DAG status, import from Airflow (off in `autoair local`).",
    )
    iam: bool = Field(
        default=True,
        description="AutoAir's own authorization (roles, role mapping, custom views, "
        "four-eyes approvals, direct-push roles). false: every signed-in user gets the "
        "Authenticated baseline plus what Airflow's DAG/configuration permissions grant; no "
        "AutoAir roles, mappings or approvals (see docs/iam.md).",
    )
    visual_editor: bool = Field(
        default=True,
        description="The visual (canvas) DAG editor. At least one of visual_editor and "
        "code_editor must be on; with only one, the builder opens in it without a toggle. "
        "Off: code editing only (no canvas or palette; visual DAGs open in the code mode).",
    )
    code_editor: bool = Field(
        default=True,
        description="Direct code editing: the full-screen code mode of visual DAGs (edit the "
        "generated file; applying it re-imports the code into the spec; needs spec:write, "
        "code:execute and python_code, since the importer executes the code) and raw code "
        "DAGs (hand-written files deployed as written; need code:raw and python_code). "
        "Off: visual editor only.",
    )
    code_edit_inline: bool = Field(
        default=False,
        description="Make the bottom panel's Code tab editable (same import-on-apply as the "
        "code mode).",
    )
    kb: bool = Field(
        default=True,
        description="Documentation knowledge base: search over the Airflow and Astronomer docs "
        "for agents, MCP (kb_search, kb_sources, kb_status) and the admin panel (see the kb "
        "section). Works without autoair[ai] (keyword search).",
    )
    autoair_docs: bool = Field(
        default=True,
        description="AutoAir's own documentation for agents: the MCP tools "
        "search_autoair_docs, read_autoair_doc and list_autoair_docs, which read the "
        "documentation packaged with this version (offline, no knowledge-base store). The "
        "knowledge base's autoair source is switched separately (kb.sources).",
    )
    source_preserve: bool = Field(
        default=True,
        description="Source-preserving deploys: DAGs whose source policy is preserve or "
        "strict-preserve (by default those imported from or edited as hand-written Python) "
        "get each change applied to their existing file as a minimal patch (LibCST) "
        "instead of a regenerated file. Off: every DAG file is generated; hand-written "
        "files are then protected by preserve.ejected_files.",
    )
    strict_source_preserve: bool = Field(
        default=False,
        description="Run every preserve deploy as strict-preserve: refuse each change that "
        "cannot be applied exactly (no replaced variable references, operator class "
        "changes or group moves through task_group=).",
    )
    ide_language_server: bool = Field(
        default=False,
        description="IDE-grade IntelliSense in code editors through a per-session language "
        "server (basedpyright or pylsp, bash-language-server; see the lsp section). Off: "
        "the built-in jedi completions.",
    )
    blueprints: bool = Field(
        default=True,
        description="Astronomer Blueprint templates (airflow-blueprint) as canvas nodes: the "
        "palette's Blueprints section, config forms and task previews (see the blueprints "
        "section). Reading config schemas needs autoair[blueprint].",
    )

    @model_validator(mode="after")
    def _one_editor(self) -> FeatureFlags:
        if not (self.visual_editor or self.code_editor):
            raise ValueError(
                "at least one of features.visual_editor and features.code_editor must be on "
                "(AUTOAIR_FEATURES__VISUAL_EDITOR, AUTOAIR_FEATURES__CODE_EDITOR); leave both "
                "on for the default, both editors"
            )
        return self


class Settings(AutoAirBaseSettings):
    """Every AutoAir setting. Sources, highest precedence first: init arguments, ``AUTOAIR_*``
    environment variables, ``.env``, configuration files (``AUTOAIR_CONFIG_FILE``; see
    :mod:`autoair.config_sources`), defaults."""

    model_config = SettingsConfigDict(
        env_prefix="AUTOAIR_", env_file=".env", extra="ignore", env_nested_delimiter="__"
    )
    warn_unknown_file_keys: ClassVar[bool] = True
    # Runtime feature overrides (autoair.feature_overrides), attached by build_services.
    _feature_overrides: Any = PrivateAttr(default=None)

    config_file: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Configuration files (YAML, JSON, INI, Python file or module:attr), "
        "comma separated or a JSON list; later files override earlier ones, environment "
        "variables override files. Read from the environment / `autoair serve --config` only.",
    )
    expose_config: ExposeConfig = Field(
        default="false",
        description="GET /api/config (admins only): false = disabled; true = every value, "
        "secrets masked as ***; non-sensitive-only = secrets left out. Like Airflow's "
        "[api] expose_config.",
    )
    features: FeatureFlags = Field(
        default_factory=FeatureFlags, description="Feature switches (AUTOAIR_FEATURES__<NAME>)."
    )
    features_locked: list[str] = Field(
        default_factory=list,
        description="Feature flags (fnmatch patterns, '*' = all) admins cannot switch at "
        "runtime from the admin panel: the configured value always wins. Flags set through "
        "the environment (AUTOAIR_FEATURES__<NAME>, .env, secrets directory) are locked too. "
        "Precedence: locked > runtime override > configured value.",
    )
    features_override_ttl: float = Field(
        default=5.0,
        ge=0,
        le=300,
        description="Seconds a replica keeps the runtime feature overrides before re-reading "
        "them from the state backend (how fast other replicas pick up a change made in the "
        "admin panel; the replica that made it sees it at once). 0 reads them on every check.",
    )
    iam: IamSettings = Field(
        default_factory=IamSettings,
        description="Authorization: AutoAir roles/permissions governed through Airflow.",
    )
    mode: Literal["plugin", "standalone", "local"] = Field(
        default="standalone",
        description="Deployment mode (set by the Airflow plugin / `autoair serve` / "
        "`autoair local`).",
    )
    dags_folder: Path = Field(
        default_factory=_default_dags_folder,
        description="Folder of a LocalDagBundle the files are written to.",
        json_schema_extra={"doc_default": "Airflow [core] dags_folder, else ./dags"},
    )
    subfolder: str = Field(
        default="autoair", description="Sub-folder inside dags_folder holding AutoAir DAGs."
    )
    airflow_url: str = Field(
        default_factory=_default_airflow_url,
        description="Airflow REST API base URL (pickers, status, runs, token checks).",
        json_schema_extra={"doc_default": "Airflow [api] base_url, else http://localhost:8080"},
    )
    airflow_secrets_backend: bool = Field(
        default=False,
        description="Airflow resolves connections/variables from a secrets backend (Vault, "
        "AWS Secrets Manager…) the REST API cannot list. true hides the pickers' "
        "'not found in Airflow's metastore' hint; unknown ids are never validation errors.",
    )
    allow_python_code: bool = Field(
        default=True,
        description="Allow Python code in DAGs (@task bodies, inline functions, script files, "
        "Python references). Also off when features.python_code is false.",
    )
    allow_privileged_operators: bool = Field(
        default=True,
        description="Allow operators that run arbitrary commands (policy.privileged_operators).",
    )
    import_check_on_deploy: bool = Field(
        default=True, description="Import the DAG with a DagBag before writing it."
    )
    operator_allow: list[str] = Field(
        default_factory=lambda: ["*"],
        description="fnmatch patterns of operator class paths shown in the palette.",
    )
    operator_deny: list[str] = Field(
        default_factory=list,
        description="fnmatch patterns of operator class paths hidden from the palette.",
    )
    extra_operator_modules: list[str] = Field(
        default_factory=list, description="Single modules scanned for operators."
    )
    extra_operator_packages: list[str] = Field(
        default_factory=list,
        description="Installed packages whose every module is scanned for operators "
        "(e.g. a company's internal operator library).",
    )
    extra_operator_distributions: list[str] = Field(
        default_factory=list,
        description="pip distributions whose every module is scanned for operators, e.g. "
        "acme-airflow-ops (found even without an Airflow requirement or provider entry point).",
    )
    extra_operator_paths: list[Path] = Field(
        default_factory=list,
        description="Operator files or package folders; their import root is inferred from "
        "__init__.py files (/opt/acme/acme_ops/jira.py -> acme_ops.jira).",
    )
    custom_operator_paths: list[Path] = Field(
        default_factory=list,
        description="Folders (import roots) scanned for custom operators, e.g. include/.",
    )
    discover_airflow_folders: bool = Field(
        default=True,
        description="Also scan Airflow's plugins_folder and dags_folder for custom operators.",
    )
    scan_provider_packages: bool = Field(
        default=True,
        description="Scan every module of installed providers, not only those declared in "
        "provider.yaml (finds undeclared and third-party provider operators).",
    )
    discovery: DiscoverySettings = Field(
        default_factory=DiscoverySettings,
        description="Operator discovery filters (AUTOAIR_DISCOVERY__<FIELD>).",
    )
    catalog: CatalogSettings = Field(
        default_factory=CatalogSettings,
        description="Catalog presentation: connection-parameter detection, palette groups, "
        "parameter widgets, package metadata (AUTOAIR_CATALOG__<FIELD>).",
    )
    blueprints: BlueprintSettings = Field(
        default_factory=BlueprintSettings,
        description="Where the blueprint catalog finds Astronomer Blueprint templates "
        "(AUTOAIR_BLUEPRINTS__<FIELD>).",
    )
    cors_origins: list[str] = Field(
        default_factory=list, description="Standalone: allowed CORS origins."
    )
    cors_allow_methods: list[str] = Field(
        default_factory=lambda: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        description="Standalone: methods allowed cross-origin (with cors_origins).",
    )
    cors_allow_headers: list[str] = Field(
        default_factory=lambda: [
            "Authorization",
            "Content-Type",
            "If-Match",
            "X-AutoAir-CSRF",
            "X-Requested-With",
        ],
        description="Standalone: request headers allowed cross-origin.",
    )
    cors_expose_headers: list[str] = Field(
        default_factory=lambda: ["ETag", "Retry-After"],
        description="Standalone: response headers readable cross-origin.",
    )
    cors_max_age: int = Field(
        default=600, ge=0, description="Standalone: seconds browsers cache a CORS preflight."
    )
    cors_allow_credentials: bool = Field(
        default=True,
        description="Standalone: allow cookies/credentials cross-origin (never with a '*' origin).",
    )

    # --- policy (organisation rules, enforced on validate & deploy)
    require_owner: bool = Field(default=False, description="Require an owner on every task.")
    require_description: bool = Field(
        default=False, description="Require a DAG description or doc_md."
    )
    min_retries: int | None = Field(default=None, description="Minimum retries of every task.")
    required_tags: list[str] = Field(default_factory=list, description="Tags every DAG must carry.")
    forbidden_operators: list[str] = Field(
        default_factory=list, description="fnmatch patterns of operators DAGs may not use."
    )
    max_tasks: int | None = Field(default=None, description="Most tasks a DAG may have.")
    dag_id_prefix: str | None = Field(default=None, description="Prefix every dag_id must have.")
    python_ref_allow: list[str] = Field(
        default_factory=lambda: ["*"],
        description="fnmatch patterns of Python objects DAGs may reference "
        '(e.g. ["acme_airflow.*", "airflow.providers.*"]); references need allow_python_code.',
    )
    policy: PolicyRules = Field(
        default_factory=PolicyRules,
        description="Validation rules: privileged operators, dag_id/task_id patterns, secret "
        "detection, known template names (AUTOAIR_POLICY__<FIELD>).",
    )

    # --- compatibility / environments / teams
    target_airflow_version: str | None = Field(
        default=None,
        description="Validate against this Airflow version instead of the installed one.",
    )
    environments: list[str] = Field(
        default_factory=list, description="Known deployment environments, e.g. dev, staging, prod."
    )
    default_environment: str | None = Field(
        default=None, description="Environment preselected in the UI."
    )
    team: str | None = Field(default=None, description="Multi-team: this deployment's team.")

    # --- state locations (fsspec URLs; default under the output dir)
    templates_url: str | None = Field(
        default=None, description="Saved templates (fsspec URL; default <state>/templates)."
    )
    templates_history_limit: int = Field(
        default=100,
        ge=1,
        description="Operator templates: how many versions of each template are kept readable "
        "in its version history (oldest dropped first; the current version is always kept).",
    )
    audit_url: str | None = Field(
        default=None, description="Audit log (fsspec URL; default <state>/audit)."
    )
    audit: AuditSettings = Field(
        default_factory=AuditSettings,
        description="Audit sinks: file, db, log, otel (AUTOAIR_AUDIT__SINKS).",
    )
    icons_url: str | None = Field(
        default=None, description="Custom icons + icon assignments (fsspec URL)."
    )
    cache_dir: Path | None = Field(
        default=None,
        description="Disk cache (catalog builds…; default $XDG_CACHE_HOME/autoair).",
    )
    catalog_file: Path | None = Field(
        default=None, description="Precomputed catalog (autoair catalog build) for fast startup."
    )
    catalog_strategy: Literal["static", "import"] = Field(
        default="static",
        description="static: parse provider code (no imports); import: import + inspect.",
    )
    catalog_fallback_timeout: float = Field(
        default=300,
        gt=0,
        description="Seconds the catalog build may spend introspecting classes in a subprocess.",
    )

    # --- timeouts & limits (operator-tunable)
    airflow_timeout: float = Field(
        default=15, gt=0, description="Timeout (s) of each call to Airflow's REST API."
    )
    auth_timeout: float = Field(
        default=10, gt=0, description="Standalone: timeout (s) of the bearer-token check."
    )
    auth_cache_ttl: float = Field(
        default=60,
        ge=0,
        description="Standalone: seconds a verified bearer token is trusted without "
        "asking Airflow again (0 checks every request).",
    )
    import_check_timeout: float = Field(
        default=60, gt=0, description="Timeout (s) of a DagBag import check."
    )
    python_import_timeout: float = Field(
        default=90, gt=0, description="Timeout (s) of importing a hand-written Python DAG."
    )
    dagbag_pool: bool = Field(
        default=True, description="Warm DagBag worker processes for import checks."
    )
    dagbag_pool_size: int = Field(
        default=2, ge=1, le=64, description="Warm DagBag worker processes."
    )
    dagbag_pool_max_tasks: int = Field(
        default=50, ge=1, description="Checks per DagBag worker before it is recycled."
    )
    dagbag_pool_start_timeout: float = Field(
        default=120, gt=0, description="Seconds a DagBag worker may take to start (import Airflow)."
    )

    # --- feature areas (AUTOAIR_<SECTION>__<FIELD>)
    runner: RunnerSettings = Field(
        default_factory=RunnerSettings, description="Test runs from the builder."
    )
    editor: EditorSettings = Field(
        default_factory=EditorSettings, description="Code editor services (completion, lint)."
    )
    lsp: LspSettings = Field(
        default_factory=LspSettings,
        description="Language servers behind features.ide_language_server (AUTOAIR_LSP__*).",
    )
    codegen: CodegenSettings = Field(
        default_factory=CodegenSettings, description="Generated DAG files."
    )
    interchange: InterchangeSettings = Field(
        default_factory=InterchangeSettings, description="DAG imports (Python, YAML, JSON)."
    )
    preserve: PreserveSettings = Field(
        default_factory=PreserveSettings,
        description="Editing existing Python DAG files: source policies and ejected-file "
        "protection (AUTOAIR_PRESERVE__<FIELD>).",
    )
    icons: IconSettings = Field(default_factory=IconSettings, description="Custom node icons.")
    sandbox: SandboxSettings = Field(
        default_factory=SandboxSettings,
        description="Environment of processes running DAG code (secrets removed).",
    )
    server: ServerSettings = Field(
        default_factory=ServerSettings, description="HTTP server and Airflow API client."
    )
    plugin: PluginSettings = Field(
        default_factory=PluginSettings, description="Airflow UI integration (plugin mode)."
    )
    outbound: OutboundSettings = Field(
        default_factory=OutboundSettings,
        description="TLS (CA bundle, client certificates, verification) and proxies of every "
        "outgoing connection (AUTOAIR_OUTBOUND__<FIELD>).",
    )
    logging: LoggingSettings = Field(
        default_factory=LoggingSettings, description="Log level and format (standalone)."
    )
    limits: LimitsSettings = Field(
        default_factory=LimitsSettings,
        description="Rate limits of expensive endpoints, per user and role.",
    )
    ui: UiSettings = Field(
        default_factory=UiSettings,
        description="UI branding (title, logo, accent, banner) and defaults (language, time zone).",
    )
    sso: SsoSettings = Field(
        default_factory=SsoSettings,
        description="Sign-in with the Airflow UI session (docs/sso.md).",
    )
    admin: AdminSettings = Field(
        default_factory=AdminSettings, description="Admin panel export and scan limits."
    )
    local: LocalModeSettings = Field(
        default_factory=LocalModeSettings, description="`autoair local` (no Airflow server)."
    )
    validator: ValidatorSettings = Field(
        default_factory=ValidatorSettings,
        description="Import checks in a separate validator service (autoair validator-serve) "
        "and the limits it applies to the processes importing user code "
        "(AUTOAIR_VALIDATOR__<FIELD>).",
    )

    # --- marketplace (signed template indexes; see autoair.marketplace)
    marketplace_sources: list[MarketplaceSource] = Field(
        default_factory=list,
        description='JSON list, e.g. [{"name": "acme", "url": "https://…/index.json", '
        '"public_key": "<base64 ed25519>"}].',
    )
    marketplace_timeout: float = Field(default=15, gt=0, description="Fetch timeout (s).")
    marketplace_cache_ttl: float = Field(
        default=300, ge=0, description="Seconds a fetched index is reused."
    )
    marketplace_max_bytes: int = Field(
        default=5 * 1024 * 1024, ge=1024, description="Largest index / payload accepted."
    )

    # --- knowledge base, AI & MCP (autoair/kb/config.py, autoair/ai/config.py)
    kb: KbSettings = Field(
        default_factory=KbSettings,
        description="Documentation knowledge base: sources (online, git, folders, object "
        "storage, bundles), store (local, pgvector, Elasticsearch), refresh and retrieval "
        "(AUTOAIR_KB__<FIELD>).",
    )
    ai: AiSettings = Field(
        default_factory=AiSettings,
        description="AI assistance, agent, retrieval, research, guardrails, AI service and "
        "MCP (AUTOAIR_AI__<FIELD>).",
    )

    # --- observability
    otel_enabled: bool = Field(
        default=False, description="OpenTelemetry tracing (pip install 'autoair[otel]')."
    )

    # --- real-time collaboration (Yjs over WebSockets; needs autoair[collab])
    collab_secret: SecretStr | None = Field(
        default=None,
        description="HMAC key for collaboration tickets (default: derived from Airflow's "
        "[api_auth] jwt_secret; set it when standalone runs several replicas).",
    )
    collab_ticket_ttl: int = Field(
        default=60,
        ge=10,
        le=86400,
        description="Seconds a collaboration ticket is valid. A ticket opens one connection "
        "(single use); the browser asks for a new one before every (re)connect.",
    )
    collab_max_rooms: int = Field(default=500, ge=1, description="Live editing rooms per process.")
    collab: CollabSettings = Field(
        default_factory=CollabSettings,
        description="Sharing live-editing rooms between replicas (AUTOAIR_COLLAB__<FIELD>).",
    )

    # --- persistence (templates, audit, AI memory, reviews… in files or a SQL database)
    database: DatabaseSettings = Field(
        default_factory=DatabaseSettings,
        description="Where AutoAir keeps its own state: files (default), SQLite or "
        "PostgreSQL (AUTOAIR_DATABASE__<FIELD>).",
    )

    # --- storage / deploy target (local folder, object storage or git)
    storage: StorageSettings = Field(
        default_factory=StorageSettings,
        description="Deploy target: local folder, object storage or git (AUTOAIR_STORAGE__*).",
    )
    team_storage: dict[str, StorageSettings] = Field(
        default_factory=dict,
        description="Multi-team: per-team deploy target (e.g. each team's repo/bundle); DAGs "
        "whose `team` is set are written there.",
    )
    team_dag_id_prefix: bool = Field(
        default=False, description="Multi-team: require dag_id to start with '<team>_'."
    )

    @field_validator("config_file", mode="before")
    @classmethod
    def _config_files(cls, value: Any) -> Any:
        return parse_file_list(value) if isinstance(value, str) else value

    @field_validator("expose_config", mode="before")
    @classmethod
    def _expose_config(cls, value: Any) -> Any:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            text = value.strip().lower().replace("_", "-")
            return {"1": "true", "yes": "true", "0": "false", "no": "false"}.get(text, text)
        return value

    def attach_feature_overrides(self, store: Any) -> None:
        """Runtime overrides (:class:`autoair.feature_overrides.FeatureOverrideStore`) that
        :meth:`effective_features` applies; set by ``build_services``."""
        self._feature_overrides = store

    @property
    def feature_overrides(self) -> Any:
        return self._feature_overrides

    def feature_lock(self, name: str) -> Literal["features_locked", "env"] | None:
        """Why flag ``name`` cannot be overridden at runtime: ``"features_locked"``,
        ``"env"`` (set through the environment) or None."""
        import fnmatch

        if any(fnmatch.fnmatchcase(name, p) for p in self.features_locked):
            return "features_locked"
        if self.origin_of(f"features.{name}") in ("env", "dotenv", "secrets"):
            return "env"
        return None

    def runtime_overrides(self) -> dict[str, bool]:
        """The runtime overrides that apply (unknown and locked flags left out)."""
        store = self._feature_overrides
        if store is None:
            return {}
        try:
            values: dict[str, bool] = store.values()
        except Exception:
            return {}
        known = type(self.features).model_fields
        return {
            name: bool(value)
            for name, value in values.items()
            if name in known and self.feature_lock(name) is None
        }

    def configured_features(self, *, overrides: bool = True) -> dict[str, bool]:
        """The flags before the derived constraints: configured values with the runtime
        overrides applied (``overrides=False``: configured values only)."""
        flags = self.features.model_dump()
        if overrides:
            changed = {**flags, **self.runtime_overrides()}
            # Never both editors off (a racing pair of changes): keep the configured pair.
            if not (changed["visual_editor"] or changed["code_editor"]):
                changed["visual_editor"] = flags["visual_editor"]
                changed["code_editor"] = flags["code_editor"]
            flags = changed
        return flags

    def effective_features(self) -> dict[str, bool]:
        """Feature flags after the runtime overrides (admin panel; locked flags excepted),
        policy (``allow_python_code=false`` turns ``python_code`` off) and what is installed
        (AI and MCP in-process need ``autoair[ai]`` / ``autoair[mcp]``). This is what
        ``/api/meta`` reports and the routers obey."""
        flags = self.configured_features()
        flags["python_code"] = flags["python_code"] and self.allow_python_code
        for name in ("ai", "mcp"):
            flags[name] = flags[name] and self.feature_unavailable(name) is None
        return flags

    def feature_unavailable(self, name: str) -> str | None:
        """Why feature ``name`` cannot run in this installation (``None``: it can): AI in
        this process needs the ``autoair[ai]`` extra unless ``ai.service_url`` proxies it;
        MCP needs ``autoair[mcp]`` unless the AI service serves it (``ai.service.proxy_mcp``).
        """
        if name == "ai" and not self.ai.service_url:
            from autoair.ai.availability import ai_unavailable_reason

            return ai_unavailable_reason()
        if name == "mcp" and not (self.ai.service_url and self.ai.service.proxy_mcp):
            from autoair.mcp.availability import mcp_unavailable_reason

            return mcp_unavailable_reason()
        return None

    def feature(self, name: str) -> bool:
        """Effective feature flag ``name``; an unknown name is a programming error (a typo
        must not silently enable a feature)."""
        flags = self.effective_features()
        if name not in flags:
            raise ValueError(f"unknown feature {name!r} (known: {', '.join(sorted(flags))})")
        return bool(flags[name])

    @property
    def output_dir(self) -> Path:
        return (self.dags_folder / self.subfolder) if self.subfolder else self.dags_folder

    @property
    def state_dir(self) -> Path:
        return self.output_dir / ".autoair"

    def target_version_tuple(self) -> tuple[int, int, int] | None:
        if not self.target_airflow_version:
            return None
        parts = [
            int("".join(c for c in p if c.isdigit()) or 0)
            for p in self.target_airflow_version.split(".")[:3]
        ]
        while len(parts) < 3:
            parts.append(0)
        return parts[0], parts[1], parts[2]
