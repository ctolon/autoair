# Configuration reference

Generated from the `Settings` model by `scripts/gen_config_docs.py`; do not edit by hand
(a test fails when it is stale). The narrative guide is
[configuration.md](configuration.md).

Every option can be set as an **environment variable** (`AUTOAIR_<KEY>`, nested
sections joined with `__`), in a **configuration file** (YAML/JSON/Python: the
dotted key as nested mappings; INI: `[autoair]` / `[autoair.<section>]` sections)
or, for some, as a CLI option. Precedence: init arguments > environment > `.env` >
configuration files > defaults. Lists and maps are JSON in environment variables
and INI files.

**Secret** values are masked as `***` by `GET /api/config` (`expose_config=true`) and
left out with `expose_config=non-sensitive-only`.

693 options.

## Top level

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_CONFIG_FILE` | `config_file` | `list[str]` | `[]` | Configuration files (YAML, JSON, INI, Python file or module:attr), comma separated or a JSON list; later files override earlier ones, environment variables override files. Read from the environment / `autoair serve --config` only. |  |
| `AUTOAIR_EXPOSE_CONFIG` | `expose_config` | `'false' \| 'true' \| 'non-sensitive-only'` | `false` | GET /api/config (admins only): false = disabled; true = every value, secrets masked as ***; non-sensitive-only = secrets left out. Like Airflow's [api] expose_config. |  |
| `AUTOAIR_FEATURES_LOCKED` | `features_locked` | `list[str]` | `[]` | Feature flags (fnmatch patterns, '*' = all) admins cannot switch at runtime from the admin panel: the configured value always wins. Flags set through the environment (AUTOAIR_FEATURES__<NAME>, .env, secrets directory) are locked too. Precedence: locked > runtime override > configured value. |  |
| `AUTOAIR_FEATURES_OVERRIDE_TTL` | `features_override_ttl` | `float` | `5.0` | Seconds a replica keeps the runtime feature overrides before re-reading them from the state backend (how fast other replicas pick up a change made in the admin panel; the replica that made it sees it at once). 0 reads them on every check. |  |
| `AUTOAIR_MODE` | `mode` | `'plugin' \| 'standalone' \| 'local'` | `standalone` | Deployment mode (set by the Airflow plugin / `autoair serve` / `autoair local`). |  |
| `AUTOAIR_DAGS_FOLDER` | `dags_folder` | `path` | `Airflow [core] dags_folder, else ./dags` | Folder of a LocalDagBundle the files are written to. |  |
| `AUTOAIR_SUBFOLDER` | `subfolder` | `str` | `autoair` | Sub-folder inside dags_folder holding AutoAir DAGs. |  |
| `AUTOAIR_AIRFLOW_URL` | `airflow_url` | `str` | `Airflow [api] base_url, else http://localhost:8080` | Airflow REST API base URL (pickers, status, runs, token checks). |  |
| `AUTOAIR_AIRFLOW_SECRETS_BACKEND` | `airflow_secrets_backend` | `bool` | `false` | Airflow resolves connections/variables from a secrets backend (Vault, AWS Secrets Manager…) the REST API cannot list. true hides the pickers' 'not found in Airflow's metastore' hint; unknown ids are never validation errors. |  |
| `AUTOAIR_ALLOW_PYTHON_CODE` | `allow_python_code` | `bool` | `true` | Allow Python code in DAGs (@task bodies, inline functions, script files, Python references). Also off when features.python_code is false. |  |
| `AUTOAIR_ALLOW_PRIVILEGED_OPERATORS` | `allow_privileged_operators` | `bool` | `true` | Allow operators that run arbitrary commands (policy.privileged_operators). |  |
| `AUTOAIR_IMPORT_CHECK_ON_DEPLOY` | `import_check_on_deploy` | `bool` | `true` | Import the DAG with a DagBag before writing it. |  |
| `AUTOAIR_OPERATOR_ALLOW` | `operator_allow` | `list[str]` | `["*"]` | fnmatch patterns of operator class paths shown in the palette. |  |
| `AUTOAIR_OPERATOR_DENY` | `operator_deny` | `list[str]` | `[]` | fnmatch patterns of operator class paths hidden from the palette. |  |
| `AUTOAIR_EXTRA_OPERATOR_MODULES` | `extra_operator_modules` | `list[str]` | `[]` | Single modules scanned for operators. |  |
| `AUTOAIR_EXTRA_OPERATOR_PACKAGES` | `extra_operator_packages` | `list[str]` | `[]` | Installed packages whose every module is scanned for operators (e.g. a company's internal operator library). |  |
| `AUTOAIR_EXTRA_OPERATOR_DISTRIBUTIONS` | `extra_operator_distributions` | `list[str]` | `[]` | pip distributions whose every module is scanned for operators, e.g. acme-airflow-ops (found even without an Airflow requirement or provider entry point). |  |
| `AUTOAIR_EXTRA_OPERATOR_PATHS` | `extra_operator_paths` | `list[path]` | `[]` | Operator files or package folders; their import root is inferred from __init__.py files (/opt/acme/acme_ops/jira.py -> acme_ops.jira). |  |
| `AUTOAIR_CUSTOM_OPERATOR_PATHS` | `custom_operator_paths` | `list[path]` | `[]` | Folders (import roots) scanned for custom operators, e.g. include/. |  |
| `AUTOAIR_DISCOVER_AIRFLOW_FOLDERS` | `discover_airflow_folders` | `bool` | `true` | Also scan Airflow's plugins_folder and dags_folder for custom operators. |  |
| `AUTOAIR_SCAN_PROVIDER_PACKAGES` | `scan_provider_packages` | `bool` | `true` | Scan every module of installed providers, not only those declared in provider.yaml (finds undeclared and third-party provider operators). |  |
| `AUTOAIR_CORS_ORIGINS` | `cors_origins` | `list[str]` | `[]` | Standalone: allowed CORS origins. |  |
| `AUTOAIR_CORS_ALLOW_METHODS` | `cors_allow_methods` | `list[str]` | `["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]` | Standalone: methods allowed cross-origin (with cors_origins). |  |
| `AUTOAIR_CORS_ALLOW_HEADERS` | `cors_allow_headers` | `list[str]` | `["Authorization", "Content-Type", "If-Match", "X-AutoAir-CSRF", "X-Requested-With"]` | Standalone: request headers allowed cross-origin. | yes |
| `AUTOAIR_CORS_EXPOSE_HEADERS` | `cors_expose_headers` | `list[str]` | `["ETag", "Retry-After"]` | Standalone: response headers readable cross-origin. | yes |
| `AUTOAIR_CORS_MAX_AGE` | `cors_max_age` | `int` | `600` | Standalone: seconds browsers cache a CORS preflight. |  |
| `AUTOAIR_CORS_ALLOW_CREDENTIALS` | `cors_allow_credentials` | `bool` | `true` | Standalone: allow cookies/credentials cross-origin (never with a '*' origin). | yes |
| `AUTOAIR_REQUIRE_OWNER` | `require_owner` | `bool` | `false` | Require an owner on every task. |  |
| `AUTOAIR_REQUIRE_DESCRIPTION` | `require_description` | `bool` | `false` | Require a DAG description or doc_md. |  |
| `AUTOAIR_MIN_RETRIES` | `min_retries` | `int \| None` | `None` | Minimum retries of every task. |  |
| `AUTOAIR_REQUIRED_TAGS` | `required_tags` | `list[str]` | `[]` | Tags every DAG must carry. |  |
| `AUTOAIR_FORBIDDEN_OPERATORS` | `forbidden_operators` | `list[str]` | `[]` | fnmatch patterns of operators DAGs may not use. |  |
| `AUTOAIR_MAX_TASKS` | `max_tasks` | `int \| None` | `None` | Most tasks a DAG may have. |  |
| `AUTOAIR_DAG_ID_PREFIX` | `dag_id_prefix` | `str \| None` | `None` | Prefix every dag_id must have. |  |
| `AUTOAIR_PYTHON_REF_ALLOW` | `python_ref_allow` | `list[str]` | `["*"]` | fnmatch patterns of Python objects DAGs may reference (e.g. ["acme_airflow.*", "airflow.providers.*"]); references need allow_python_code. |  |
| `AUTOAIR_TARGET_AIRFLOW_VERSION` | `target_airflow_version` | `str \| None` | `None` | Validate against this Airflow version instead of the installed one. |  |
| `AUTOAIR_ENVIRONMENTS` | `environments` | `list[str]` | `[]` | Known deployment environments, e.g. dev, staging, prod. |  |
| `AUTOAIR_DEFAULT_ENVIRONMENT` | `default_environment` | `str \| None` | `None` | Environment preselected in the UI. |  |
| `AUTOAIR_TEAM` | `team` | `str \| None` | `None` | Multi-team: this deployment's team. |  |
| `AUTOAIR_TEMPLATES_URL` | `templates_url` | `str \| None` | `None` | Saved templates (fsspec URL; default <state>/templates). |  |
| `AUTOAIR_TEMPLATES_HISTORY_LIMIT` | `templates_history_limit` | `int` | `100` | Operator templates: how many versions of each template are kept readable in its version history (oldest dropped first; the current version is always kept). |  |
| `AUTOAIR_AUDIT_URL` | `audit_url` | `str \| None` | `None` | Audit log (fsspec URL; default <state>/audit). |  |
| `AUTOAIR_ICONS_URL` | `icons_url` | `str \| None` | `None` | Custom icons + icon assignments (fsspec URL). |  |
| `AUTOAIR_CACHE_DIR` | `cache_dir` | `path \| None` | `None` | Disk cache (catalog builds…; default $XDG_CACHE_HOME/autoair). |  |
| `AUTOAIR_CATALOG_FILE` | `catalog_file` | `path \| None` | `None` | Precomputed catalog (autoair catalog build) for fast startup. |  |
| `AUTOAIR_CATALOG_STRATEGY` | `catalog_strategy` | `'static' \| 'import'` | `static` | static: parse provider code (no imports); import: import + inspect. |  |
| `AUTOAIR_CATALOG_FALLBACK_TIMEOUT` | `catalog_fallback_timeout` | `float` | `300` | Seconds the catalog build may spend introspecting classes in a subprocess. |  |
| `AUTOAIR_AIRFLOW_TIMEOUT` | `airflow_timeout` | `float` | `15` | Timeout (s) of each call to Airflow's REST API. |  |
| `AUTOAIR_AUTH_TIMEOUT` | `auth_timeout` | `float` | `10` | Standalone: timeout (s) of the bearer-token check. |  |
| `AUTOAIR_AUTH_CACHE_TTL` | `auth_cache_ttl` | `float` | `60` | Standalone: seconds a verified bearer token is trusted without asking Airflow again (0 checks every request). |  |
| `AUTOAIR_IMPORT_CHECK_TIMEOUT` | `import_check_timeout` | `float` | `60` | Timeout (s) of a DagBag import check. |  |
| `AUTOAIR_PYTHON_IMPORT_TIMEOUT` | `python_import_timeout` | `float` | `90` | Timeout (s) of importing a hand-written Python DAG. |  |
| `AUTOAIR_DAGBAG_POOL` | `dagbag_pool` | `bool` | `true` | Warm DagBag worker processes for import checks. |  |
| `AUTOAIR_DAGBAG_POOL_SIZE` | `dagbag_pool_size` | `int` | `2` | Warm DagBag worker processes. |  |
| `AUTOAIR_DAGBAG_POOL_MAX_TASKS` | `dagbag_pool_max_tasks` | `int` | `50` | Checks per DagBag worker before it is recycled. |  |
| `AUTOAIR_DAGBAG_POOL_START_TIMEOUT` | `dagbag_pool_start_timeout` | `float` | `120` | Seconds a DagBag worker may take to start (import Airflow). |  |
| `AUTOAIR_MARKETPLACE_SOURCES` | `marketplace_sources` | `list[MarketplaceSource]` | `[]` | JSON list, e.g. [{"name": "acme", "url": "https://…/index.json", "public_key": "<base64 ed25519>"}]. |  |
| `AUTOAIR_MARKETPLACE_TIMEOUT` | `marketplace_timeout` | `float` | `15` | Fetch timeout (s). |  |
| `AUTOAIR_MARKETPLACE_CACHE_TTL` | `marketplace_cache_ttl` | `float` | `300` | Seconds a fetched index is reused. |  |
| `AUTOAIR_MARKETPLACE_MAX_BYTES` | `marketplace_max_bytes` | `int` | `5242880` | Largest index / payload accepted. |  |
| `AUTOAIR_OTEL_ENABLED` | `otel_enabled` | `bool` | `false` | OpenTelemetry tracing (pip install 'autoair[otel]'). |  |
| `AUTOAIR_COLLAB_SECRET` | `collab_secret` | `secret str \| None` | `None` | HMAC key for collaboration tickets (default: derived from Airflow's [api_auth] jwt_secret; set it when standalone runs several replicas). | yes |
| `AUTOAIR_COLLAB_TICKET_TTL` | `collab_ticket_ttl` | `int` | `60` | Seconds a collaboration ticket is valid. A ticket opens one connection (single use); the browser asks for a new one before every (re)connect. |  |
| `AUTOAIR_COLLAB_MAX_ROOMS` | `collab_max_rooms` | `int` | `500` | Live editing rooms per process. |  |
| `AUTOAIR_TEAM_STORAGE` | `team_storage` | `dict[str, StorageSettings]` | `{}` | Multi-team: per-team deploy target (e.g. each team's repo/bundle); DAGs whose `team` is set are written there. |  |
| `AUTOAIR_TEAM_DAG_ID_PREFIX` | `team_dag_id_prefix` | `bool` | `false` | Multi-team: require dag_id to start with '<team>_'. |  |

## `features`

Feature switches (AUTOAIR_FEATURES__<NAME>).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_FEATURES__AI` | `features.ai` | `bool` | `true` | AI assistance (generate, review, fix, explain…). |  |
| `AUTOAIR_FEATURES__MCP` | `features.mcp` | `bool` | `true` | Model Context Protocol endpoint for agents. |  |
| `AUTOAIR_FEATURES__COLLAB` | `features.collab` | `bool` | `true` | Real-time co-editing. |  |
| `AUTOAIR_FEATURES__MARKETPLACE` | `features.marketplace` | `bool` | `true` | Template marketplaces. |  |
| `AUTOAIR_FEATURES__TEMPLATES` | `features.templates` | `bool` | `true` | Template gallery and saving templates. |  |
| `AUTOAIR_FEATURES__IMPORTS` | `features.imports` | `bool` | `true` | Import Python/YAML/Airflow DAGs. |  |
| `AUTOAIR_FEATURES__TEST_RUNS` | `features.test_runs` | `bool` | `true` | Test a task/DAG from the builder. |  |
| `AUTOAIR_FEATURES__RUNS` | `features.runs` | `bool` | `true` | Trigger, backfill, clear and pause from the UI. |  |
| `AUTOAIR_FEATURES__DEPLOY` | `features.deploy` | `bool` | `true` | Deploy (write/push DAG files). |  |
| `AUTOAIR_FEATURES__CUSTOM_ICONS` | `features.custom_icons` | `bool` | `true` | Upload custom node icons. |  |
| `AUTOAIR_FEATURES__PYTHON_CODE` | `features.python_code` | `bool` | `true` | Python code in DAGs (@task bodies, inline functions, script files, raw code DAGs). |  |
| `AUTOAIR_FEATURES__AIRFLOW` | `features.airflow` | `bool` | `true` | Features that call an Airflow server: connection/pool/variable pickers, DAG status, import from Airflow (off in `autoair local`). |  |
| `AUTOAIR_FEATURES__IAM` | `features.iam` | `bool` | `true` | AutoAir's own authorization (roles, role mapping, custom views, four-eyes approvals, direct-push roles). false: every signed-in user gets the Authenticated baseline plus what Airflow's DAG/configuration permissions grant; no AutoAir roles, mappings or approvals (see docs/iam.md). |  |
| `AUTOAIR_FEATURES__VISUAL_EDITOR` | `features.visual_editor` | `bool` | `true` | The visual (canvas) DAG editor. At least one of visual_editor and code_editor must be on; with only one, the builder opens in it without a toggle. Off: code editing only (no canvas or palette; visual DAGs open in the code mode). |  |
| `AUTOAIR_FEATURES__CODE_EDITOR` | `features.code_editor` | `bool` | `true` | Direct code editing: the full-screen code mode of visual DAGs (edit the generated file; applying it re-imports the code into the spec; needs spec:write, code:execute and python_code, since the importer executes the code) and raw code DAGs (hand-written files deployed as written; need code:raw and python_code). Off: visual editor only. |  |
| `AUTOAIR_FEATURES__CODE_EDIT_INLINE` | `features.code_edit_inline` | `bool` | `false` | Make the bottom panel's Code tab editable (same import-on-apply as the code mode). |  |
| `AUTOAIR_FEATURES__KB` | `features.kb` | `bool` | `true` | Documentation knowledge base: search over the Airflow and Astronomer docs for agents, MCP (kb_search, kb_sources, kb_status) and the admin panel (see the kb section). Works without autoair[ai] (keyword search). |  |
| `AUTOAIR_FEATURES__AUTOAIR_DOCS` | `features.autoair_docs` | `bool` | `true` | AutoAir's own documentation for agents: the MCP tools search_autoair_docs, read_autoair_doc and list_autoair_docs, which read the documentation packaged with this version (offline, no knowledge-base store). The knowledge base's autoair source is switched separately (kb.sources). |  |
| `AUTOAIR_FEATURES__SOURCE_PRESERVE` | `features.source_preserve` | `bool` | `true` | Source-preserving deploys: DAGs whose source policy is preserve or strict-preserve (by default those imported from or edited as hand-written Python) get each change applied to their existing file as a minimal patch (LibCST) instead of a regenerated file. Off: every DAG file is generated; hand-written files are then protected by preserve.ejected_files. |  |
| `AUTOAIR_FEATURES__STRICT_SOURCE_PRESERVE` | `features.strict_source_preserve` | `bool` | `false` | Run every preserve deploy as strict-preserve: refuse each change that cannot be applied exactly (no replaced variable references, operator class changes or group moves through task_group=). |  |
| `AUTOAIR_FEATURES__IDE_LANGUAGE_SERVER` | `features.ide_language_server` | `bool` | `false` | IDE-grade IntelliSense in code editors through a per-session language server (basedpyright or pylsp, bash-language-server; see the lsp section). Off: the built-in jedi completions. |  |
| `AUTOAIR_FEATURES__BLUEPRINTS` | `features.blueprints` | `bool` | `true` | Astronomer Blueprint templates (airflow-blueprint) as canvas nodes: the palette's Blueprints section, config forms and task previews (see the blueprints section). Reading config schemas needs autoair[blueprint]. |  |

## `iam`

Authorization: AutoAir roles/permissions governed through Airflow.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_IAM__STRATEGIES` | `iam.strategies` | `list['airflow_permissions' \| 'role_mapping' \| 'custom_view']` | `["airflow_permissions"]` | How permissions are derived; the union of all listed strategies applies. 'airflow_permissions': from Airflow's DAG/configuration permissions (default); 'role_mapping': Airflow role names -> AutoAir roles (FAB roles, SimpleAuthManager role, or the token's role claim standalone); 'custom_view': each AutoAir permission is an Airflow custom-view permission ('can_create on AutoAir Deploy'…) granted in Airflow (FAB, Keycloak, AWS auth managers; plugin mode). |  |
| `AUTOAIR_IAM__ROLE_MAPPING` | `iam.role_mapping` | `dict[str, list[str]]` | `{"Admin": ["Admin"], "Op": ["Author", "Reviewer", "Deployer", "Operator", "TemplateManager"], "User": ["Author", "Operator"], "Viewer": ["Viewer"]}` | 'role_mapping' strategy: Airflow role name (case-insensitive) -> AutoAir roles, e.g. {"data-eng": ["Author", "Deployer"]}. |  |
| `AUTOAIR_IAM__ROLE_PREFIX` | `iam.role_prefix` | `str` | `AutoAir ` | 'role_mapping' strategy: an Airflow role named '<prefix><AutoAir role>' (e.g. 'AutoAir Deployer') grants that AutoAir role; empty disables this. |  |
| `AUTOAIR_IAM__ROLES_CLAIMS` | `iam.roles_claims` | `list[str]` | `["role", "roles"]` | Standalone 'role_mapping': JWT claims holding the Airflow role(s). Only used when Airflow verified the token (the default; not with --no-auth). |  |
| `AUTOAIR_IAM__ROLES` | `iam.roles` | `dict[str, list[str]]` | `{}` | Custom roles or overrides of built-in ones: name -> permissions (wildcards allowed), e.g. {"Auditor": ["spec:read", "audit:read"]}. |  |
| `AUTOAIR_IAM__DEFAULT_ROLES` | `iam.default_roles` | `list[str]` | `[]` | Roles every signed-in user gets, whatever the strategy. |  |
| `AUTOAIR_IAM__ANONYMOUS_ROLES` | `iam.anonymous_roles` | `list[str]` | `["Viewer"]` | Roles of unauthenticated callers (serve/mcp-serve/ai-serve --no-auth, which only bind to loopback); such users never get direct-push rights through storage.git.direct_push_roles. Set e.g. ['Admin'] to let them edit and deploy. |  |
| `AUTOAIR_IAM__CUSTOM_VIEW_PREFIX` | `iam.custom_view_prefix` | `str` | `AutoAir` | 'custom_view' strategy: Airflow resource names are '<prefix> <resource>' (e.g. 'AutoAir Deploy'). |  |
| `AUTOAIR_IAM__DIRECT_PUSH_ROLES` | `iam.direct_push_roles` | `list[str]` | `["Admin"]` | AutoAir roles holding 'deploy:direct_push' (push straight to the branch in git PR mode); a deploy target's storage.git.direct_push_roles overrides it when set. Empty: nobody, except users granted the permission explicitly. |  |
| `AUTOAIR_IAM__FORCE_PR_WITHOUT_DIRECT_PUSH` | `iam.force_pr_without_direct_push` | `bool` | `false` | Git push mode: users without 'deploy:direct_push' have their deploys opened as pull requests instead of pushed. |  |
| `AUTOAIR_IAM__REQUIRE_APPROVAL` | `iam.require_approval` | `bool` | `false` | Four-eyes rule: a deploy that goes live needs an approval of that exact spec version by another user holding 'review' (pull requests are exempt). A deploy without one is stored as a proposal (HTTP 202) that a reviewer approves and anyone holding 'deploy' then deploys. |  |
| `AUTOAIR_IAM__PROPOSALS_URL` | `iam.proposals_url` | `str \| None` | `None` | Where deploy proposals and their reviews are kept (fsspec URL; default <state>/proposals). Use shared storage when several replicas serve the API. |  |
| `AUTOAIR_IAM__PROPOSAL_TTL_SECONDS` | `iam.proposal_ttl_seconds` | `float` | `604800` | Deploy proposals (iam.require_approval) older than this are discarded. |  |
| `AUTOAIR_IAM__IMPORT_CHECK_REQUIRES_CODE_EXECUTE` | `iam.import_check_requires_code_execute` | `bool` | `true` | A deploy runs the import check (executes the DAG file in a sandboxed subprocess on the AutoAir server) only for callers holding 'code:execute'; for others it is skipped and reported as an info problem (static validation still applies). |  |
| `AUTOAIR_IAM__CUSTOM_VIEW_CACHE_SECONDS` | `iam.custom_view_cache_seconds` | `float` | `30` | 'custom_view' strategy: how long the auth manager's answers are cached per user and token (0 disables the cache). |  |
| `AUTOAIR_IAM__AUDIT_DENIALS` | `iam.audit_denials` | `bool` | `true` | Record permission denials in the audit trail. |  |
| `AUTOAIR_IAM__LOGIN_ENABLED` | `iam.login_enabled` | `bool` | `true` | Standalone: POST /api/auth/login exchanges an Airflow username/password for an Airflow access token. |  |
| `AUTOAIR_IAM__LOGIN_MAX_FAILURES` | `iam.login_max_failures` | `int` | `5` | Standalone login: failed attempts per client IP and per username within login_window_seconds before further attempts are refused (429). |  |
| `AUTOAIR_IAM__LOGIN_MAX_TRACKED_KEYS` | `iam.login_max_tracked_keys` | `int` | `10000` | Standalone login: hard cap of client IPs/usernames tracked by the rate limiter (least recently used ones are forgotten first). |  |
| `AUTOAIR_IAM__LOGIN_WINDOW_SECONDS` | `iam.login_window_seconds` | `float` | `300` | Standalone login: rate-limit window (s). |  |
| `AUTOAIR_IAM__LOGIN_TIMEOUT` | `iam.login_timeout` | `float` | `10` | Standalone login: timeout (s) of Airflow's token call. |  |

## `iam.airflow_permission_roles`

'airflow_permissions' strategy: roles per standard Airflow permission.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_IAM__AIRFLOW_PERMISSION_ROLES__AUTHENTICATED` | `iam.airflow_permission_roles.authenticated` | `list[str]` | `["Authenticated"]` | Roles of every signed-in user (Airflow still enforces proxied calls). |  |
| `AUTOAIR_IAM__AIRFLOW_PERMISSION_ROLES__DAG_READ` | `iam.airflow_permission_roles.dag_read` | `list[str]` | `["Viewer"]` | Roles granted by DAG read (GET). |  |
| `AUTOAIR_IAM__AIRFLOW_PERMISSION_ROLES__DAG_EDIT` | `iam.airflow_permission_roles.dag_edit` | `list[str]` | `["Author", "Reviewer", "Deployer", "Operator", "TemplateManager"]` | Roles granted by DAG edit (PUT). |  |
| `AUTOAIR_IAM__AIRFLOW_PERMISSION_ROLES__CONFIGURATION_EDIT` | `iam.airflow_permission_roles.configuration_edit` | `list[str]` | `["Admin"]` | Roles granted by configuration edit (PUT); plugin mode only — standalone cannot check it and never infers direct-push rights from it. |  |
| `AUTOAIR_IAM__AIRFLOW_PERMISSION_ROLES__UNVERIFIED` | `iam.airflow_permission_roles.unverified` | `list[str]` | `["Viewer"]` | Roles granted instead when AutoAir cannot ask Airflow for one of the permissions above (standalone `autoair serve` only verifies the token): a permission that cannot be checked never grants its own roles. Grant more with the 'role_mapping' strategy (the token's role claim) or iam.default_roles. |  |

## `discovery`

Operator discovery filters (AUTOAIR_DISCOVERY__<FIELD>).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_DISCOVERY__MAX_FILE_BYTES` | `discovery.max_file_bytes` | `int` | `2000000` | Python files larger than this are not scanned. |  |
| `AUTOAIR_DISCOVERY__SKIP_DIRS` | `discovery.skip_dirs` | `list[str]` | `["__pycache__", ".git", ".hg", ".venv", "venv", ".tox", ".nox", "node_modules", "site-packages", ".autoair", ".ipynb_checkpoints"]` | Directory names never walked in custom operator folders/packages (dot-directories are always skipped). Overrides the built-in list. |  |
| `AUTOAIR_DISCOVERY__SKIP_DIRS_EXTRA` | `discovery.skip_dirs_extra` | `list[str]` | `[]` | Directory names skipped in addition to skip_dirs. |  |
| `AUTOAIR_DISCOVERY__SKIP_MODULE_PATTERN` | `discovery.skip_module_pattern` | `str` | `(^\|\.)(tests?\|example_dags\|system\|migrations\|_vendor\|decorators)(\.\|$)\|(^\|\.)conftest$` | Regex over dotted module names that are never scanned (tests, examples, vendored code…). Overrides the built-in pattern; empty disables it. |  |
| `AUTOAIR_DISCOVERY__SKIP_MODULE_PATTERNS_EXTRA` | `discovery.skip_module_patterns_extra` | `list[str]` | `[]` | More module-name regexes; a module matching any pattern is skipped. |  |
| `AUTOAIR_DISCOVERY__SKIP_CLASS_PATTERN` | `discovery.skip_class_pattern` | `str` | `Decorated(Sensor)?Operator$` | Regex over class names left out of the palette (internal TaskFlow implementation classes). Overrides the built-in pattern; empty disables it. |  |
| `AUTOAIR_DISCOVERY__SKIP_CLASS_PATTERNS_EXTRA` | `discovery.skip_class_patterns_extra` | `list[str]` | `[]` | More class-name regexes; a class matching any pattern is left out. |  |
| `AUTOAIR_DISCOVERY__CLASS_HINT_PATTERN` | `discovery.class_hint_pattern` | `str` | `Operator\|Sensor\|Notifier` | Pre-filter: a file is only parsed when it defines a class and its text matches this regex. Overrides the built-in pattern; empty disables the pre-filter (every file defining a class is parsed). |  |
| `AUTOAIR_DISCOVERY__CLASS_HINT_PATTERNS_EXTRA` | `discovery.class_hint_patterns_extra` | `list[str]` | `[]` | More pre-filter regexes (e.g. 'Task' for task classes named *Task); a file matching any pattern is parsed. |  |
| `AUTOAIR_DISCOVERY__EXCLUDE_DISTRIBUTIONS` | `discovery.exclude_distributions` | `list[str]` | `["apache-airflow", "apache-airflow-core", "apache-airflow-task-sdk", "autoair"]` | fnmatch patterns of distribution names (normalised, e.g. 'apache-airflow-core') never scanned as operator libraries. Overrides the built-in list. |  |
| `AUTOAIR_DISCOVERY__EXCLUDE_DISTRIBUTIONS_EXTRA` | `discovery.exclude_distributions_extra` | `list[str]` | `[]` | More distribution name patterns never scanned (e.g. 'acme-airflow-*'). |  |
| `AUTOAIR_DISCOVERY__PARALLEL_THRESHOLD` | `discovery.parallel_threshold` | `int` | `300` | Modules to parse before parsing in parallel processes. |  |
| `AUTOAIR_DISCOVERY__WORKERS` | `discovery.workers` | `int \| None` | `None` | Parallel parser processes (default: min(4, CPU count)). |  |
| `AUTOAIR_DISCOVERY__CACHE_MAX_FILES` | `discovery.cache_max_files` | `int` | `8` | Catalog cache files kept in the cache directory. |  |
| `AUTOAIR_DISCOVERY__PROBE_TIMEOUT` | `discovery.probe_timeout` | `float` | `120` | Seconds the import probe of operator modules may take. |  |

## `catalog`

Catalog presentation: connection-parameter detection, palette groups, parameter widgets, package metadata (AUTOAIR_CATALOG__<FIELD>).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_CATALOG__CONNECTION_PARAM_PATTERNS` | `catalog.connection_param_patterns` | `list[str \| ConnectionParamPattern]` | `["(^\|_)conn(ection)?_id$"]` | Regexes over constructor parameter names that hold a connection id (connection picker). An entry is a regex or {pattern, conn_type}. Overrides the built-in list (the *_conn_id / *_connection_id naming); changing it rebuilds the catalog. |  |
| `AUTOAIR_CATALOG__CONNECTION_PARAM_PATTERNS_EXTRA` | `catalog.connection_param_patterns_extra` | `list[str \| ConnectionParamPattern]` | `[]` | More connection-parameter regexes (e.g. '_dsn$' or {pattern: '^warehouse$', conn_type: snowflake}), added to connection_param_patterns. |  |
| `AUTOAIR_CATALOG__GROUPS` | `catalog.groups` | `list[GroupRule]` | `[]` | Palette groups: [{match: regex, group: 'Bifrost', order: 10}] (first match wins). Operators without a group are listed under their provider/package. |  |
| `AUTOAIR_CATALOG__DEFAULT_GROUP_ORDER` | `catalog.default_group_order` | `int` | `100` | Sort key of the palette groups no rule names (provider groups). |  |
| `AUTOAIR_CATALOG__PARAM_OVERRIDES` | `catalog.param_overrides` | `dict[str, dict[str, ParamOverride]]` | `{}` | Parameter widgets per operator: {'<class path fnmatch pattern>': {'<param>': {widget, enum, conn_type, label, help, required, hidden, default}}}. Every matching pattern applies, more specific (longer) patterns last. |  |
| `AUTOAIR_CATALOG__PACKAGE_METADATA` | `catalog.package_metadata` | `bool` | `true` | Read operator metadata shipped by packages (autoair.yaml / autoair.json at the package root, the autoair.operator_metadata entry point group). |  |
| `AUTOAIR_CATALOG__METADATA_MAX_BYTES` | `catalog.metadata_max_bytes` | `int` | `524288` | Largest package metadata file read. |  |
| `AUTOAIR_CATALOG__PACKAGE_ICON_MAX_BYTES` | `catalog.package_icon_max_bytes` | `int` | `65536` | Largest SVG icon a package may ship (sanitised before it is served). |  |

## `blueprints`

Where the blueprint catalog finds Astronomer Blueprint templates (AUTOAIR_BLUEPRINTS__<FIELD>).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_BLUEPRINTS__ENTRY_POINTS` | `blueprints.entry_points` | `bool` | `true` | Discover installed packages that advertise blueprint modules under the `airflow_blueprint.blueprints` entry-point group (the blueprint project's own sharing mechanism). false: only template_dirs and modules. |  |
| `AUTOAIR_BLUEPRINTS__TEMPLATE_DIRS` | `blueprints.template_dirs` | `list[str]` | `[]` | Folders scanned for blueprint templates (*.py files defining Blueprint[Config] subclasses), like the blueprint registry scans the dags folder. A file's module name is its path relative to the folder, so generated DAGs import it the way Airflow does when the folder is the dags folder (on sys.path). The files are imported (in a child process) to read schemas: list only folders that admins control, never one DAG authors deploy to. JSON list in the environment. Empty: no folders. |  |
| `AUTOAIR_BLUEPRINTS__MODULES` | `blueprints.modules` | `list[str]` | `[]` | Additional importable modules holding blueprint templates (dotted names). JSON list in the environment. |  |
| `AUTOAIR_BLUEPRINTS__MAX_FILES` | `blueprints.max_files` | `int` | `2000` | Most Python files read per catalog build across template_dirs, modules and entry-point packages; files beyond it are reported as catalog errors. |  |
| `AUTOAIR_BLUEPRINTS__SCHEMA_TIMEOUT` | `blueprints.schema_timeout` | `float` | `120.0` | Seconds the child process may take to import the blueprint modules and read their config schemas (one process per catalog build). |  |
| `AUTOAIR_BLUEPRINTS__RESCAN_INTERVAL` | `blueprints.rescan_interval` | `float` | `30.0` | Seconds between checks whether template files or packages changed (a cheap stat of the sources); a change rebuilds the catalog. 0: check on every request. |  |
| `AUTOAIR_BLUEPRINTS__PREVIEW_TIMEOUT` | `blueprints.preview_timeout` | `float` | `60.0` | Seconds a task preview of one blueprint step may take (rendered in the warm DagBag worker pool). |  |

## `policy`

Validation rules: privileged operators, dag_id/task_id patterns, secret detection, known template names (AUTOAIR_POLICY__<FIELD>).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_POLICY__PRIVILEGED_OPERATORS` | `policy.privileged_operators` | `list[str]` | `["airflow.providers.standard.operators.bash.BashOperator", "airflow.providers.standard.sensors.bash.BashSensor"]` | Operator class paths (fnmatch) that execute arbitrary commands: they need allow_privileged_operators. Overrides the built-in list. |  |
| `AUTOAIR_POLICY__PRIVILEGED_OPERATORS_EXTRA` | `policy.privileged_operators_extra` | `list[str]` | `[]` | More privileged operators (fnmatch), e.g. 'airflow.providers.ssh.operators.ssh.SSHOperator'. |  |
| `AUTOAIR_POLICY__DAG_ID_PATTERN` | `policy.dag_id_pattern` | `str` | `""` | Regex every dag_id must match (fullmatch), e.g. '[a-z][a-z0-9_]*'. Airflow's own rule ([A-Za-z0-9_.-]+, ≤ 250 chars) always applies too. |  |
| `AUTOAIR_POLICY__DAG_ID_PATTERNS_EXTRA` | `policy.dag_id_patterns_extra` | `list[str]` | `[]` | More regexes every dag_id must match (fullmatch). |  |
| `AUTOAIR_POLICY__TASK_ID_PATTERN` | `policy.task_id_pattern` | `str` | `""` | Regex every task_id must match (fullmatch). Airflow's own rule always applies too. |  |
| `AUTOAIR_POLICY__TASK_ID_PATTERNS_EXTRA` | `policy.task_id_patterns_extra` | `list[str]` | `[]` | More regexes every task_id must match (fullmatch). |  |
| `AUTOAIR_POLICY__SECRET_PARAM_PATTERN` | `policy.secret_param_pattern` | `str` | `(password\|passwd\|secret\|token\|api[_-]?key\|access[_-]?key\|private[_-]?key\|credential)` | Regex (case-insensitive) over parameter names whose literal value is reported as a hard-coded secret. Overrides the built-in pattern; empty disables it. |  |
| `AUTOAIR_POLICY__SECRET_PARAM_PATTERNS_EXTRA` | `policy.secret_param_patterns_extra` | `list[str]` | `[]` | More secret parameter-name regexes (any match). |  |
| `AUTOAIR_POLICY__SECRET_PARAM_EXCLUDE_PATTERN` | `policy.secret_param_exclude_pattern` | `str` | `(_conn_id\|_id\|_name\|_path\|_file\|_field\|_url\|_ref)$\|^token_url$` | Regex (case-insensitive) over parameter names that are never secrets even when secret_param_pattern matches (token_url, *_conn_id…). |  |
| `AUTOAIR_POLICY__SECRET_PARAM_EXCLUDE_PATTERNS_EXTRA` | `policy.secret_param_exclude_patterns_extra` | `list[str]` | `[]` | More never-a-secret parameter-name regexes. |  |
| `AUTOAIR_POLICY__SECRET_ASSIGNMENT_PATTERN` | `policy.secret_assignment_pattern` | `str` | `(?i)(?<![\w.\[\]'"])[\w.\[\]'"]{0,120}?(?:password\|passwd\|secret\|token\|api_?key\|access_?key)[\w'"\]]{0,120}[ \t]*=[ \t]*["'][^"'{}\s]{6,}["']` | Regex over inline code / scripts finding secret-looking assignments. Overrides the built-in pattern; empty disables it. |  |
| `AUTOAIR_POLICY__SECRET_ASSIGNMENT_PATTERNS_EXTRA` | `policy.secret_assignment_patterns_extra` | `list[str]` | `[]` | More secret-assignment regexes (any match). |  |
| `AUTOAIR_POLICY__JINJA_NAMES_EXTRA` | `policy.jinja_names_extra` | `list[str]` | `[]` | Template variables known besides Airflow's context (user_defined_macros, plugin macros…): no 'unknown template variable' warning for them. |  |
| `AUTOAIR_POLICY__RULES` | `policy.rules` | `list[PolicyRule]` | `[]` | Declarative rules: a match (environments, teams, dag_ids, tags, schedule, operators, params) and a check (forbid, min_retries, require_owner, tags_include, dag_field, task_field, param…) with severity error or warning. See docs/policy.md. |  |
| `AUTOAIR_POLICY__OPA_URL` | `policy.opa_url` | `str \| None` | `None` | Open Policy Agent decision URL (e.g. http://opa:8181/v1/data/autoair/violations). Validation POSTs {input: {spec, context}} and reports the decision's violations. Unset: OPA is not asked. |  |
| `AUTOAIR_POLICY__OPA_TIMEOUT` | `policy.opa_timeout` | `float` | `2.0` | Seconds to wait for the OPA decision. |  |
| `AUTOAIR_POLICY__OPA_FAIL_MODE` | `policy.opa_fail_mode` | `'open' \| 'closed'` | `open` | When OPA cannot be asked: open reports a warning and lets the DAG pass, closed reports an error (blocks deploys). |  |
| `AUTOAIR_POLICY__OPA_TOKEN` | `policy.opa_token` | `secret str \| None` | `None` | Bearer token sent to OPA (OPA's authentication=token). | yes |
| `AUTOAIR_POLICY__OPA_HEADERS` | `policy.opa_headers` | `dict[str, str]` | `{}` | Extra HTTP headers sent to OPA. | yes |
| `AUTOAIR_POLICY__OPA_CACHE_SECONDS` | `policy.opa_cache_seconds` | `float` | `5.0` | Seconds an OPA decision is reused for an identical input (0: always ask). |  |

## `audit`

Audit sinks: file, db, log, otel (AUTOAIR_AUDIT__SINKS).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AUDIT__SINKS` | `audit.sinks` | `list['file' \| 'db' \| 'log' \| 'otel'] \| None` | `None` | Audit sinks: file (JSON lines at audit_url), db (the SQL database of database.backend), log (one structured line on the 'autoair.audit' logger), otel (an OpenTelemetry span per record; needs autoair[otel] and a tracer provider). Unset: the state store (db with a database, else file) plus log. The audit view of the UI reads db, else file. |  |

## `runner`

Test runs from the builder.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_RUNNER__DEFAULT_TASK_TIMEOUT` | `runner.default_task_timeout` | `float` | `120` | Seconds a task test may run when the request sets none. |  |
| `AUTOAIR_RUNNER__DEFAULT_DAG_TIMEOUT` | `runner.default_dag_timeout` | `float` | `300` | Seconds a DAG test may run when the request sets none. |  |
| `AUTOAIR_RUNNER__MAX_TIMEOUT` | `runner.max_timeout` | `float` | `600` | Largest timeout (s) a test-run request may ask for. |  |
| `AUTOAIR_RUNNER__MAX_LOG_CHARS` | `runner.max_log_chars` | `int` | `64000` | Characters of task log returned per test run. |  |
| `AUTOAIR_RUNNER__MAX_VALUE_CHARS` | `runner.max_value_chars` | `int` | `4000` | Characters of a returned value/XCom repr shown. |  |
| `AUTOAIR_RUNNER__ALLOW_DB_MODE` | `runner.allow_db_mode` | `bool` | `false` | Allow DAG test runs with mode=db (dag.test()): the run writes a DAG run to the metadata DB, so the DAG code gets the metadata DB connection and the Fernet key, i.e. every Airflow connection and variable. Even then only callers holding admin:config (besides run:test and code:execute) may use it, and mode=auto picks db only for them; for everyone else auto runs in memory. Read for every request. |  |
| `AUTOAIR_RUNNER__PROCESS_ISOLATION` | `runner.process_isolation` | `'auto' \| 'required' \| 'off'` | `auto` | Run each local test run as PID 1 of its own PID, user and mount namespaces with a fresh /proc, so its code (Python or a BashOperator command) cannot see other processes, the AutoAir or Airflow server that started it included (Linux unprivileged user namespaces); the namespace also hides Airflow's secret files and mounts the DAG folders read-only. auto (default, every mode): when the kernel allows it (Ubuntu 24.04+ restricts it by default through AppArmor); otherwise the run still executes with the audit hook (audit_hook), read-only DAG folders and a secret-free copy of airflow.cfg, its result carries a 'not fully isolated' notice and the server logs one warning. required: test runs fail without namespaces. off: never. For isolated test runs on hosts without namespaces, configure validator.url. Runs on the validator service use validator.run_process_isolation. |  |
| `AUTOAIR_RUNNER__AUDIT_HOOK` | `runner.audit_hook` | `bool` | `true` | A Python audit hook in each local test-run child denies reading other processes' /proc/<pid> entries (environment, command line, memory) and other test runs' folders. It does not restrict subprocesses, network or writes (task code needs them), and it does not see what a subprocess does: process_isolation does. |  |

## `editor`

Code editor services (completion, lint).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_EDITOR__MAX_SOURCE_CHARS` | `editor.max_source_chars` | `int` | `200000` | Largest code accepted by completion and lint. |  |
| `AUTOAIR_EDITOR__MAX_COMPLETIONS` | `editor.max_completions` | `int` | `100` | Completion items returned per request. |  |
| `AUTOAIR_EDITOR__MAX_DOC_CHARS` | `editor.max_doc_chars` | `int` | `4000` | Characters of a docstring shown in hover/completion. |  |
| `AUTOAIR_EDITOR__SHELLCHECK_TIMEOUT` | `editor.shellcheck_timeout` | `float` | `5` | Seconds per ShellCheck run (DAG validation checks the scripts that changed since the last validation in runs of up to 50 scripts). |  |
| `AUTOAIR_EDITOR__SHELLCHECK_EXCLUDE` | `editor.shellcheck_exclude` | `list[str]` | `["SC1091", "SC2034", "SC2154"]` | ShellCheck codes never reported (noise for Airflow commands: masked Jinja, sourced env files). Overrides the built-in list. |  |
| `AUTOAIR_EDITOR__SHELLCHECK_EXCLUDE_EXTRA` | `editor.shellcheck_exclude_extra` | `list[str]` | `[]` | More ShellCheck codes never reported. |  |
| `AUTOAIR_EDITOR__SHELLCHECK_IN_VALIDATION` | `editor.shellcheck_in_validation` | `bool` | `true` | Report ShellCheck findings of bash_command, @task.bash and .sh file assets as warnings in DAG validation (Problems tab). Needs ShellCheck. |  |
| `AUTOAIR_EDITOR__PYTHON_LINT_SELECT` | `editor.python_lint_select` | `list[str]` | `["F", "E9", "B006", "B008", "B012", "B018", "PLE"]` | ruff rules checked in Python task code. Overrides the built-in list. |  |
| `AUTOAIR_EDITOR__PYTHON_LINT_SELECT_EXTRA` | `editor.python_lint_select_extra` | `list[str]` | `[]` | More ruff rules checked (e.g. 'S' for bandit). |  |
| `AUTOAIR_EDITOR__PYTHON_LINT_IGNORE` | `editor.python_lint_ignore` | `list[str]` | `["F401"]` | ruff rules never reported in task code. Overrides the built-in list. |  |
| `AUTOAIR_EDITOR__PYTHON_LINT_IGNORE_EXTRA` | `editor.python_lint_ignore_extra` | `list[str]` | `[]` | More ruff rules never reported. |  |
| `AUTOAIR_EDITOR__PYTHON_LINT_TIMEOUT` | `editor.python_lint_timeout` | `float` | `15` | Seconds per ruff lint run of task code (DAG validation lints all Python tasks that changed since the last validation in one run). |  |
| `AUTOAIR_EDITOR__VALIDATE_TIMEOUT` | `editor.validate_timeout` | `float` | `15` | Seconds each external checker (bash -n, gofmt, javac, tsc…) may run when a code field is validated (POST /api/lsp/validate). |  |
| `AUTOAIR_EDITOR__VALIDATE_COMMANDS` | `editor.validate_commands` | `dict[str, list[str]]` | `{}` | Checker commands overriding the PATH lookup, by tool name, e.g. {"tsc": ["/opt/node/bin/tsc"], "javac": ["/usr/lib/jvm/java-21/bin/javac"]}. |  |

## `lsp`

Language servers behind features.ide_language_server (AUTOAIR_LSP__*).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_LSP__PYTHON_SERVER` | `lsp.python_server` | `'basedpyright' \| 'pylsp'` | `basedpyright` | Python language server: basedpyright (pip install basedpyright; bundles Node) or pylsp (pip install 'autoair[lsp]': python-lsp-server + python-lsp-ruff). |  |
| `AUTOAIR_LSP__BASEDPYRIGHT_COMMAND` | `lsp.basedpyright_command` | `list[str]` | `["basedpyright-langserver", "--stdio"]` | Command starting basedpyright in stdio mode (first item looked up on PATH and next to the running Python). |  |
| `AUTOAIR_LSP__PYLSP_COMMAND` | `lsp.pylsp_command` | `list[str]` | `["pylsp"]` | Command starting python-lsp-server in stdio mode. |  |
| `AUTOAIR_LSP__BASH_COMMAND` | `lsp.bash_command` | `list[str]` | `["bash-language-server", "start"]` | Command starting bash-language-server (npm i -g bash-language-server); when missing, the editor keeps its built-in completions + ShellCheck. |  |
| `AUTOAIR_LSP__MAX_SESSIONS` | `lsp.max_sessions` | `int` | `16` | Language-server processes alive at once. |  |
| `AUTOAIR_LSP__MAX_SESSIONS_PER_USER` | `lsp.max_sessions_per_user` | `int` | `4` | Language-server processes per user. |  |
| `AUTOAIR_LSP__IDLE_TIMEOUT` | `lsp.idle_timeout` | `float` | `600` | Seconds without a message before a session is closed. |  |
| `AUTOAIR_LSP__START_TIMEOUT` | `lsp.start_timeout` | `float` | `30` | Seconds a language server may take to start. |  |
| `AUTOAIR_LSP__MEMORY_LIMIT_MB` | `lsp.memory_limit_mb` | `int \| None` | `2048` | Address-space limit (MB) of each language-server process (POSIX RLIMIT_AS; ignored where unsupported). Empty for no limit. |  |
| `AUTOAIR_LSP__CPU_LIMIT_SECONDS` | `lsp.cpu_limit_seconds` | `int \| None` | `3600` | CPU-time limit (s) of each language-server process (POSIX RLIMIT_CPU). Empty for no limit. |  |
| `AUTOAIR_LSP__MAX_MESSAGE_BYTES` | `lsp.max_message_bytes` | `int` | `8388608` | Largest JSON-RPC message accepted from the browser or the server. |  |

## `codegen`

Generated DAG files.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_CODEGEN__LINE_LENGTH` | `codegen.line_length` | `int` | `100` | ruff line length of generated DAG files. |  |
| `AUTOAIR_CODEGEN__FORMAT_TIMEOUT` | `codegen.format_timeout` | `float` | `30` | Seconds the ruff formatting pass may take. |  |

## `interchange`

DAG imports (Python, YAML, JSON).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_INTERCHANGE__MAX_SOURCE_CHARS` | `interchange.max_source_chars` | `int` | `2000000` | Largest DAG source accepted by an import. |  |
| `AUTOAIR_INTERCHANGE__YAML_MAX_NODES` | `interchange.yaml_max_nodes` | `int` | `200000` | Largest YAML document (values after alias expansion) accepted; guards against 'billion laughs' documents. |  |
| `AUTOAIR_INTERCHANGE__BULK_MAX_FILES` | `interchange.bulk_max_files` | `int` | `500` | Bulk import of a DAG repository: most Python files imported per request (more are reported as not scanned). |  |
| `AUTOAIR_INTERCHANGE__BULK_WORKERS` | `interchange.bulk_workers` | `int` | `4` | Bulk import: files imported concurrently (one isolated subprocess each). |  |
| `AUTOAIR_INTERCHANGE__BULK_EXCLUDE` | `interchange.bulk_exclude` | `list[str]` | `[".*", "*/.*", "tests/*", "*/tests/*", "test_*.py", "*/test_*.py", "*/__pycache__/*", "*/node_modules/*"]` | Bulk import: fnmatch patterns over repository-relative paths that are never imported (tests, hidden folders…). Files without both 'airflow' and 'dag' in their text are skipped as well (Airflow's safe mode heuristic). |  |
| `AUTOAIR_INTERCHANGE__BULK_GIT_ALLOWED_HOSTS` | `interchange.bulk_git_allowed_hosts` | `list[str]` | `["*"]` | Bulk import from git: fnmatch patterns of the hosts that may be cloned (https only), e.g. ['github.com', 'gitlab.acme.internal']. Empty: git sources are disabled. |  |
| `AUTOAIR_INTERCHANGE__BULK_GIT_TIMEOUT` | `interchange.bulk_git_timeout` | `float` | `120` | Bulk import: seconds a shallow git clone may take. |  |

## `preserve`

Editing existing Python DAG files: source policies and ejected-file protection (AUTOAIR_PRESERVE__<FIELD>).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_PRESERVE__ALLOWED_POLICIES` | `preserve.allowed_policies` | `list['generate' \| 'preserve' \| 'strict-preserve']` | `["generate", "preserve", "strict-preserve"]` | Source policies DAGs may use (JSON list in the environment). A DAG whose policy is not listed is refused at deploy; 'auto' resolves to the closest allowed one (preserve -> strict-preserve -> generate). Removing 'generate' means a hand-written file is never regenerated from its spec. |  |
| `AUTOAIR_PRESERVE__EJECTED_FILES` | `preserve.ejected_files` | `'block' \| 'warn' \| 'overwrite'` | `block` | A deploy that would regenerate a DAG file changed outside AutoAir (ejected: its fingerprint or recorded hash no longer matches, or a hand-written file without AutoAir's marker): block = stop and offer to apply the change to the file with preserve; warn = apply it with preserve automatically and report a warning (stop when that is not possible); overwrite = stop and offer preserve or an overwrite, which needs an explicit confirmation, the 'deploy:overwrite_ejected' permission and preserve.allow_force_overwrite=true (without it overwrite acts like block). Files without the marker are never overwritten. |  |
| `AUTOAIR_PRESERVE__ALLOW_FORCE_OVERWRITE` | `preserve.allow_force_overwrite` | `bool` | `false` | Offer and accept the 'Overwrite (force)' choice for a DAG file AutoAir would otherwise not replace: the hand-written file it keeps for a DAG (source-preserving deploys, raw code takeovers), a file changed outside AutoAir (ejected; for Python files also preserve.ejected_files=overwrite) and forced deploys of the other output modes. The caller still needs 'deploy:overwrite_ejected' and an explicit confirmation. false (default): the choice is hidden and refused; such files are changed only with preserve or kept as raw code. |  |
| `AUTOAIR_PRESERVE__IMPORT_TIMEOUT` | `preserve.import_timeout` | `float` | `90` | Seconds each import of the round-trip check may take (the existing file, the generated and the patched file are imported in a sandboxed subprocess). |  |
| `AUTOAIR_PRESERVE__CACHE_TTL` | `preserve.cache_ttl` | `float` | `300` | Seconds a replica keeps the import of an unchanged file (keyed by its content hash) between a deploy preview and the deploy. 0: no cache. |  |
| `AUTOAIR_PRESERVE__MAX_DIFF_CHARS` | `preserve.max_diff_chars` | `int` | `200000` | Largest unified diff returned by the source preview (longer diffs are cut and marked truncated). |  |

## `icons`

Custom node icons.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_ICONS__MAX_UPLOAD_BYTES` | `icons.max_upload_bytes` | `int` | `524288` | Largest icon upload accepted. |  |
| `AUTOAIR_ICONS__MAX_SIDE` | `icons.max_side` | `int` | `128` | Stored icons are scaled to fit this (px). |  |
| `AUTOAIR_ICONS__MAX_PIXELS` | `icons.max_pixels` | `int` | `16777216` | Uploads with more pixels are refused (decompression-bomb guard). |  |
| `AUTOAIR_ICONS__RULES_EXTRA` | `icons.rules_extra` | `list[IconRule]` | `[]` | Icon rules evaluated before the built-in inference: [{match: regex over the operator class path or its distribution/provider, icon: 'builtin:<logo>', a glyph name such as 'agent', or 'custom:<uploaded>'}] (first match wins). |  |

## `sandbox`

Environment of processes running DAG code (secrets removed).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_SANDBOX__SECRET_ENV_PATTERN` | `sandbox.secret_env_pattern` | `str` | `TOKEN\|SECRET\|PASSWORD\|PASSWD\|API_KEY\|PRIVATE_KEY\|CREDENTIAL\|EXTRA_HEADERS` | Regex (case-insensitive) over AUTOAIR_* variable names removed from the environment of DAG-code processes. Overrides the built-in pattern. |  |
| `AUTOAIR_SANDBOX__SECRET_ENV_PATTERNS_EXTRA` | `sandbox.secret_env_patterns_extra` | `list[str]` | `[]` | More AUTOAIR_* name regexes to remove (any match). |  |
| `AUTOAIR_SANDBOX__SCRUB_ENV` | `sandbox.scrub_env` | `list[str]` | `["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"]` | Variables (any prefix) always removed from DAG-code processes. Overrides the built-in list. |  |
| `AUTOAIR_SANDBOX__SCRUB_ENV_EXTRA` | `sandbox.scrub_env_extra` | `list[str]` | `[]` | More variables removed, e.g. ['AWS_SECRET_ACCESS_KEY', 'VAULT_TOKEN']. |  |
| `AUTOAIR_SANDBOX__CREDENTIAL_ENV_PATTERN` | `sandbox.credential_env_pattern` | `str` | `^AIRFLOW_(CONN\|VAR)_\|^AIRFLOW__.*(SECRET\|FERNET\|SQL_ALCHEMY_CONN\|PASSWORD\|PASSWD\|TOKEN\|PRIVATE_KEY\|BROKER_URL\|RESULT_BACKEND\|_KWARGS$\|ENGINE_ARGS\|BASIC_AUTH\|_DSN$\|_CMD$)\|(^\|_)(SECRET\|PASSWORD\|PASSWD\|TOKEN\|API_KEY\|ACCESS_KEY\|PRIVATE_KEY\|CREDENTIALS?\|CONNECTION_STRING\|SAS_TOKEN\|ACCOUNT_KEY\|SESSION_TOKEN\|SECURITY_TOKEN\|DSN\|BASIC_AUTH\|WEBHOOK\|WEBHOOK_URL\|BROKER_URL)(_\|$)\|_(PASS\|PWD)$\|^AWS_(ACCESS_KEY_ID\|WEB_IDENTITY_TOKEN_FILE\|CONTAINER_AUTHORIZATION_TOKEN.*)$\|^AZURE_(CLIENT_SECRET\|CLIENT_CERTIFICATE_.*\|FEDERATED_TOKEN_FILE\|USERNAME\|STORAGE_KEY)$\|^(GOOGLE_APPLICATION_CREDENTIALS\|CLOUDSDK_AUTH_.*\|PGPASSWORD\|PGPASSFILE\|MYSQL_PWD\|DATABASE_URL)$` | Regex (case-insensitive) over every variable name (any prefix) removed from DAG-code processes: Airflow's secrets (AIRFLOW__DATABASE__SQL_ALCHEMY_CONN, AIRFLOW__CORE__FERNET_KEY, AIRFLOW__API_AUTH__JWT_SECRET, any *_CMD or *_SECRET indirection, AIRFLOW_CONN_*, AIRFLOW_VAR_*) and common cloud credentials (AWS_*, GOOGLE_APPLICATION_CREDENTIALS, AZURE_*). Non-secret Airflow configuration stays. Overrides the built-in pattern; empty disables this rule. |  |
| `AUTOAIR_SANDBOX__CREDENTIAL_ENV_PATTERNS_EXTRA` | `sandbox.credential_env_patterns_extra` | `list[str]` | `[]` | More regexes over any variable name to remove (any match). |  |
| `AUTOAIR_SANDBOX__ENV_PASSTHROUGH` | `sandbox.env_passthrough` | `list[str]` | `[]` | Variable names (fnmatch patterns, case-insensitive) kept for DAG-code processes although credential_env_pattern matches them, e.g. ['AIRFLOW_VAR_ENVIRONMENT', 'AIRFLOW_CONN_TEST_*'] for DAGs that read a variable at parse time or task tests that need a connection. AutoAir's own secrets and the validator key connection are removed regardless. |  |

## `server`

HTTP server and Airflow API client.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_SERVER__HOST` | `server.host` | `str` | `127.0.0.1` | `autoair serve` bind address. |  |
| `AUTOAIR_SERVER__PORT` | `server.port` | `int` | `8765` | `autoair serve` port. |  |
| `AUTOAIR_SERVER__GZIP_MIN_SIZE` | `server.gzip_min_size` | `int` | `2048` | Responses larger than this (bytes) are gzip-compressed. |  |
| `AUTOAIR_SERVER__GZIP_LEVEL` | `server.gzip_level` | `int` | `6` | gzip compression level. |  |
| `AUTOAIR_SERVER__OTEL_EXCLUDED_URLS` | `server.otel_excluded_urls` | `list[str]` | `["health", "ready", "metrics"]` | URL patterns OpenTelemetry does not trace. |  |
| `AUTOAIR_SERVER__AIRFLOW_FETCH_CONCURRENCY` | `server.airflow_fetch_concurrency` | `int` | `8` | Parallel requests to Airflow's API per fan-out call. |  |
| `AUTOAIR_SERVER__AIRFLOW_PAGE_SIZE` | `server.airflow_page_size` | `int` | `1000` | Entries requested per list call to Airflow (connections, pools, variables, task instances of a run…). |  |
| `AUTOAIR_SERVER__AIRFLOW_LIST_MAX_ENTRIES` | `server.airflow_list_max_entries` | `int` | `50000` | Most entries read from one paged Airflow list (connections, pools, variables, import errors): pages are followed until Airflow's total_entries is reached, an empty page arrives or this many entries were read (a warning is logged then). |  |
| `AUTOAIR_SERVER__PUBLIC_URL` | `server.public_url` | `str \| None` | `None` | Public base URL of the AutoAir UI as users reach it (e.g. https://autoair.example.com or https://airflow.example.com/autoair). Used for links back to a DAG in commits/pull requests and for the 'Sign in with Airflow' return URL. Unset: no links. |  |
| `AUTOAIR_SERVER__WORKERS` | `server.workers` | `int` | `1` | `autoair serve`: worker processes. More than one needs shared state (collab_secret, shared proposals/audit URLs) and disables in-memory caches sharing. |  |
| `AUTOAIR_SERVER__GRACEFUL_SHUTDOWN_TIMEOUT` | `server.graceful_shutdown_timeout` | `float` | `30` | `autoair serve`: seconds in-flight requests get to finish on shutdown. |  |
| `AUTOAIR_SERVER__KEEP_ALIVE_TIMEOUT` | `server.keep_alive_timeout` | `float` | `5` | `autoair serve`: idle seconds before a keep-alive closes. |  |
| `AUTOAIR_SERVER__LIMIT_CONCURRENCY` | `server.limit_concurrency` | `int \| None` | `None` | `autoair serve`: most concurrent connections before answering 503. |  |
| `AUTOAIR_SERVER__ACCESS_LOG` | `server.access_log` | `bool` | `true` | `autoair serve`: log every request. |  |
| `AUTOAIR_SERVER__HARDEN_PROCESS` | `server.harden_process` | `bool` | `true` | Make AutoAir's own server processes (`autoair serve`, `autoair local`, their uvicorn workers, `mcp-serve --http`, `ai-serve`) non-dumpable at start-up (Linux prctl PR_SET_DUMPABLE=0), so child processes running user DAG code as the same user cannot read the server's environment or memory through /proc. Turn it off only for debugging with py-spy, gdb or core dumps. Never applied inside the Airflow plugin. |  |
| `AUTOAIR_SERVER__SSL_CERTFILE` | `server.ssl_certfile` | `path \| None` | `None` | `autoair serve`: TLS certificate chain (PEM); enables HTTPS. |  |
| `AUTOAIR_SERVER__SSL_KEYFILE` | `server.ssl_keyfile` | `path \| None` | `None` | `autoair serve`: TLS private key (PEM). |  |
| `AUTOAIR_SERVER__SSL_KEYFILE_PASSWORD` | `server.ssl_keyfile_password` | `secret str \| None` | `None` | `autoair serve`: password of an encrypted ssl_keyfile. | yes |
| `AUTOAIR_SERVER__SSL_CA_CERTS` | `server.ssl_ca_certs` | `path \| None` | `None` | `autoair serve`: CA bundle client certificates are verified against (mutual TLS, with ssl_cert_reqs). |  |
| `AUTOAIR_SERVER__SSL_CERT_REQS` | `server.ssl_cert_reqs` | `'none' \| 'optional' \| 'required'` | `none` | `autoair serve`: client certificates (mutual TLS): none, optional, required (needs ssl_ca_certs). |  |
| `AUTOAIR_SERVER__SSL_CIPHERS` | `server.ssl_ciphers` | `str \| None` | `None` | `autoair serve`: OpenSSL cipher string (default: Python's secure set). |  |
| `AUTOAIR_SERVER__SSL_MIN_VERSION` | `server.ssl_min_version` | `'TLSv1.2' \| 'TLSv1.3'` | `TLSv1.2` | `autoair serve`: lowest TLS version accepted. |  |
| `AUTOAIR_SERVER__PROXY_HEADERS` | `server.proxy_headers` | `bool` | `true` | `autoair serve`: trust X-Forwarded-Proto/For from forwarded_allow_ips (scheme and client address behind a reverse proxy). | yes |
| `AUTOAIR_SERVER__FORWARDED_ALLOW_IPS` | `server.forwarded_allow_ips` | `list[str]` | `["127.0.0.1"]` | `autoair serve`: proxy addresses whose X-Forwarded-* headers are trusted ('*' trusts every peer: only behind a proxy that strips them). |  |
| `AUTOAIR_SERVER__SECURITY_HEADERS` | `server.security_headers` | `bool` | `true` | Standalone: send Content-Security-Policy, X-Content-Type-Options, Referrer-Policy, X-Frame-Options/frame-ancestors (and HSTS when hsts_seconds > 0). | yes |
| `AUTOAIR_SERVER__CONTENT_SECURITY_POLICY` | `server.content_security_policy` | `str \| None` | `None` | Standalone: the whole Content-Security-Policy header, replacing the built-in policy for the single-page app (frame_ancestors is then ignored). |  |
| `AUTOAIR_SERVER__FRAME_ANCESTORS` | `server.frame_ancestors` | `list[str]` | `["'self'"]` | Standalone: who may embed the UI in a frame (CSP frame-ancestors), e.g. ["'self'", "https://portal.example.com"]; ["'none'"] forbids embedding. |  |
| `AUTOAIR_SERVER__REFERRER_POLICY` | `server.referrer_policy` | `str` | `strict-origin-when-cross-origin` | Standalone: Referrer-Policy header (empty: not sent). |  |
| `AUTOAIR_SERVER__HSTS_SECONDS` | `server.hsts_seconds` | `int` | `0` | Standalone: Strict-Transport-Security max-age sent on HTTPS responses (0: no HSTS header). Only enable when every client reaches AutoAir over HTTPS. |  |
| `AUTOAIR_SERVER__HSTS_INCLUDE_SUBDOMAINS` | `server.hsts_include_subdomains` | `bool` | `false` | Standalone: HSTS includeSubDomains. |  |
| `AUTOAIR_SERVER__HSTS_PRELOAD` | `server.hsts_preload` | `bool` | `false` | Standalone: HSTS preload. |  |
| `AUTOAIR_SERVER__COOKIE_SECURE` | `server.cookie_secure` | `'auto' \| 'always' \| 'never'` | `auto` | Secure flag of cookies AutoAir sets (sign-in state, refreshed Airflow session): auto = when the request came over HTTPS. |  |
| `AUTOAIR_SERVER__COOKIE_SAMESITE` | `server.cookie_samesite` | `'lax' \| 'strict'` | `lax` | SameSite attribute of cookies AutoAir sets. |  |
| `AUTOAIR_SERVER__MAX_BODY_BYTES` | `server.max_body_bytes` | `int` | `16777216` | Largest request body accepted (413 above it); per-feature limits such as interchange.max_source_chars still apply. |  |
| `AUTOAIR_SERVER__STATIC_MAX_AGE` | `server.static_max_age` | `int` | `31536000` | Cache-Control max-age (s) of content-hashed UI assets (immutable); entry files are always revalidated. |  |
| `AUTOAIR_SERVER__METRICS_ENABLED` | `server.metrics_enabled` | `bool` | `true` | Serve Prometheus metrics at /api/metrics. |  |
| `AUTOAIR_SERVER__READY_CHECK_STORAGE` | `server.ready_check_storage` | `bool` | `true` | /api/ready also checks the deploy target is reachable. |  |
| `AUTOAIR_SERVER__READY_CHECK_AIRFLOW` | `server.ready_check_airflow` | `bool` | `false` | /api/ready also checks Airflow's /api/v2/monitor/health answers. |  |
| `AUTOAIR_SERVER__READY_TIMEOUT` | `server.ready_timeout` | `float` | `5` | Seconds each /api/ready check may take. |  |

## `plugin`

Airflow UI integration (plugin mode).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_PLUGIN__NAV_NAME` | `plugin.nav_name` | `str` | `DAG Builder` | Name of the navigation entry. |  |
| `AUTOAIR_PLUGIN__NAV_CATEGORY` | `plugin.nav_category` | `str` | `browse` | Airflow navigation menu holding the entry (browse, admin, docs, user…). |  |
| `AUTOAIR_PLUGIN__DAG_TAB_NAME` | `plugin.dag_tab_name` | `str` | `AutoAir` | Name of the tab on every DAG page. |  |
| `AUTOAIR_PLUGIN__DAG_TAB` | `plugin.dag_tab` | `bool` | `true` | Show the tab on DAG pages. |  |
| `AUTOAIR_PLUGIN__DAG_TAB_TAGS` | `plugin.dag_tab_tags` | `list[str]` | `[]` | Airflow >= 3.4: show the DAG tab only on DAGs carrying at least one of these tags (the plugin's applies_to scoping, evaluated by Airflow's UI). Empty: the tab is on every DAG page, so a hand-written DAG can be imported from it. Ignored before Airflow 3.4, which cannot scope plugin tabs. JSON list in environment variables, e.g. '["autoair"]'. |  |

## `outbound`

TLS (CA bundle, client certificates, verification) and proxies of every outgoing connection (AUTOAIR_OUTBOUND__<FIELD>).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_OUTBOUND__VERIFY` | `outbound.verify` | `bool` | `true` | Verify TLS certificates. false is for throw-away test setups only: AutoAir logs a warning for every client built without verification. |  |
| `AUTOAIR_OUTBOUND__CA_BUNDLE` | `outbound.ca_bundle` | `path \| None` | `None` | CA bundle (PEM file, or a folder of hashed certificates) used instead of the default trust store, like REQUESTS_CA_BUNDLE (e.g. a corporate root CA); unset: certifi's store (SSL_CERT_FILE is honoured when trust_env is true). |  |
| `AUTOAIR_OUTBOUND__CLIENT_CERT` | `outbound.client_cert` | `path \| None` | `None` | Client certificate (PEM) for mutual TLS with servers. |  |
| `AUTOAIR_OUTBOUND__CLIENT_KEY` | `outbound.client_key` | `path \| None` | `None` | Private key of client_cert (PEM; default: inside client_cert). |  |
| `AUTOAIR_OUTBOUND__CLIENT_KEY_PASSWORD` | `outbound.client_key_password` | `secret str \| None` | `None` | Password of an encrypted client_key. | yes |
| `AUTOAIR_OUTBOUND__HTTP_PROXY` | `outbound.http_proxy` | `str \| None` | `None` | Proxy for http:// URLs (e.g. http://proxy:3128). |  |
| `AUTOAIR_OUTBOUND__HTTPS_PROXY` | `outbound.https_proxy` | `str \| None` | `None` | Proxy for https:// URLs (e.g. http://proxy:3128). |  |
| `AUTOAIR_OUTBOUND__NO_PROXY` | `outbound.no_proxy` | `list[str]` | `[]` | Hosts reached without the proxy: names (sub-domains match), IPs, 'host:port'; e.g. ['localhost', '127.0.0.1', '.internal']. |  |
| `AUTOAIR_OUTBOUND__TRUST_ENV` | `outbound.trust_env` | `bool` | `true` | Also honour the standard environment variables (HTTP(S)_PROXY, NO_PROXY, SSL_CERT_FILE…) when the settings above leave them unset. |  |
| `AUTOAIR_OUTBOUND__CLIENTS` | `outbound.clients` | `dict['airflow' \| 'git' \| 'marketplace' \| 'ai' \| 'research' \| 'login' \| 'mcp' \| 'opa' \| 'kb' \| 'validator', OutboundOverride]` | `{}` | Per-client overrides, e.g. `{'git': {'ca_bundle': '/etc/ssl/gitlab.pem'}, 'ai': {'proxy': 'http://egress:3128'}}`. Clients: airflow, git, marketplace, ai, research, login, mcp (external MCP servers of the agents), opa (policy.opa_url), kb (documentation fetches and the Elasticsearch store of the knowledge base), validator (validator.url). |  |

## `logging`

Log level and format (standalone).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_LOGGING__LEVEL` | `logging.level` | `'DEBUG' \| 'INFO' \| 'WARNING' \| 'ERROR' \| 'CRITICAL'` | `INFO` | Root log level. |  |
| `AUTOAIR_LOGGING__FORMAT` | `logging.format` | `'text' \| 'json'` | `text` | text: human readable lines; json: one JSON object per line (timestamp, level, logger, message, exception) for log shippers. |  |
| `AUTOAIR_LOGGING__LEVELS` | `logging.levels` | `dict[str, str]` | `{}` | Per-logger levels, e.g. {'autoair.storage': 'DEBUG', 'httpx2': 'WARNING'}. |  |

## `limits`

Rate limits of expensive endpoints, per user and role.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_LIMITS__RATE_LIMITS_ENABLED` | `limits.rate_limits_enabled` | `bool` | `true` | Enforce rate_limits (429 with Retry-After above them). |  |
| `AUTOAIR_LIMITS__WINDOW_SECONDS` | `limits.window_seconds` | `float` | `60` | Length of the rate-limit window (s). |  |
| `AUTOAIR_LIMITS__RATE_LIMITS` | `limits.rate_limits` | `dict['validate' \| 'import' \| 'ai' \| 'test_runs' \| 'deploy' \| 'catalog_refresh', int]` | `{"validate": 240, "import": 60, "ai": 60, "test_runs": 30, "deploy": 60, "catalog_refresh": 6}` | Requests per user and window per endpoint group (0: unlimited): validate (validation, code preview lint), import (Python/YAML/Airflow imports), ai (every AI call), test_runs (task/DAG tests), deploy (deploys, deletes, promotions), catalog_refresh. |  |
| `AUTOAIR_LIMITS__ROLE_RATE_LIMITS` | `limits.role_rate_limits` | `dict[str, dict['validate' \| 'import' \| 'ai' \| 'test_runs' \| 'deploy' \| 'catalog_refresh', int]]` | `{}` | Per AutoAir role overrides, e.g. {'Admin': {'ai': 0}, 'Viewer': {'validate': 30}}; a user gets the most generous limit of their roles (0 wins). |  |
| `AUTOAIR_LIMITS__MAX_TRACKED_USERS` | `limits.max_tracked_users` | `int` | `10000` | Users tracked by the limiter (least recent dropped). |  |

## `ui`

UI branding (title, logo, accent, banner) and defaults (language, time zone).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_UI__TITLE` | `ui.title` | `str` | `AutoAir` | Product name in the UI. |  |
| `AUTOAIR_UI__LOGO_URL` | `ui.logo_url` | `str \| None` | `None` | Logo shown in the UI (https URL, or a path served by your proxy). |  |
| `AUTOAIR_UI__ACCENT_COLOR` | `ui.accent_color` | `str \| None` | `None` | Accent colour of the banner/brand strip: a CSS hex colour or a Chakra palette name (blue, teal, red…). |  |
| `AUTOAIR_UI__BANNER_TEXT` | `ui.banner_text` | `str \| None` | `None` | Banner shown above the builder, e.g. 'PRODUCTION' or a maintenance notice (empty: none). |  |
| `AUTOAIR_UI__BANNER_COLOR` | `ui.banner_color` | `str` | `orange` | Banner colour: a Chakra palette name (red, orange, green…) or a hex colour. |  |
| `AUTOAIR_UI__DEFAULT_LANGUAGE` | `ui.default_language` | `'en' \| 'tr' \| None` | `None` | UI language for users who never chose one (default: the browser's). |  |
| `AUTOAIR_UI__DEFAULT_TIMEZONE` | `ui.default_timezone` | `str \| None` | `None` | IANA time zone used to show times (e.g. 'Europe/Istanbul', 'UTC'); default: the browser's. |  |
| `AUTOAIR_UI__DATE_LOCALE` | `ui.date_locale` | `str \| None` | `None` | BCP 47 locale for dates and numbers (e.g. 'en-GB'); default: the UI language. |  |

## `sso`

Sign-in with the Airflow UI session (docs/sso.md).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_SSO__ENABLED` | `sso.enabled` | `bool` | `true` | Standalone: accept the Airflow UI session cookie (when the browser sends it: same host, or a shared parent domain set up at the proxy) and offer 'Sign in with Airflow'. Plugin mode always uses Airflow's session. |  |
| `AUTOAIR_SSO__COOKIE_NAME` | `sso.cookie_name` | `str` | `_token` | Name of Airflow's session (JWT) cookie. |  |
| `AUTOAIR_SSO__AIRFLOW_PUBLIC_URL` | `sso.airflow_public_url` | `str \| None` | `None` | Airflow UI URL as browsers reach it (default: airflow_url). 'Sign in with Airflow' opens <airflow_public_url>/auth/login. |  |
| `AUTOAIR_SSO__REQUIRE_CSRF_HEADER` | `sso.require_csrf_header` | `bool` | `true` | Standalone with SSO, and plugin mode (Airflow's _token cookie): state-changing requests authenticated by the cookie (not a bearer token) must carry the X-AutoAir-CSRF header and a same-origin (or allowed CORS) Origin — the cookie is SameSite=Lax, which still lets sibling sites on the same registrable domain send it. |  |
| `AUTOAIR_SSO__REFRESH_MARGIN_SECONDS` | `sso.refresh_margin_seconds` | `int` | `300` | The UI refreshes the session this many seconds before the token expires (cookie sessions: through Airflow's refresh middleware). |  |
| `AUTOAIR_SSO__SESSION_CHECK_SECONDS` | `sso.session_check_seconds` | `int` | `60` | The UI re-checks the session this often (s) so a sign-out in Airflow signs AutoAir out too (0: only on API errors). |  |
| `AUTOAIR_SSO__PROPAGATE_LOGOUT` | `sso.propagate_logout` | `bool` | `true` | Signing out of AutoAir also revokes the Airflow token (Airflow >= 3.2) and clears the shared session cookie (signs the Airflow UI out when it shares it). |  |

## `admin`

Admin panel export and scan limits.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_ADMIN__EXPORT_LIMIT` | `admin.export_limit` | `int` | `50000` | Most audit records one CSV export may return (larger `limit` requests are refused). |  |
| `AUTOAIR_ADMIN__EXPORT_DEFAULT` | `admin.export_default` | `int` | `10000` | Audit records a CSV export returns without an explicit `limit` (capped at export_limit). |  |
| `AUTOAIR_ADMIN__SCAN_LIMIT` | `admin.scan_limit` | `int` | `5000` | Most recent audit records scanned to list the users seen. |  |
| `AUTOAIR_ADMIN__USAGE_SCAN_LIMIT` | `admin.usage_scan_limit` | `int` | `20000` | AI audit records aggregated for the AI usage view on the file backend (SQL backends aggregate in the database). |  |

## `local`

`autoair local` (no Airflow server).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_LOCAL__TOKEN` | `local.token` | `secret str \| None` | `None` | Access token for `autoair local` (Bearer header or pasted in the UI). Unset: `autoair local` creates a random token per run and opens the UI with it (`--no-token` serves without authentication on loopback, with iam.anonymous_roles). | yes |
| `AUTOAIR_LOCAL__ROLES` | `local.roles` | `list[str]` | `["Admin"]` | AutoAir roles of the `autoair local` token holder (the local user); never direct-push rights through storage.git.direct_push_roles. |  |
| `AUTOAIR_LOCAL__OPEN_BROWSER` | `local.open_browser` | `bool` | `false` | Open the UI in the default browser once started. |  |

## `validator`

Import checks in a separate validator service (autoair validator-serve) and the limits it applies to the processes importing user code (AUTOAIR_VALIDATOR__<FIELD>).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_VALIDATOR__URL` | `validator.url` | `str` | `""` | Base URL of the validator service, e.g. http://autoair-validator:8791. Empty: import checks run in this process's own warm DagBag pool (dagbag_pool*). |  |
| `AUTOAIR_VALIDATOR__SECRET_CONN_ID` | `validator.secret_conn_id` | `str` | `autoair_validator` | Airflow connection whose password (at least 16 bytes) is the HMAC key shared by the main server and the validator; it is removed from the environment of processes running DAG code on both sides. The main server reads it like any Airflow connection; the validator reads only the environment variable AIRFLOW_CONN_<CONN_ID> (JSON or URI form) and never passes it to its workers. |  |
| `AUTOAIR_VALIDATOR__KEY_CACHE_SECONDS` | `validator.key_cache_seconds` | `float` | `60` | Seconds the HMAC key read from the connection is reused before it is read again (rotation delay); 0 reads it for every check. |  |
| `AUTOAIR_VALIDATOR__REQUEST_OVERHEAD_SECONDS` | `validator.request_overhead_seconds` | `float` | `30` | Seconds added to the import check timeout for the HTTP request to the validator (queueing, worker start-up, transfer). |  |
| `AUTOAIR_VALIDATOR__RETRIES` | `validator.retries` | `int` | `2` | Extra attempts after a connection error or HTTP 429/502/503/504 from the validator. A request that timed out is not retried. |  |
| `AUTOAIR_VALIDATOR__RETRY_BACKOFF_SECONDS` | `validator.retry_backoff_seconds` | `float` | `0.5` | Wait before the first retry (s); doubled for every further retry. |  |
| `AUTOAIR_VALIDATOR__HEALTH_CHECK_INTERVAL_SECONDS` | `validator.health_check_interval_seconds` | `float` | `10` | After an outage (connection error, HTTP 401/403/502/504, invalid answer) the validator counts as down: checks fail at once and /ready is probed at most this often (s) until it answers again. A busy validator (429/503) or a timed-out check fails only that check. |  |
| `AUTOAIR_VALIDATOR__HEALTH_TIMEOUT_SECONDS` | `validator.health_timeout_seconds` | `float` | `3` | Timeout (s) of a /ready probe of the validator. |  |
| `AUTOAIR_VALIDATOR__FAIL_MODE` | `validator.fail_mode` | `'closed' \| 'open' \| 'local'` | `open` | When the validator cannot be asked: open (default) reports a warning and skips the import check, so deploys go ahead without it; closed reports an error (deploys are blocked until the validator answers); local runs the check in this process's warm DagBag pool instead (user code then runs here again). |  |
| `AUTOAIR_VALIDATOR__POOL_SIZE` | `validator.pool_size` | `int` | `2` | Validator: warm DagBag worker processes. |  |
| `AUTOAIR_VALIDATOR__MAX_TASKS_PER_CHILD` | `validator.max_tasks_per_child` | `int` | `1` | Validator: checks per worker before it is replaced. 1 (default) gives every check a fresh interpreter, so code of one check cannot observe or alter the next one; a spare worker is started in advance. |  |
| `AUTOAIR_VALIDATOR__START_TIMEOUT_SECONDS` | `validator.start_timeout_seconds` | `float` | `120` | Validator: seconds a worker may take to start (import Airflow). |  |
| `AUTOAIR_VALIDATOR__MAX_CHECK_SECONDS` | `validator.max_check_seconds` | `float` | `120` | Validator: largest import check timeout (s) a request may ask for. |  |
| `AUTOAIR_VALIDATOR__MAX_REQUEST_BYTES` | `validator.max_request_bytes` | `int` | `4194304` | Validator: largest request body (bytes, the JSON-encoded DAG source); larger requests get HTTP 413. |  |
| `AUTOAIR_VALIDATOR__MAX_QUEUE` | `validator.max_queue` | `int` | `8` | Validator: checks that may wait for a worker on top of pool_size; more concurrent requests get HTTP 503 (the main server retries). |  |
| `AUTOAIR_VALIDATOR__MEMORY_LIMIT_MB` | `validator.memory_limit_mb` | `int` | `1024` | Validator: address-space limit of a worker process (MiB, RLIMIT_AS); an import that allocates more fails with MemoryError. 0: no limit. |  |
| `AUTOAIR_VALIDATOR__CPU_LIMIT_SECONDS` | `validator.cpu_limit_seconds` | `int` | `120` | Validator: CPU seconds a worker process may use over its lifetime (RLIMIT_CPU, includes about 2 s for importing Airflow); the worker is then killed. 0: no limit. |  |
| `AUTOAIR_VALIDATOR__FILE_SIZE_LIMIT_MB` | `validator.file_size_limit_mb` | `int` | `16` | Validator: largest file a worker may write (MiB, RLIMIT_FSIZE). 0: no limit. |  |
| `AUTOAIR_VALIDATOR__OPEN_FILES_LIMIT` | `validator.open_files_limit` | `int` | `1024` | Validator: open file descriptors per worker (RLIMIT_NOFILE). 0: inherited. |  |
| `AUTOAIR_VALIDATOR__PROCESS_LIMIT` | `validator.process_limit` | `int` | `0` | Validator: RLIMIT_NPROC of a worker. It counts every process of the user, so set it only when the validator runs as its own user. 0: inherited. |  |
| `AUTOAIR_VALIDATOR__AUDIT_HOOK` | `validator.audit_hook` | `bool` | `true` | Validator: a Python audit hook in each worker denies subprocesses, fork/exec, signals, ctypes, network connections and DNS, writes outside the worker's scratch folder, and reads of other processes' /proc entries. Defence in depth: the container (no egress, read-only root, non-root) is the boundary. |  |
| `AUTOAIR_VALIDATOR__NETWORK_ISOLATION` | `validator.network_isolation` | `'auto' \| 'required' \| 'off'` | `auto` | Validator: move each worker into its own empty network namespace (Linux unprivileged user namespaces). auto: when the kernel allows it, required: workers fail to start otherwise, off: never. |  |
| `AUTOAIR_VALIDATOR__METRICS_ENABLED` | `validator.metrics_enabled` | `bool` | `true` | Validator: serve Prometheus metrics at /metrics. |  |
| `AUTOAIR_VALIDATOR__TEST_RUNS` | `validator.test_runs` | `bool` | `true` | Send task and DAG test runs to the validator (POST /v1/run on run_url, else url) when one is set, so their code runs in the validator's sandbox instead of this host. When the validator cannot be asked, fail_mode local runs the test here and both open and closed fail it (a test run never silently runs locally). mode=db runs always run here: the validator has no metadata DB. false: test runs always run here. |  |
| `AUTOAIR_VALIDATOR__RUN_URL` | `validator.run_url` | `str` | `""` | Base URL of the validator that executes test runs; empty: url. Point it at a second validator deployment whose NetworkPolicy allows the egress task tests need, so the import-check validator keeps no network at all (a NetworkPolicy applies to a whole pod and cannot tell run processes from check workers). |  |
| `AUTOAIR_VALIDATOR__RUNS_ENABLED` | `validator.runs_enabled` | `bool` | `true` | Validator: serve test runs (POST /v1/run). Set false on an import-check-only deployment (when the server's run_url points at a separate one): /v1/run then answers 404. |  |
| `AUTOAIR_VALIDATOR__RUN_NETWORK` | `validator.run_network` | `bool` | `true` | Validator: test-run processes may open network connections (task tests often call services). The pod then needs a NetworkPolicy egress rule; import checks keep no network either way. false: every run gets an empty network namespace (network_isolation decides whether that is required). |  |
| `AUTOAIR_VALIDATOR__RUN_PROCESS_ISOLATION` | `validator.run_process_isolation` | `'auto' \| 'required' \| 'off'` | `auto` | Validator: run each test run as PID 1 of its own PID, user and mount namespaces with a fresh /proc (see runner.process_isolation). auto: when the kernel or container allows it, required: runs fail otherwise, off: never. |  |
| `AUTOAIR_VALIDATOR__MAX_RUN_SECONDS` | `validator.max_run_seconds` | `float` | `600` | Validator: largest test-run timeout (s) a request may ask for. |  |
| `AUTOAIR_VALIDATOR__MAX_CONCURRENT_RUNS` | `validator.max_concurrent_runs` | `int` | `2` | Validator: test runs executing at the same time (one process each); more concurrent requests get HTTP 503 (the main server retries). |  |
| `AUTOAIR_VALIDATOR__RUN_MEMORY_LIMIT_MB` | `validator.run_memory_limit_mb` | `int` | `2048` | Validator: address-space limit of a test-run process (MiB, RLIMIT_AS); file_size_limit_mb, open_files_limit and process_limit apply to runs too, the CPU time is bounded by the run's timeout. 0: no limit. |  |

## `kb`

Documentation knowledge base: sources (online, git, folders, object storage, bundles), store (local, pgvector, Elasticsearch), refresh and retrieval (AUTOAIR_KB__<FIELD>).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_KB__ONLINE` | `kb.online` | `bool` | `false` | Let the official source fetch from airflow.apache.org and astronomer.io. Off (air-gapped): only the git, path, mirror and bundle sources you configure. |  |
| `AUTOAIR_KB__COMPONENTS` | `kb.components` | `list['core' \| 'task_sdk' \| 'providers' \| 'helm_chart' \| 'airflowctl' \| 'astronomer_airflow' \| 'astro_cli' \| 'astro' \| 'astro_runtime' \| 'autoair']` | `["core", "task_sdk", "providers", "helm_chart", "airflowctl", "astronomer_airflow", "autoair"]` | Components to index: core, task_sdk, providers, helm_chart, airflowctl (Apache), astronomer_airflow (Astronomer's Airflow guides), astro_cli, astro, astro_runtime (Astronomer products; off by default), autoair (AutoAir's own documentation, read by the autoair source). JSON list. |  |
| `AUTOAIR_KB__PROVIDERS` | `kb.providers` | `'installed' \| 'all'` | `installed` | Provider packages to index: installed (the providers installed here, at their installed versions: matches the code agents may write and keeps the index small) or all (every provider the docs list, latest stable docs for the ones not installed). |  |
| `AUTOAIR_KB__PROVIDER_INCLUDE` | `kb.provider_include` | `list[str]` | `["*"]` | Provider name patterns to index (fnmatch on e.g. amazon, common-sql). |  |
| `AUTOAIR_KB__PROVIDER_EXCLUDE` | `kb.provider_exclude` | `list[str]` | `[]` | Provider name patterns never indexed. |  |
| `AUTOAIR_KB__VERSIONS` | `kb.versions` | `list[str]` | `["installed"]` | Versions per component: installed (the installed package version; stable when not installed or not published), stable, or explicit versions (several are indexed side by side and can be filtered). JSON list. |  |
| `AUTOAIR_KB__SOURCES` | `kb.sources` | `list[KbSourceConfig]` | `[{"id": "official", "kind": "official", "enabled": true, "title": null, "url": null, "conn_id": null, "ref": null, "layout": "auto", "format": "auto", "components": null, "versions": null, "component": "custom", "public_base_url": null, "include": [], "exclude": [], "allow_private": false, "storage_options": {}}, {"id": "autoair", "kind": "autoair", "enabled": true, "title": "AutoAir documentation", "url": null, "conn_id": null, "ref": null, "layout": "auto", "format": "auto", "components": null, "versions": null, "component": "custom", "public_base_url": null, "include": [], "exclude": [], "allow_private": false, "storage_options": {}}]` | Documentation sources (JSON list), e.g. [{"id": "official", "kind": "official"}, {"id": "autoair", "kind": "autoair"}, {"id": "mirror", "kind": "git", "url": "https://git.corp/airflow.git", "conn_id": "kb_git", "ref": "3.1.0", "layout": "airflow_repo"}]. Default: official and autoair (AutoAir's packaged documentation); a list set here replaces the default, so keep the autoair entry to keep AutoAir's documentation searchable. |  |
| `AUTOAIR_KB__STORE` | `kb.store` | `'local' \| 'pgvector' \| 'elasticsearch'` | `local` | Where the index lives: local (SQLite with FTS5 BM25 and vectors, no dependencies), pgvector (PostgreSQL, autoair[kb-pgvector]) or elasticsearch (autoair[kb-elasticsearch]). |  |
| `AUTOAIR_KB__REFRESH_INTERVAL_HOURS` | `kb.refresh_interval_hours` | `float` | `24` | Incremental refresh interval of the server (hours); 0 disables scheduled refreshes (manual: admin panel or `autoair kb refresh`). |  |
| `AUTOAIR_KB__REFRESH_ON_START` | `kb.refresh_on_start` | `bool` | `true` | Build the index soon after start-up when it is empty and a source is usable. |  |
| `AUTOAIR_KB__LOCK_TTL_SECONDS` | `kb.lock_ttl_seconds` | `float` | `600` | Lease of the ingestion lock (renewed while a refresh runs); a replica that dies frees it after this long. |  |
| `AUTOAIR_KB__MAX_ERRORS` | `kb.max_errors` | `int` | `50` | Errors kept in the status of a refresh. |  |
| `AUTOAIR_KB__CACHE_DIR` | `kb.cache_dir` | `str \| None` | `None` | Folder of git checkouts and the embedding cache (default <cache_dir or state>/kb). |  |
| `AUTOAIR_KB__STATE_URL` | `kb.state_url` | `str \| None` | `None` | Where status, lock and admin overrides live when database.backend is file (fsspec URL; default <state>/kb-state). |  |

## `kb.fetch`

Fetch limits.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_KB__FETCH__ALLOWED_DOMAINS` | `kb.fetch.allowed_domains` | `list[str]` | `["airflow.apache.org", "astronomer.io"]` | Hosts online sources may fetch from (sub-domains included); a site or llms_txt source's own host is added for that source. Every hop is checked against this list and must resolve to a public address (SSRF protection). |  |
| `AUTOAIR_KB__FETCH__TIMEOUT_SECONDS` | `kb.fetch.timeout_seconds` | `float` | `30` | Timeout of one request (s). |  |
| `AUTOAIR_KB__FETCH__MAX_PAGE_BYTES` | `kb.fetch.max_page_bytes` | `int` | `8388608` | Largest response or file read (bytes); larger pages are skipped and reported. |  |
| `AUTOAIR_KB__FETCH__MAX_PAGES` | `kb.fetch.max_pages` | `int` | `25000` | Pages one refresh may fetch or read in total. |  |
| `AUTOAIR_KB__FETCH__MAX_PAGES_PER_PACKAGE` | `kb.fetch.max_pages_per_package` | `int` | `3000` | Pages per package version (one docs site package). |  |
| `AUTOAIR_KB__FETCH__MAX_RUN_SECONDS` | `kb.fetch.max_run_seconds` | `float` | `14400` | Wall-clock limit of one refresh (s); a refresh that runs out keeps what it indexed, removes nothing and reports a partial run. |  |
| `AUTOAIR_KB__FETCH__CONCURRENCY` | `kb.fetch.concurrency` | `int` | `4` | Parallel fetches. |  |
| `AUTOAIR_KB__FETCH__REQUESTS_PER_SECOND` | `kb.fetch.requests_per_second` | `float` | `4.0` | Requests per second over all hosts. |  |
| `AUTOAIR_KB__FETCH__MAX_REDIRECTS` | `kb.fetch.max_redirects` | `int` | `3` | Redirects followed. |  |
| `AUTOAIR_KB__FETCH__RETRIES` | `kb.fetch.retries` | `int` | `2` | Retries of a request that failed with 429/5xx. |  |
| `AUTOAIR_KB__FETCH__USER_AGENT` | `kb.fetch.user_agent` | `str` | `AutoAir-KB/1.0 (+https://github.com/ctolon/autoair)` | User-Agent of documentation fetches. |  |
| `AUTOAIR_KB__FETCH__INCLUDE_API_REFERENCE` | `kb.fetch.include_api_reference` | `bool` | `false` | Also index the generated API reference pages (_api/…) of Sphinx docs. Off by default: research already indexes the docstrings of the installed packages, and the API reference multiplies the size of the index. |  |
| `AUTOAIR_KB__FETCH__EXCLUDE_PAGES` | `kb.fetch.exclude_pages` | `list[str]` | `["genindex", "search", "py-modindex", "_modules/*"]` | Sphinx page names never indexed (fnmatch, JSON list). |  |
| `AUTOAIR_KB__FETCH__GIT_TIMEOUT_SECONDS` | `kb.fetch.git_timeout_seconds` | `float` | `900` | Timeout of one git command (clone/fetch) (s). |  |

## `kb.chunk`

Chunking.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_KB__CHUNK__SIZE` | `kb.chunk.size` | `int` | `1200` | Target characters per chunk. |  |
| `AUTOAIR_KB__CHUNK__OVERLAP` | `kb.chunk.overlap` | `int` | `150` | Characters shared by adjacent chunks. |  |
| `AUTOAIR_KB__CHUNK__MIN_CHARS` | `kb.chunk.min_chars` | `int` | `200` | Sections shorter than this are merged with the next section of the same page (0: never merge). |  |
| `AUTOAIR_KB__CHUNK__MAX_CHUNKS_PER_PAGE` | `kb.chunk.max_chunks_per_page` | `int` | `400` | Chunks kept per page (the rest is dropped). |  |

## `kb.search`

Retrieval.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_KB__SEARCH__TOP_K` | `kb.search.top_k` | `int` | `6` | Default number of results. |  |
| `AUTOAIR_KB__SEARCH__MAX_RESULTS` | `kb.search.max_results` | `int` | `20` | Upper bound of results any caller can ask for. |  |
| `AUTOAIR_KB__SEARCH__CANDIDATE_POOL` | `kb.search.candidate_pool` | `int` | `50` | Candidates per ranker before fusion/rerank. |  |
| `AUTOAIR_KB__SEARCH__RRF_K` | `kb.search.rrf_k` | `int` | `60` | Reciprocal Rank Fusion constant. |  |
| `AUTOAIR_KB__SEARCH__MAX_PER_PAGE` | `kb.search.max_per_page` | `int` | `2` | Results from the same page (diversity). |  |
| `AUTOAIR_KB__SEARCH__EMBEDDINGS` | `kb.search.embeddings` | `bool` | `true` | Use dense embeddings (ai.embedding, needs autoair[ai]) next to BM25. Without an embedder the knowledge base is keyword (BM25) only. |  |
| `AUTOAIR_KB__SEARCH__RERANK` | `kb.search.rerank` | `bool` | `true` | Rerank with ai.retrieval.reranker when one is configured. |  |
| `AUTOAIR_KB__SEARCH__RERANK_TOP_N` | `kb.search.rerank_top_n` | `int` | `20` | Candidates reranked. |  |
| `AUTOAIR_KB__SEARCH__SNIPPET_CHARS` | `kb.search.snippet_chars` | `int` | `320` | Snippet length. |  |
| `AUTOAIR_KB__SEARCH__MAX_TEXT_CHARS` | `kb.search.max_text_chars` | `int` | `1500` | Chunk text returned per result. |  |
| `AUTOAIR_KB__SEARCH__CONTEXT_MAX_CHARS` | `kb.search.context_max_chars` | `int` | `3000` | Documentation passages injected into one prompt (review, fix, writers). |  |

## `kb.local`

Local (SQLite) store.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_KB__LOCAL__PATH` | `kb.local.path` | `str \| None` | `None` | SQLite file of the local index (default <cache_dir or state>/kb/kb.sqlite3). |  |
| `AUTOAIR_KB__LOCAL__SHARED` | `kb.local.shared` | `bool` | `false` | The file is on storage every replica uses: one replica indexes for all. False: each host keeps (and refreshes) its own copy. |  |

## `kb.pgvector`

pgvector store.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_KB__PGVECTOR__URL` | `kb.pgvector.url` | `str \| None` | `None` | SQLAlchemy URL of the PostgreSQL database with the vector extension (postgresql+psycopg://host:5432/db), without credentials. Default: AutoAir's own database when database.backend is postgresql. |  |
| `AUTOAIR_KB__PGVECTOR__CONN_ID` | `kb.pgvector.conn_id` | `str \| None` | `None` | Airflow Postgres connection with host, login and password (overrides url). |  |
| `AUTOAIR_KB__PGVECTOR__TABLE_PREFIX` | `kb.pgvector.table_prefix` | `str` | `autoair_kb_` | Prefix of the knowledge-base tables. |  |
| `AUTOAIR_KB__PGVECTOR__SCHEMA_NAME` | `kb.pgvector.schema_name` | `str \| None` | `None` | Schema of the tables (default: the search path). |  |
| `AUTOAIR_KB__PGVECTOR__HNSW_INDEX` | `kb.pgvector.hnsw_index` | `bool` | `true` | Create an HNSW index for the embeddings (pgvector >= 0.5). |  |
| `AUTOAIR_KB__PGVECTOR__CREATE_EXTENSION` | `kb.pgvector.create_extension` | `bool` | `true` | Run CREATE EXTENSION IF NOT EXISTS vector (needs the privilege; create it once as a superuser otherwise and set false). |  |
| `AUTOAIR_KB__PGVECTOR__POOL_SIZE` | `kb.pgvector.pool_size` | `int` | `4` | Connections kept open. |  |
| `AUTOAIR_KB__PGVECTOR__SSL_MODE` | `kb.pgvector.ssl_mode` | `'disable' \| 'allow' \| 'prefer' \| 'require' \| 'verify-ca' \| 'verify-full' \| None` | `None` | PostgreSQL sslmode (driver default when unset). |  |
| `AUTOAIR_KB__PGVECTOR__SSL_ROOT_CERT` | `kb.pgvector.ssl_root_cert` | `str \| None` | `None` | CA bundle that verifies the server (sslrootcert). |  |

## `kb.elasticsearch`

Elasticsearch store.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_KB__ELASTICSEARCH__URL` | `kb.elasticsearch.url` | `str \| None` | `None` | Elasticsearch base URL (https://es.example:9200), without credentials. Requests go through AutoAir's HTTP stack (outbound.clients.kb for TLS/proxy). |  |
| `AUTOAIR_KB__ELASTICSEARCH__CONN_ID` | `kb.elasticsearch.conn_id` | `str \| None` | `None` | Airflow connection with login/password or extra.api_key (and host when url is unset). |  |
| `AUTOAIR_KB__ELASTICSEARCH__INDEX_PREFIX` | `kb.elasticsearch.index_prefix` | `str` | `autoair-kb` | Prefix of the knowledge-base indices. |  |
| `AUTOAIR_KB__ELASTICSEARCH__TIMEOUT_SECONDS` | `kb.elasticsearch.timeout_seconds` | `float` | `30` | Timeout of one request (s). |  |
| `AUTOAIR_KB__ELASTICSEARCH__NUM_CANDIDATES` | `kb.elasticsearch.num_candidates` | `int` | `200` | kNN num_candidates per shard. |  |
| `AUTOAIR_KB__ELASTICSEARCH__BULK_SIZE` | `kb.elasticsearch.bulk_size` | `int` | `500` | Documents per bulk request. |  |
| `AUTOAIR_KB__ELASTICSEARCH__SHARDS` | `kb.elasticsearch.shards` | `int` | `1` | Primary shards of new indices. |  |
| `AUTOAIR_KB__ELASTICSEARCH__REPLICAS` | `kb.elasticsearch.replicas` | `int` | `1` | Replicas of new indices. |  |

## `kb.bundle`

KB bundles (export/import).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_KB__BUNDLE__PUBLIC_KEYS` | `kb.bundle.public_keys` | `list[str]` | `[]` | Base64 Ed25519 public keys a bundle may be signed with (JSON list). When set, every imported bundle must carry a valid signature from one of them. |  |
| `AUTOAIR_KB__BUNDLE__REQUIRE_SIGNATURE` | `kb.bundle.require_signature` | `bool` | `false` | Refuse unsigned bundles even when no public key is configured (then every bundle is refused). Checksums are always verified. |  |
| `AUTOAIR_KB__BUNDLE__MAX_BYTES` | `kb.bundle.max_bytes` | `int` | `4294967296` | Largest bundle accepted (bytes); uploads from the admin panel are also bound by server.max_body_bytes. |  |
| `AUTOAIR_KB__BUNDLE__MAX_PAGES` | `kb.bundle.max_pages` | `int` | `200000` | Largest number of pages imported. |  |

## `ai`

AI assistance, agent, retrieval, research, guardrails, AI service and MCP (AUTOAIR_AI__<FIELD>).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__ENABLED` | `ai.enabled` | `bool` | `true` | Build the LLM client. False: LLM features answer 503 while the rules-only features (leak scan, review, optimise, search) keep working. To hide AI completely use features.ai=false. |  |
| `AUTOAIR_AI__PROVIDER` | `ai.provider` | `'anthropic' \| 'openai' \| 'azure' \| 'gemini' \| 'google-cloud' \| 'ollama' \| 'bedrock' \| 'groq' \| 'mistral' \| 'cohere' \| None` | `None` | LLM provider (a pydantic-ai provider; install its autoair[ai-*] extra). Default: inferred from the Airflow connection / the API key that is set (Anthropic first). |  |
| `AUTOAIR_AI__MODEL` | `ai.model` | `str \| None` | `None` | Model id, optionally as 'provider:model' like Airflow common.ai (default per provider, e.g. claude-sonnet-5-5). |  |
| `AUTOAIR_AI__MODELS` | `ai.models` | `list[str]` | `[]` | Models users may pick per request (besides model). |  |
| `AUTOAIR_AI__EFFORT` | `ai.effort` | `str \| None` | `None` | Reasoning effort (low/medium/high/xhigh/max) when supported. |  |
| `AUTOAIR_AI__API_KEY` | `ai.api_key` | `secret str \| None` | `None` | API key of the configured provider (any provider). | yes |
| `AUTOAIR_AI__ANTHROPIC_API_KEY` | `ai.anthropic_api_key` | `secret str \| None` | `None` | Anthropic API key (alternative to api_key). | yes |
| `AUTOAIR_AI__BASE_URL` | `ai.base_url` | `str \| None` | `None` | Provider endpoint: OpenAI-compatible base URL (vLLM, LM Studio), Azure OpenAI endpoint, Ollama URL, or an Anthropic/Gemini gateway. |  |
| `AUTOAIR_AI__API_VERSION` | `ai.api_version` | `str \| None` | `None` | Azure OpenAI api-version (provider openai: switches to Azure). |  |
| `AUTOAIR_AI__EXTRA_HEADERS` | `ai.extra_headers` | `dict[str, str]` | `{}` | Extra HTTP headers for LLM gateways. | yes |
| `AUTOAIR_AI__LLM_CONN_ID` | `ai.llm_conn_id` | `str \| None` | `None` | Airflow connection holding the model (common.ai 'pydanticai' format: extra.model 'provider:model', password = API key, host = base URL). |  |
| `AUTOAIR_AI__TIMEOUT_SECONDS` | `ai.timeout_seconds` | `float` | `180` | Timeout of one model call. |  |
| `AUTOAIR_AI__MAX_RETRIES` | `ai.max_retries` | `int` | `3` | Retries of a model call. |  |
| `AUTOAIR_AI__MAX_OUTPUT_TOKENS` | `ai.max_output_tokens` | `int` | `16000` | Upper bound of max_tokens for any call. |  |
| `AUTOAIR_AI__PRICING` | `ai.pricing` | `dict[str, dict[str, float]]` | `{}` | USD per million tokens per model, e.g. {"claude-sonnet-5-5": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}}; enables cost reporting. |  |
| `AUTOAIR_AI__LLM_MODE` | `ai.llm_mode` | `'central' \| 'per_user' \| 'both'` | `central` | central: everyone uses the model above; per_user: every user configures their own; both: a user's own model when set, else the central one. |  |
| `AUTOAIR_AI__MEMORY_URL` | `ai.memory_url` | `str \| None` | `None` | Folder of the AI memory (team/user conventions); default <state>/ai-memory. |  |
| `AUTOAIR_AI__SERVICE_URL` | `ai.service_url` | `str \| None` | `None` | Base URL of a separate AI service (autoair ai-serve); when set, /api/ai/* is proxied there instead of running in-process. |  |
| `AUTOAIR_AI__STREAM_HEARTBEAT_SECONDS` | `ai.stream_heartbeat_seconds` | `float` | `15` | SSE keep-alive comment interval. |  |

## `ai.embedding`

Embeddings for hybrid retrieval.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__EMBEDDING__PROVIDER` | `ai.embedding.provider` | `'openai' \| 'gemini' \| 'ollama' \| 'cohere' \| 'bedrock' \| None` | `None` | Embeddings provider for hybrid retrieval (pydantic-ai embeddings); default: keyword (BM25) only. |  |
| `AUTOAIR_AI__EMBEDDING__MODEL` | `ai.embedding.model` | `str \| None` | `None` | Embedding model id (provider default). |  |
| `AUTOAIR_AI__EMBEDDING__BASE_URL` | `ai.embedding.base_url` | `str \| None` | `None` | Embeddings endpoint (default: the chat provider's URL). |  |
| `AUTOAIR_AI__EMBEDDING__API_KEY` | `ai.embedding.api_key` | `secret str \| None` | `None` | API key for the embeddings provider (default: chat key). | yes |
| `AUTOAIR_AI__EMBEDDING__BATCH_SIZE` | `ai.embedding.batch_size` | `int` | `64` | Texts per embedding call. |  |
| `AUTOAIR_AI__EMBEDDING__MAX_CHARS` | `ai.embedding.max_chars` | `int` | `1500` | Characters of each chunk that are embedded. |  |

## `ai.agent`

Agent limits.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__AGENT__MAX_STEPS` | `ai.agent.max_steps` | `int` | `8` | Model turns per agent run. |  |
| `AUTOAIR_AI__AGENT__TOKEN_BUDGET` | `ai.agent.token_budget` | `int` | `250000` | Tokens (in + out) one agent run may use. |  |
| `AUTOAIR_AI__AGENT__COST_BUDGET_USD` | `ai.agent.cost_budget_usd` | `float \| None` | `None` | USD one agent run may spend (needs `pricing`); unlimited when unset. |  |
| `AUTOAIR_AI__AGENT__TIMEOUT_SECONDS` | `ai.agent.timeout_seconds` | `float` | `300` | Wall-clock limit of one agent/writer run. |  |
| `AUTOAIR_AI__AGENT__TOOL_TIMEOUT_SECONDS` | `ai.agent.tool_timeout_seconds` | `float` | `90` | Wall-clock limit of one tool call. |  |
| `AUTOAIR_AI__AGENT__MAX_TOOL_OUTPUT_CHARS` | `ai.agent.max_tool_output_chars` | `int` | `12000` | Tool output given back to the model (truncated). |  |
| `AUTOAIR_AI__AGENT__MAX_TOKENS_PER_CALL` | `ai.agent.max_tokens_per_call` | `int` | `8000` | max_tokens of each agent model call. |  |
| `AUTOAIR_AI__AGENT__FINAL_TURN_GRACE_SECONDS` | `ai.agent.final_turn_grace_seconds` | `float` | `30` | Wall-clock seconds reserved for the final (forced answer) turn of a run (at most a quarter of `timeout_seconds`); on timeout the run returns its completed steps with `stopped=timeout`. |  |

## `ai.retrieval`

Search, rerank and chunking.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__RETRIEVAL__TOP_K` | `ai.retrieval.top_k` | `int` | `8` | Default number of hits. |  |
| `AUTOAIR_AI__RETRIEVAL__CANDIDATE_POOL` | `ai.retrieval.candidate_pool` | `int` | `40` | Candidates per ranker before fusion/rerank. |  |
| `AUTOAIR_AI__RETRIEVAL__RRF_K` | `ai.retrieval.rrf_k` | `int` | `60` | Reciprocal Rank Fusion constant. |  |
| `AUTOAIR_AI__RETRIEVAL__BM25_K1` | `ai.retrieval.bm25_k1` | `float` | `1.4` | BM25 term-frequency saturation. |  |
| `AUTOAIR_AI__RETRIEVAL__BM25_B` | `ai.retrieval.bm25_b` | `float` | `0.7` | BM25 length normalisation. |  |
| `AUTOAIR_AI__RETRIEVAL__TITLE_BOOST` | `ai.retrieval.title_boost` | `int` | `3` | Title term weight in BM25. |  |
| `AUTOAIR_AI__RETRIEVAL__CHUNK_SIZE` | `ai.retrieval.chunk_size` | `int` | `1200` | Target characters per chunk (docs and code). |  |
| `AUTOAIR_AI__RETRIEVAL__CHUNK_OVERLAP` | `ai.retrieval.chunk_overlap` | `int` | `150` | Characters shared by chunks. |  |
| `AUTOAIR_AI__RETRIEVAL__INDEX_TTL_SECONDS` | `ai.retrieval.index_ttl_seconds` | `float` | `120` | Seconds before sources are re-read for changes. |  |
| `AUTOAIR_AI__RETRIEVAL__QUERY_CACHE_SIZE` | `ai.retrieval.query_cache_size` | `int` | `256` | Cached query results. |  |
| `AUTOAIR_AI__RETRIEVAL__EMBEDDING_CACHE_MAX_ENTRIES` | `ai.retrieval.embedding_cache_max_entries` | `int` | `100000` | Vectors kept in the on-disk embedding cache (oldest pruned; 0 = no cap). |  |
| `AUTOAIR_AI__RETRIEVAL__QUERY_CACHE_TTL_SECONDS` | `ai.retrieval.query_cache_ttl_seconds` | `float` | `300` | Seconds a cached query result is reused. |  |
| `AUTOAIR_AI__RETRIEVAL__QUERY_EXPANSION` | `ai.retrieval.query_expansion` | `bool` | `true` | Expand queries with Airflow synonyms/abbreviations. |  |
| `AUTOAIR_AI__RETRIEVAL__LLM_QUERY_REWRITE` | `ai.retrieval.llm_query_rewrite` | `bool` | `false` | Let the LLM rewrite research queries (extra call). |  |
| `AUTOAIR_AI__RETRIEVAL__SYNONYMS` | `ai.retrieval.synonyms` | `dict[str, list[str]]` | `{}` | Extra query-expansion vocabulary, e.g. {"dwh": ["snowflake", "warehouse"]} (merged over the built-in Airflow/cloud terms). |  |
| `AUTOAIR_AI__RETRIEVAL__RERANKER` | `ai.retrieval.reranker` | `'none' \| 'cross_encoder' \| 'llm' \| 'cohere' \| 'voyage' \| 'jina'` | `none` | Reranker of the fused candidates: cross_encoder (autoair[ai-rerank]), llm (the configured model), or a hosted rerank API. |  |
| `AUTOAIR_AI__RETRIEVAL__RERANK_MODEL` | `ai.retrieval.rerank_model` | `str \| None` | `None` | Reranker model id (default per reranker). |  |
| `AUTOAIR_AI__RETRIEVAL__RERANK_API_KEY` | `ai.retrieval.rerank_api_key` | `secret str \| None` | `None` | API key of the hosted rerank API. | yes |
| `AUTOAIR_AI__RETRIEVAL__RERANK_BASE_URL` | `ai.retrieval.rerank_base_url` | `str \| None` | `None` | Override the hosted rerank API endpoint. |  |
| `AUTOAIR_AI__RETRIEVAL__RERANK_TOP_N` | `ai.retrieval.rerank_top_n` | `int` | `20` | Candidates reranked. |  |
| `AUTOAIR_AI__RETRIEVAL__CONTEXT_MAX_CHARS` | `ai.retrieval.context_max_chars` | `int` | `4000` | Retrieved context injected into one prompt. |  |
| `AUTOAIR_AI__RETRIEVAL__MAX_SPECS` | `ai.retrieval.max_specs` | `int` | `300` | Saved DAGs indexed. |  |
| `AUTOAIR_AI__RETRIEVAL__MAX_TEMPLATES` | `ai.retrieval.max_templates` | `int` | `300` | Templates indexed. |  |

## `ai.research`

Agentic research (air-gapped/online).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__RESEARCH__ONLINE` | `ai.research.online` | `bool` | `false` | Allow fetching documentation from the internet (allow-listed domains). Off = air-gapped: only installed packages, AutoAir docs and the knowledge base. |  |
| `AUTOAIR_AI__RESEARCH__ALLOWED_DOMAINS` | `ai.research.allowed_domains` | `list[str]` | `["airflow.apache.org", "pypi.org", "raw.githubusercontent.com"]` | Domains online research may fetch (sub-domains included). |  |
| `AUTOAIR_AI__RESEARCH__PROXY` | `ai.research.proxy` | `str \| None` | `None` | HTTP(S) proxy for online research. |  |
| `AUTOAIR_AI__RESEARCH__TIMEOUT_SECONDS` | `ai.research.timeout_seconds` | `float` | `15` | Timeout of one fetch. |  |
| `AUTOAIR_AI__RESEARCH__MAX_BYTES` | `ai.research.max_bytes` | `int` | `2097152` | Largest response body read. |  |
| `AUTOAIR_AI__RESEARCH__MAX_REDIRECTS` | `ai.research.max_redirects` | `int` | `3` | Redirects followed. |  |
| `AUTOAIR_AI__RESEARCH__RATE_LIMIT_PER_MINUTE` | `ai.research.rate_limit_per_minute` | `int` | `30` | Fetches per minute per domain (token bucket). |  |
| `AUTOAIR_AI__RESEARCH__CACHE_TTL_SECONDS` | `ai.research.cache_ttl_seconds` | `float` | `86400` | Seconds a fetched page is reused. |  |
| `AUTOAIR_AI__RESEARCH__CACHE_DIR` | `ai.research.cache_dir` | `path \| None` | `None` | Fetch cache folder (default <state>/ai-research-cache). |  |
| `AUTOAIR_AI__RESEARCH__INDEX_INSTALLED` | `ai.research.index_installed` | `bool` | `true` | Index docstrings, provider metadata and examples of installed packages. |  |
| `AUTOAIR_AI__RESEARCH__PACKAGE_PATTERNS` | `ai.research.package_patterns` | `list[str]` | `["apache-airflow*", "airflow*"]` | Distributions whose modules are indexed (fnmatch, normalised names). |  |
| `AUTOAIR_AI__RESEARCH__EXTRA_SOURCE_DIRS` | `ai.research.extra_source_dirs` | `list[path]` | `[]` | More folders research may read (besides site-packages and AutoAir custom operator paths). |  |
| `AUTOAIR_AI__RESEARCH__MAX_SOURCE_CHARS` | `ai.research.max_source_chars` | `int` | `60000` | Characters returned by read_source. |  |
| `AUTOAIR_AI__RESEARCH__MAX_INDEX_MODULES` | `ai.research.max_index_modules` | `int` | `4000` | Modules parsed when indexing installed packages. |  |
| `AUTOAIR_AI__RESEARCH__USER_AGENT` | `ai.research.user_agent` | `str` | `AutoAir-Research/1.0 (+https://github.com/ctolon/autoair)` | User-Agent of online fetches. |  |

## `ai.guardrails`

Input/output guardrails.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__GUARDRAILS__REDACT_SECRETS` | `ai.guardrails.redact_secrets` | `bool` | `true` | Mask secret-looking strings before text leaves AutoAir. |  |
| `AUTOAIR_AI__GUARDRAILS__REDACT_PATTERNS` | `ai.guardrails.redact_patterns` | `list[str]` | `[]` | Extra regexes whose matches are redacted. |  |
| `AUTOAIR_AI__GUARDRAILS__PII_MASKING` | `ai.guardrails.pii_masking` | `bool` | `false` | Mask personal data (emails, phones, cards, IBANs, IPs). |  |
| `AUTOAIR_AI__GUARDRAILS__PII_ENTITIES` | `ai.guardrails.pii_entities` | `list['email' \| 'phone' \| 'credit_card' \| 'iban' \| 'ipv4' \| 'ssn']` | `["email", "phone", "credit_card", "iban", "ssn"]` | Kinds of personal data masked when pii_masking is on. |  |
| `AUTOAIR_AI__GUARDRAILS__PROMPT_INJECTION` | `ai.guardrails.prompt_injection` | `'off' \| 'flag' \| 'block'` | `flag` | Injection heuristics on retrieved content / tool outputs: 'flag' wraps and annotates, 'block' drops the suspicious text. |  |
| `AUTOAIR_AI__GUARDRAILS__INJECTION_PATTERNS_EXTRA` | `ai.guardrails.injection_patterns_extra` | `list[str]` | `[]` | More regexes (case-insensitive) that mark untrusted text as an injection attempt, in addition to the built-in phrases. |  |
| `AUTOAIR_AI__GUARDRAILS__MAX_INPUT_CHARS` | `ai.guardrails.max_input_chars` | `int` | `200000` | Largest user text accepted by AI endpoints. |  |
| `AUTOAIR_AI__GUARDRAILS__TOOL_ALLOWLIST` | `ai.guardrails.tool_allowlist` | `dict[str, list[str]]` | `{"viewer": ["search_*", "get_operator", "validate_spec", "lint_spec", "render_code", "get_saved_spec", "list_dag_runs", "list_task_instances", "run_statistics", "read_source", "list_installed_packages", "get_package_info", "fetch_doc", "get_current_spec", "propose_spec_change", "write_code", "list_conventions", "remember"], "editor": ["*"], "admin": ["*"]}` | Agent tools per role (fnmatch patterns): viewer / editor / admin. |  |
| `AUTOAIR_AI__GUARDRAILS__CHECK_GENERATED_CODE` | `ai.guardrails.check_generated_code` | `bool` | `true` | Generated code must pass AutoAir validation, ruff and ShellCheck (when installed) before it is suggested. |  |
| `AUTOAIR_AI__GUARDRAILS__BLOCK_SECRET_OUTPUT` | `ai.guardrails.block_secret_output` | `bool` | `true` | Redact secrets the model writes into suggestions. |  |
| `AUTOAIR_AI__GUARDRAILS__EXTERNAL` | `ai.guardrails.external` | `list[str]` | `[]` | Extra guardrails: 'llm_guard', 'nemo:<config dir>', 'guardrails_ai', or 'package.module:factory' returning a Guardrail (autoair[ai-guardrails]). |  |

## `ai.audit`

AI audit log.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__AUDIT__ENABLED` | `ai.audit.enabled` | `bool` | `true` | Record every AI request. |  |
| `AUTOAIR_AI__AUDIT__URL` | `ai.audit.url` | `str \| None` | `None` | Audit folder (fsspec URL); default <state>/ai-audit. |  |
| `AUTOAIR_AI__AUDIT__INCLUDE_PROMPTS` | `ai.audit.include_prompts` | `bool` | `false` | Also store (redacted) prompts - may contain sensitive data. |  |
| `AUTOAIR_AI__AUDIT__RETENTION_DAYS` | `ai.audit.retention_days` | `int` | `90` | Delete audit day folders older than this many days (0 = keep forever). |  |
| `AUTOAIR_AI__AUDIT__MAX_TOTAL_MB` | `ai.audit.max_total_mb` | `int` | `1024` | Delete the oldest audit days while the log is larger than this (0 = no size limit). |  |

## `ai.knowledge`

Knowledge base overrides.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__KNOWLEDGE__DISABLED` | `ai.knowledge.disabled` | `list[str]` | `[]` | Built-in document ids (file stems) to switch off. |  |
| `AUTOAIR_AI__KNOWLEDGE__EXTRA_DIRS` | `ai.knowledge.extra_dirs` | `list[str]` | `[]` | More Markdown folders (paths or fsspec URLs); a file with the same name as a built-in document replaces it. |  |
| `AUTOAIR_AI__KNOWLEDGE__STORE_URL` | `ai.knowledge.store_url` | `str \| None` | `None` | Where uploaded overrides live (fsspec URL); default <state>/ai-knowledge. |  |
| `AUTOAIR_AI__KNOWLEDGE__INCLUDE_REPO_DOCS` | `ai.knowledge.include_repo_docs` | `bool` | `true` | Also index AutoAir's docs/ folder when running from source. |  |
| `AUTOAIR_AI__KNOWLEDGE__MAX_UPLOAD_BYTES` | `ai.knowledge.max_upload_bytes` | `int` | `524288` | Largest uploaded knowledge document. |  |

## `ai.prompts`

Prompts.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__PROMPTS__OVERRIDE_DIR` | `ai.prompts.override_dir` | `path \| None` | `None` | Folder of prompt overrides: <NAME>.md replaces the built-in system prompt NAME (e.g. REVIEW_SYSTEM.md); see GET /api/ai/prompts. |  |
| `AUTOAIR_AI__PROMPTS__REPLACE` | `ai.prompts.replace` | `dict[str, str]` | `{}` | Replace a built-in system prompt: {NAME: text}, e.g. {"TROUBLESHOOT_SYSTEM": "..."} (an override_dir file still wins). ${variables} are substituted (docs/ai-assistants.md#prompt-variables). |  |
| `AUTOAIR_AI__PROMPTS__APPEND` | `ai.prompts.append` | `dict[str, str]` | `{}` | Text appended to a system prompt: {NAME: text}, e.g. {"DEVELOPER_ASSISTANT_SYSTEM": "Always answer in German."}; ${variables} are substituted. |  |
| `AUTOAIR_AI__PROMPTS__VARIABLES` | `ai.prompts.variables` | `dict[str, str]` | `{}` | Extra ${name} variables for prompts (besides the built-in ones such as ${organization}, ${user}, ${team}, ${airflow_version}, ${today}). |  |
| `AUTOAIR_AI__PROMPTS__ORGANIZATION` | `ai.prompts.organization` | `str \| None` | `None` | Organisation name the assistants refer to (${organization}). |  |

## `ai.writers`

Writers.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__WRITERS__MAX_REPAIRS` | `ai.writers.max_repairs` | `int` | `2` | Validate-and-repair rounds of the code writers. |  |
| `AUTOAIR_AI__WRITERS__LINT_TIMEOUT_SECONDS` | `ai.writers.lint_timeout_seconds` | `float` | `15` | Timeout of one ruff / ShellCheck run. |  |
| `AUTOAIR_AI__WRITERS__MAX_OPERATORS_IN_PROMPT` | `ai.writers.max_operators_in_prompt` | `int` | `25` | Catalog operators described to the writers. |  |
| `AUTOAIR_AI__WRITERS__MAX_PACKAGES_IN_PROMPT` | `ai.writers.max_packages_in_prompt` | `int` | `120` | Installed packages listed to the writers. |  |

## `ai.assist`

Assist feature limits.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__ASSIST__MAX_LOG_CHARS` | `ai.assist.max_log_chars` | `int` | `12000` | Tail of a failed task's log sent to fix. |  |
| `AUTOAIR_AI__ASSIST__MAX_CODE_CHARS` | `ai.assist.max_code_chars` | `int` | `40000` | Code sent to explain-code (truncated). |  |
| `AUTOAIR_AI__ASSIST__FINDINGS_MAX_TOKENS` | `ai.assist.findings_max_tokens` | `int` | `12000` | max_tokens of review / leak / optimise / fix calls. |  |
| `AUTOAIR_AI__ASSIST__EXPLAIN_MAX_TOKENS` | `ai.assist.explain_max_tokens` | `int` | `6000` | max_tokens of explain-DAG calls. |  |
| `AUTOAIR_AI__ASSIST__EXPLAIN_CODE_MAX_TOKENS` | `ai.assist.explain_code_max_tokens` | `int` | `3000` | max_tokens of explain-code calls. |  |
| `AUTOAIR_AI__ASSIST__TASK_CODE_MAX_TOKENS` | `ai.assist.task_code_max_tokens` | `int` | `8000` | max_tokens of task-code / writer calls. |  |
| `AUTOAIR_AI__ASSIST__FIX_ROUNDS` | `ai.assist.fix_rounds` | `int` | `2` | Patch-and-revalidate rounds of fix. |  |
| `AUTOAIR_AI__ASSIST__DEFAULT_TASK_TIMEOUT_SECONDS` | `ai.assist.default_task_timeout_seconds` | `int` | `3600` | execution_timeout the rules suggest for tasks. |  |
| `AUTOAIR_AI__ASSIST__DEFAULT_SENSOR_TIMEOUT_SECONDS` | `ai.assist.default_sensor_timeout_seconds` | `int` | `21600` | timeout the rules suggest for sensors. |  |
| `AUTOAIR_AI__ASSIST__MEMORY_MAX_ITEMS` | `ai.assist.memory_max_items` | `int` | `2000` | Conventions/preferences the AI memory keeps. |  |

## `ai.user_llm`

Per-user model restrictions.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__USER_LLM__ALLOWED_PROVIDERS` | `ai.user_llm.allowed_providers` | `list['anthropic' \| 'openai' \| 'azure' \| 'gemini' \| 'google-cloud' \| 'ollama' \| 'bedrock' \| 'groq' \| 'mistral' \| 'cohere']` | `["anthropic", "openai", "gemini", "ollama"]` | Providers users may choose. |  |
| `AUTOAIR_AI__USER_LLM__ALLOWED_MODELS` | `ai.user_llm.allowed_models` | `list[str]` | `["*"]` | Model ids users may choose (fnmatch). |  |
| `AUTOAIR_AI__USER_LLM__ALLOWED_BASE_URLS` | `ai.user_llm.allowed_base_urls` | `list[str]` | `[]` | Custom endpoints users may set (URL prefixes, e.g. https://llm.internal.example/v1); empty = provider defaults only. |  |
| `AUTOAIR_AI__USER_LLM__ALLOW_PRIVATE_NETWORKS` | `ai.user_llm.allow_private_networks` | `bool` | `false` | Allow user endpoints that resolve to private/loopback addresses (SSRF). |  |
| `AUTOAIR_AI__USER_LLM__ALLOWED_CONN_IDS` | `ai.user_llm.allowed_conn_ids` | `list[str]` | `[]` | Airflow connection ids users may reference instead of a key (fnmatch); empty = not allowed (the server would use the connection on their behalf). |  |
| `AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY` | `ai.user_llm.encryption_key` | `secret str \| None` | `None` | Fernet key encrypting stored user API keys (generate with `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`); default: a key file created in the state folder. | yes |
| `AUTOAIR_AI__USER_LLM__STORE_URL` | `ai.user_llm.store_url` | `str \| None` | `None` | Where user model settings live; default <state>/ai-users. |  |

## `ai.service`

Separate AI service / proxy.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__SERVICE__REPLAY_CACHE_MAX_ENTRIES` | `ai.service.replay_cache_max_entries` | `int` | `100000` | Maximum signed request nonces retained by SQL-backed AI service replay detection, divided among 256 hash buckets. A full bucket rejects new requests until entries expire; live entries are never evicted. |  |
| `AUTOAIR_AI__SERVICE__TIMEOUT_SECONDS` | `ai.service.timeout_seconds` | `float` | `600` | Proxy timeout (streams may run this long). |  |
| `AUTOAIR_AI__SERVICE__PROXY_MCP` | `ai.service.proxy_mcp` | `bool` | `false` | Also proxy /mcp to the AI service (instead of in-process). |  |
| `AUTOAIR_AI__SERVICE__SERVE_MCP` | `ai.service.serve_mcp` | `bool` | `true` | The AI service also serves /mcp. |  |
| `AUTOAIR_AI__SERVICE__SHARED_SECRET` | `ai.service.shared_secret` | `secret str \| None` | `None` | HMAC secret shared by the main server and the AI service: the proxy signs the caller identity, the service trusts signed requests. | yes |
| `AUTOAIR_AI__SERVICE__VERIFY_TOKENS` | `ai.service.verify_tokens` | `bool` | `true` | AI service: verify unsigned bearer tokens against Airflow. |  |

## `ai.mcp`

MCP server.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__MCP__ALLOW_WRITES` | `ai.mcp.allow_writes` | `bool` | `true` | Allow MCP clients to save/delete DAGs and trigger runs (always behind confirm=true); false makes the MCP server read-only. |  |

## `ai.assistants`

Chat assistants (Ask AI).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__ASSISTANTS__STORE_URL` | `ai.assistants.store_url` | `str \| None` | `None` | Where chat threads live (fsspec URL); default <state>/ai-threads (rows of the SQL database when database.backend is sqlite/postgresql). |  |
| `AUTOAIR_AI__ASSISTANTS__MAX_THREADS_PER_USER` | `ai.assistants.max_threads_per_user` | `int` | `200` | Threads kept per user and mode (oldest deleted). |  |
| `AUTOAIR_AI__ASSISTANTS__MAX_MESSAGES_PER_THREAD` | `ai.assistants.max_messages_per_thread` | `int` | `400` | Messages kept per thread (oldest dropped). |  |
| `AUTOAIR_AI__ASSISTANTS__COMPACTION_TRIGGER_CHARS` | `ai.assistants.compaction_trigger_chars` | `int` | `40000` | Compact a thread (summarise older turns) once its history sent to the model exceeds this many characters. |  |
| `AUTOAIR_AI__ASSISTANTS__COMPACTION_KEEP_MESSAGES` | `ai.assistants.compaction_keep_messages` | `int` | `6` | Recent messages always sent verbatim. |  |
| `AUTOAIR_AI__ASSISTANTS__SUMMARY_MAX_TOKENS` | `ai.assistants.summary_max_tokens` | `int` | `1500` | max_tokens of the compaction summary call. |  |
| `AUTOAIR_AI__ASSISTANTS__MAX_FOLLOW_UPS` | `ai.assistants.max_follow_ups` | `int` | `3` | Suggested follow-up questions per answer. |  |

## `ai.assistants.developer`

Developer assistant.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__ASSISTANTS__DEVELOPER__ENABLED` | `ai.assistants.developer.enabled` | `bool` | `true` | Offer this assistant in the chat panel. |  |
| `AUTOAIR_AI__ASSISTANTS__DEVELOPER__TOOLS` | `ai.assistants.developer.tools` | `list[str]` | `["*"]` | Tools this assistant may call (fnmatch over tool names); the developer assistant is also limited by ai.guardrails.tool_allowlist of the caller's role. |  |
| `AUTOAIR_AI__ASSISTANTS__DEVELOPER__MAX_STEPS` | `ai.assistants.developer.max_steps` | `int` | `10` | Model turns per chat turn. |  |
| `AUTOAIR_AI__ASSISTANTS__DEVELOPER__MEMORY` | `ai.assistants.developer.memory` | `bool` | `true` | Use and offer to store memories in this assistant's scope. |  |
| `AUTOAIR_AI__ASSISTANTS__DEVELOPER__ALLOW_EXTERNAL_MCP` | `ai.assistants.developer.allow_external_mcp` | `bool` | `true` | Attach the external MCP servers scoped to this assistant (ai.mcp_client.servers[].agents); their tools also go through this profile's tools allow-list, the developer assistant's ai.guardrails.tool_allowlist (mcp__<server>__<tool>) and, for the admin assistant, only tools the server marks read-only. |  |

## `ai.assistants.admin`

Admin assistant (read-only tools).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__ASSISTANTS__ADMIN__ENABLED` | `ai.assistants.admin.enabled` | `bool` | `true` | Offer this assistant in the chat panel. |  |
| `AUTOAIR_AI__ASSISTANTS__ADMIN__TOOLS` | `ai.assistants.admin.tools` | `list[str]` | `["*"]` | Tools this assistant may call (fnmatch over tool names); the developer assistant is also limited by ai.guardrails.tool_allowlist of the caller's role. |  |
| `AUTOAIR_AI__ASSISTANTS__ADMIN__MAX_STEPS` | `ai.assistants.admin.max_steps` | `int` | `12` | Model turns per chat turn. |  |
| `AUTOAIR_AI__ASSISTANTS__ADMIN__MEMORY` | `ai.assistants.admin.memory` | `bool` | `true` | Use and offer to store memories in this assistant's scope. |  |
| `AUTOAIR_AI__ASSISTANTS__ADMIN__ALLOW_EXTERNAL_MCP` | `ai.assistants.admin.allow_external_mcp` | `bool` | `true` | Attach the external MCP servers scoped to this assistant (ai.mcp_client.servers[].agents); their tools also go through this profile's tools allow-list, the developer assistant's ai.guardrails.tool_allowlist (mcp__<server>__<tool>) and, for the admin assistant, only tools the server marks read-only. |  |

## `ai.troubleshoot`

AI troubleshooting of DAG runs.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__TROUBLESHOOT__ENABLED` | `ai.troubleshoot.enabled` | `bool` | `true` | Offer 'Analyze with AI' on runs and tasks. |  |
| `AUTOAIR_AI__TROUBLESHOOT__MAX_TASKS` | `ai.troubleshoot.max_tasks` | `int` | `4` | Failed task instances whose logs are pre-loaded. |  |
| `AUTOAIR_AI__TROUBLESHOOT__MAX_TRIES` | `ai.troubleshoot.max_tries` | `int` | `4` | Tries per task instance whose logs are read. |  |
| `AUTOAIR_AI__TROUBLESHOOT__MAX_LOG_CHARS` | `ai.troubleshoot.max_log_chars` | `int` | `12000` | Tail of each try's log given to the agent. |  |
| `AUTOAIR_AI__TROUBLESHOOT__MAX_STEPS` | `ai.troubleshoot.max_steps` | `int` | `12` | Model turns per analysis. |  |
| `AUTOAIR_AI__TROUBLESHOOT__TIMEOUT_SECONDS` | `ai.troubleshoot.timeout_seconds` | `float` | `420` | Wall-clock limit of one analysis. |  |
| `AUTOAIR_AI__TROUBLESHOOT__ALLOW_EXTERNAL_MCP` | `ai.troubleshoot.allow_external_mcp` | `bool` | `true` | Let users attach allow-listed external MCP servers. |  |
| `AUTOAIR_AI__TROUBLESHOOT__STORE_URL` | `ai.troubleshoot.store_url` | `str \| None` | `None` | Where analyses (reports, proposals, decisions) live; default <state>/ai-troubleshoot (SQL rows with a database backend). |  |
| `AUTOAIR_AI__TROUBLESHOOT__AIRFLOW_ACTIONS` | `ai.troubleshoot.airflow_actions` | `'read_only' \| 'hitl' \| 'apply'` | `read_only` | What happens with the Airflow actions an analysis proposes (clear or retry a task, clear a run, mark a state): read_only = instructions only; hitl = each action gets an Execute button and runs only after the user approves it; apply = the vetted actions run automatically when the analysis completes. Actions always run with the user's own Airflow token, need run:manage and features.runs, and stay limited to allowed_actions and the analysed DAG. A request may only choose a more restrictive mode. |  |
| `AUTOAIR_AI__TROUBLESHOOT__ALLOWED_ACTIONS` | `ai.troubleshoot.allowed_actions` | `list['clear' \| 'retry' \| 'clear_run' \| 'mark_success' \| 'mark_failed' \| 'mark_run_success' \| 'mark_run_failed']` | `["clear", "retry", "clear_run"]` | Action types that may be executed (JSON list in the environment): clear (task instance), retry (a failed task instance), clear_run (only failed tasks unless the action says otherwise), mark_success / mark_failed (task instance), mark_run_success / mark_run_failed. Proposals of other types stay instructions. |  |
| `AUTOAIR_AI__TROUBLESHOOT__MAX_ACTIONS` | `ai.troubleshoot.max_actions` | `int` | `5` | Executable actions per analysis; further proposals stay instructions (0 = none). |  |
| `AUTOAIR_AI__TROUBLESHOOT__MAX_CLEARED_TASK_INSTANCES` | `ai.troubleshoot.max_cleared_task_instances` | `int` | `20` | Task instances one clear may reset (checked with a dry run first, downstream tasks included); larger clears are refused. |  |
| `AUTOAIR_AI__TROUBLESHOOT__ACTION_CLAIM_TIMEOUT_SECONDS` | `ai.troubleshoot.action_claim_timeout_seconds` | `float` | `300.0` | Seconds a hitl action stays claimed (status running) by the replica executing it; after that another approval may take it over (the replica died mid-call). Longer than the slowest Airflow clear. |  |
| `AUTOAIR_AI__TROUBLESHOOT__ALLOW_INCLUDE_DOWNSTREAM` | `ai.troubleshoot.allow_include_downstream` | `bool` | `true` | A clear may include the task's downstream tasks. |  |
| `AUTOAIR_AI__TROUBLESHOOT__ALLOW_INCLUDE_UPSTREAM` | `ai.troubleshoot.allow_include_upstream` | `bool` | `false` | A clear may include the task's upstream tasks. |  |
| `AUTOAIR_AI__TROUBLESHOOT__ACTIONS_OTHER_RUNS` | `ai.troubleshoot.actions_other_runs` | `bool` | `false` | Actions may target other runs of the analysed DAG (default: only the analysed run). |  |

## `ai.troubleshoot.resources`

Airflow resources it may read.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__TROUBLESHOOT__RESOURCES__CONNECTIONS` | `ai.troubleshoot.resources.connections` | `bool` | `true` | Connection metadata: conn_type, host, schema, port and the names of extra fields - never logins, passwords or extra values. |  |
| `AUTOAIR_AI__TROUBLESHOOT__RESOURCES__VARIABLES` | `ai.troubleshoot.resources.variables` | `bool` | `true` | Variable keys (never values). |  |
| `AUTOAIR_AI__TROUBLESHOOT__RESOURCES__POOLS` | `ai.troubleshoot.resources.pools` | `bool` | `true` | Pools and their slot usage. |  |
| `AUTOAIR_AI__TROUBLESHOOT__RESOURCES__XCOM_KEYS` | `ai.troubleshoot.resources.xcom_keys` | `bool` | `true` | XCom keys of the analysed run. |  |
| `AUTOAIR_AI__TROUBLESHOOT__RESOURCES__XCOM_VALUES` | `ai.troubleshoot.resources.xcom_values` | `bool` | `false` | XCom values (truncated, secrets scrubbed). |  |
| `AUTOAIR_AI__TROUBLESHOOT__RESOURCES__OTHER_RUNS` | `ai.troubleshoot.resources.other_runs` | `bool` | `true` | Other runs of the DAG (history). |  |
| `AUTOAIR_AI__TROUBLESHOOT__RESOURCES__IMPORT_ERRORS` | `ai.troubleshoot.resources.import_errors` | `bool` | `true` | DAG import errors. |  |
| `AUTOAIR_AI__TROUBLESHOOT__RESOURCES__AIRFLOW_CONFIG` | `ai.troubleshoot.resources.airflow_config` | `bool` | `false` | Airflow's configuration (GET /config; Airflow masks sensitive values and AutoAir scrubs secrets again). |  |

## `ai.troubleshoot.filesystem`

Sandboxed log/DAG file access.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__TROUBLESHOOT__FILESYSTEM__ROOTS` | `ai.troubleshoot.filesystem.roots` | `list[path]` | `[]` | Folders the agent may read (e.g. Airflow's base_log_folder); empty = no filesystem access. Symlinks and '..' cannot leave them. |  |
| `AUTOAIR_AI__TROUBLESHOOT__FILESYSTEM__INCLUDE_DAGS_FOLDER` | `ai.troubleshoot.filesystem.include_dags_folder` | `bool` | `false` | Also allow the DAGs folder (read-only). |  |
| `AUTOAIR_AI__TROUBLESHOOT__FILESYSTEM__ALLOW_GLOBS` | `ai.troubleshoot.filesystem.allow_globs` | `list[str]` | `["*"]` | File names the agent may read (fnmatch); directories are always listed. |  |
| `AUTOAIR_AI__TROUBLESHOOT__FILESYSTEM__DENY_GLOBS` | `ai.troubleshoot.filesystem.deny_globs` | `list[str]` | `[".env", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "id_rsa*", "id_ed25519*", "*secret*", "*credential*", "*.cfg", "*.ini", "*.db", "*.sqlite*"]` | File names never read (fnmatch, checked on every path component). |  |
| `AUTOAIR_AI__TROUBLESHOOT__FILESYSTEM__MAX_FILE_BYTES` | `ai.troubleshoot.filesystem.max_file_bytes` | `int` | `52428800` | Larger files are refused (only listed). |  |
| `AUTOAIR_AI__TROUBLESHOOT__FILESYSTEM__MAX_READ_CHARS` | `ai.troubleshoot.filesystem.max_read_chars` | `int` | `20000` | Characters returned by one read (head/tail/grep). |  |
| `AUTOAIR_AI__TROUBLESHOOT__FILESYSTEM__MAX_LIST_ENTRIES` | `ai.troubleshoot.filesystem.max_list_entries` | `int` | `200` | Entries returned by one directory listing. |  |

## `ai.mcp_client`

External MCP servers (agent tools).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__MCP_CLIENT__SERVERS` | `ai.mcp_client.servers` | `list[McpServerConfig]` | `[]` | Servers defined in the configuration. |  |
| `AUTOAIR_AI__MCP_CLIENT__DATABASE_SERVERS` | `ai.mcp_client.database_servers` | `bool` | `true` | Also offer servers defined in the admin panel (stored with the state; http/sse only - stdio commands can only come from the configuration). |  |
| `AUTOAIR_AI__MCP_CLIENT__MAX_TOOLS_PER_SERVER` | `ai.mcp_client.max_tools_per_server` | `int` | `40` | Tools of one server given to an agent. |  |
| `AUTOAIR_AI__MCP_CLIENT__MAX_OUTPUT_CHARS` | `ai.mcp_client.max_output_chars` | `int` | `12000` | Characters of one MCP tool result (truncated). |  |
| `AUTOAIR_AI__MCP_CLIENT__CONNECT_TIMEOUT_SECONDS` | `ai.mcp_client.connect_timeout_seconds` | `float` | `15` | Timeout of connecting and listing tools. |  |
| `AUTOAIR_AI__MCP_CLIENT__STORE_URL` | `ai.mcp_client.store_url` | `str \| None` | `None` | Where admin-defined servers live (fsspec URL; header/env secrets encrypted); default <state>/ai-mcp-servers (SQL: rows). |  |

## `ai.rules`

Project AI rules.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__RULES__ENABLED` | `ai.rules.enabled` | `bool` | `true` | Inject the project rules. |  |
| `AUTOAIR_AI__RULES__MAX_CHARS` | `ai.rules.max_chars` | `int` | `8000` | Characters of rules injected into one prompt. |  |
| `AUTOAIR_AI__RULES__STORE_URL` | `ai.rules.store_url` | `str \| None` | `None` | Where rules live; default <state>/ai-rules (SQL: rows). |  |
| `AUTOAIR_AI__RULES__CACHE_SECONDS` | `ai.rules.cache_seconds` | `float` | `10` | Seconds the rules are cached per process. |  |

## `ai.approvals`

Human approval of individual agent tool calls.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__APPROVALS__ENABLED` | `ai.approvals.enabled` | `bool` | `true` | Pause a run when the agent calls a tool that needs approval and ask the user who started it. False: such tools are not offered to the agent at all (it never runs them unapproved). |  |
| `AUTOAIR_AI__APPROVALS__AGENTS` | `ai.approvals.agents` | `dict['developer' \| 'admin' \| 'troubleshoot', ToolApprovalRule]` | `{}` | Rule per agent (developer, admin, troubleshoot), replacing default for that agent, e.g. {"developer": {"classes": ["write"], "exempt": ["mcp__jira__get_*"]}} (JSON in the environment). |  |
| `AUTOAIR_AI__APPROVALS__TTL_SECONDS` | `ai.approvals.ttl_seconds` | `float` | `3600` | Seconds a pending approval can be decided; afterwards it expires and the paused run cannot be resumed (the user asks again). |  |
| `AUTOAIR_AI__APPROVALS__MAX_PENDING_PER_USER` | `ai.approvals.max_pending_per_user` | `int` | `20` | Pending approvals per user; a new one beyond this cancels the oldest. |  |
| `AUTOAIR_AI__APPROVALS__RETENTION_HOURS` | `ai.approvals.retention_hours` | `float` | `168` | Hours decided, expired and cancelled approvals are kept (their outcome stays in the AI audit log). |  |
| `AUTOAIR_AI__APPROVALS__CLAIM_TIMEOUT_SECONDS` | `ai.approvals.claim_timeout_seconds` | `float` | `900` | Seconds a resumed approval may stay claimed by the replica running it; a claim older than this (the replica died) is shown as failed. It is never taken over, so an approved tool runs at most once. |  |
| `AUTOAIR_AI__APPROVALS__MAX_ARGUMENT_CHARS` | `ai.approvals.max_argument_chars` | `int` | `4000` | Characters of a tool call's arguments shown on the approval card (longer values are cut; secrets are always masked). |  |
| `AUTOAIR_AI__APPROVALS__MAX_REASON_CHARS` | `ai.approvals.max_reason_chars` | `int` | `1000` | Longest reason a user may give with a decision (it is sent to the model with a denial). 0 = no reasons. |  |
| `AUTOAIR_AI__APPROVALS__STORE_URL` | `ai.approvals.store_url` | `str \| None` | `None` | Where pending approvals live (fsspec URL); default <state>/ai-approvals (rows of the SQL database when database.backend is sqlite/postgresql). |  |

## `ai.approvals.default`

Rule of every agent without its own entry in agents.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__APPROVALS__DEFAULT__CLASSES` | `ai.approvals.default.classes` | `list['read' \| 'write' \| 'execute']` | `["write"]` | Tool classes whose calls need approval (JSON list in the environment): write = tools that change something (stored memories, external MCP tools not listed in the server's read_only_tools) and tools that run Python on the server (class execute: import_python_dag, deploy_dry_run); execute = only the latter; read = every read-only tool too. [] = none by class. |  |
| `AUTOAIR_AI__APPROVALS__DEFAULT__TOOLS` | `ai.approvals.default.tools` | `list[str]` | `[]` | Tool names (fnmatch, e.g. mcp__jira__*) that always need approval, whatever their class; takes precedence over exempt. |  |
| `AUTOAIR_AI__APPROVALS__DEFAULT__EXEMPT` | `ai.approvals.default.exempt` | `list[str]` | `[]` | Tool names (fnmatch) that never need approval although their class does, e.g. mcp__metrics__*. Ignored for write and execute tools of a troubleshooting analysis in hitl mode, and for remember (a stored memory always needs approval). |  |

## `ai.upgrade`

Airflow upgrade assistant.

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_AI__UPGRADE__RUFF_RULES` | `ai.upgrade.ruff_rules` | `list[str]` | `["AIR3"]` | ruff rule selectors run on the generated code (AIR3xx: Airflow 3 migration rules). |  |
| `AUTOAIR_AI__UPGRADE__MAX_DAGS` | `ai.upgrade.max_dags` | `int` | `100` | DAGs analysed by one 'all DAGs' request. |  |

## `collab`

Sharing live-editing rooms between replicas (AUTOAIR_COLLAB__<FIELD>).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_COLLAB__SYNC` | `collab.sync` | `'auto' \| 'memory' \| 'database' \| 'redis'` | `auto` | memory: rooms live in one process (several replicas need sticky sessions); database: share rooms through the SQL database (PostgreSQL LISTEN/NOTIFY, SQLite polling); redis: Redis pub/sub (collab.redis_url); auto: redis when redis_url is set, else database with a SQL backend, else memory. |  |
| `AUTOAIR_COLLAB__REDIS_URL` | `collab.redis_url` | `secret str \| None` | `None` | Redis URL for pub/sub fan-out, e.g. redis://redis:6379/0 (pip install 'autoair[collab-redis]'). | yes |
| `AUTOAIR_COLLAB__REDIS_CHANNEL` | `collab.redis_channel` | `str` | `autoair:collab` | Redis pub/sub channel. |  |
| `AUTOAIR_COLLAB__POLL_SECONDS` | `collab.poll_seconds` | `float` | `0.25` | Database fan-out: how often a replica reads new messages (PostgreSQL is woken by NOTIFY and polls only as a fallback, 20x slower). |  |
| `AUTOAIR_COLLAB__JOIN_TIMEOUT` | `collab.join_timeout` | `float` | `1.5` | Seconds a replica opening a room waits for the others to send its current state (shared sync only). |  |
| `AUTOAIR_COLLAB__SNAPSHOT_SECONDS` | `collab.snapshot_seconds` | `float` | `5` | Shared sync: how often a changed room is saved to the database. |  |
| `AUTOAIR_COLLAB__SNAPSHOT_TTL_SECONDS` | `collab.snapshot_ttl_seconds` | `float` | `300` | A saved room is restored only when it changed within this many seconds (older ones are stale: the stored spec is the source of truth). |  |
| `AUTOAIR_COLLAB__RETENTION_SECONDS` | `collab.retention_seconds` | `float` | `300` | Database fan-out: how long relayed messages are kept before pruning. |  |
| `AUTOAIR_COLLAB__SESSION_SECONDS` | `collab.session_seconds` | `float` | `300` | A live-editing connection is closed after this many seconds; the browser reconnects with a new ticket, which re-checks DAG edit permission, so a revoked permission ends co-editing within this interval. Lower is stricter (one ticket request per user and interval). |  |

## `database`

Where AutoAir keeps its own state: files (default), SQLite or PostgreSQL (AUTOAIR_DATABASE__<FIELD>).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_DATABASE__BACKEND` | `database.backend` | `'file' \| 'sqlite' \| 'postgresql'` | `file` | file: JSON files on fsspec URLs (single replica or shared volume); sqlite: one SQLite file; postgresql: a PostgreSQL server (multi-replica). |  |
| `AUTOAIR_DATABASE__URL` | `database.url` | `str \| None` | `None` | SQLAlchemy URL, e.g. postgresql+psycopg://autoair@db:5432/autoair or sqlite:////var/lib/autoair/autoair.db (default for sqlite: <state>/autoair.db). Credentials in the URL are masked when the configuration is exposed. |  |
| `AUTOAIR_DATABASE__PASSWORD` | `database.password` | `secret str \| None` | `None` | Database password, merged into url (keeps the secret out of the URL). | yes |
| `AUTOAIR_DATABASE__POOL_SIZE` | `database.pool_size` | `int` | `5` | Connections kept open. |  |
| `AUTOAIR_DATABASE__MAX_OVERFLOW` | `database.max_overflow` | `int` | `10` | Extra connections beyond pool_size under load. |  |
| `AUTOAIR_DATABASE__POOL_TIMEOUT` | `database.pool_timeout` | `float` | `30` | Seconds to wait for a free connection. |  |
| `AUTOAIR_DATABASE__POOL_RECYCLE` | `database.pool_recycle` | `int` | `1800` | Seconds after which a connection is replaced (-1: never); keep it below the server's / proxy's idle timeout. |  |
| `AUTOAIR_DATABASE__POOL_PRE_PING` | `database.pool_pre_ping` | `bool` | `true` | Test a pooled connection before use (survives restarts). |  |
| `AUTOAIR_DATABASE__CONNECT_ARGS` | `database.connect_args` | `dict[str, Any]` | `{}` | Extra DBAPI connect() arguments (psycopg / sqlite3), e.g. {"connect_timeout": 10, "application_name": "autoair"}. |  |
| `AUTOAIR_DATABASE__SSL_MODE` | `database.ssl_mode` | `'disable' \| 'allow' \| 'prefer' \| 'require' \| 'verify-ca' \| 'verify-full' \| None` | `None` | PostgreSQL sslmode (disable, allow, prefer, require, verify-ca, verify-full); unset: the driver default. |  |
| `AUTOAIR_DATABASE__SSL_ROOT_CERT` | `database.ssl_root_cert` | `str \| None` | `None` | PostgreSQL: CA bundle verifying the server (sslrootcert). |  |
| `AUTOAIR_DATABASE__ECHO` | `database.echo` | `bool` | `false` | Log every SQL statement (debugging). |  |
| `AUTOAIR_DATABASE__SCHEMA_NAME` | `database.schema_name` | `str \| None` | `None` | PostgreSQL schema holding AutoAir's tables (default: the search path). |  |
| `AUTOAIR_DATABASE__TABLE_PREFIX` | `database.table_prefix` | `str` | `autoair_` | Prefix of every AutoAir table (share a database with other apps). |  |
| `AUTOAIR_DATABASE__MIGRATE_ON_START` | `database.migrate_on_start` | `bool` | `true` | Apply schema migrations when the server starts; turn off when an init job runs `autoair db upgrade` (several replicas starting at once). |  |
| `AUTOAIR_DATABASE__HEARTBEAT_SECONDS` | `database.heartbeat_seconds` | `float` | `30` | How often a replica records that it is alive (admin panel replicas). |  |
| `AUTOAIR_DATABASE__SYNC_SECONDS` | `database.sync_seconds` | `float` | `5` | SQL backends: how often a replica picks up shared changes made on another replica (the deploy target chosen in the UI). |  |
| `AUTOAIR_DATABASE__SHARED_RATE_LIMIT` | `database.shared_rate_limit` | `bool` | `true` | SQL backends: count failed sign-ins in the database so the limit holds across replicas (file backend: per replica, in memory). |  |

## `storage`

Deploy target: local folder, object storage or git (AUTOAIR_STORAGE__*).

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_STORAGE__MODE` | `storage.mode` | `'local' \| 'object' \| 'git'` | `local` | Deploy target: a local folder, object storage (fsspec) or a git repository. |  |
| `AUTOAIR_STORAGE__ALLOWED_TARGETS` | `storage.allowed_targets` | `list['local' \| 'object' \| 'git']` | `["local", "git", "object"]` | Target kinds the deploy-target dialog/API offers and accepts, e.g. ['git'] (only repositories) or ['local'] (only the DAG folder). Admin policy: the UI cannot change it. |  |
| `AUTOAIR_STORAGE__LOCAL_PATH` | `storage.local_path` | `path \| None` | `None` | mode=local: folder written to (default: dags_folder/subfolder). |  |
| `AUTOAIR_STORAGE__OBJECT_URL` | `storage.object_url` | `str \| None` | `None` | mode=object: fsspec URL of the bundle prefix. |  |
| `AUTOAIR_STORAGE__STORAGE_OPTIONS` | `storage.storage_options` | `dict[str, Any]` | `{}` | mode=object: fsspec storage options (credentials, endpoint…). | yes |
| `AUTOAIR_STORAGE__HISTORY_LIMIT` | `storage.history_limit` | `int` | `100` | Saved versions kept per DAG. |  |
| `AUTOAIR_STORAGE__OBJECT_LOCK_SECONDS` | `storage.object_lock_seconds` | `float` | `300.0` | mode=object: seconds a write lease of a DAG lasts. Writers of one DAG hold it (a document in AutoAir's state backend, shared by every replica with database.backend sqlite/postgresql) while they check base_etag and write, so two replicas never both win; a replica that died holding it blocks that DAG's deploys at most this long. Must exceed the longest save. |  |
| `AUTOAIR_STORAGE__OBJECT_LOCK_WAIT_SECONDS` | `storage.object_lock_wait_seconds` | `float` | `30.0` | mode=object: seconds a save waits for another writer's lease on the same DAG before it fails with a conflict (409). |  |
| `AUTOAIR_STORAGE__REQUIRE_BASE_ETAG` | `storage.require_base_etag` | `bool` | `true` | Overwriting or deleting an existing DAG spec needs the etag it was loaded with (REST: base_etag or If-Match on PUT, If-Match on DELETE, else HTTP 428 Precondition Required with the current etag; MCP save_dag_spec and delete_dag_spec: base_etag, else the call is refused with current_etag). false lets legacy API clients overwrite or delete without one (last writer wins). Creating a new DAG never needs an etag. |  |
| `AUTOAIR_STORAGE__FILE_MODE` | `storage.file_mode` | `str` | `0644` | mode=local: permissions (octal) of written files. |  |
| `AUTOAIR_STORAGE__DIR_MODE` | `storage.dir_mode` | `str \| None` | `None` | mode=local: permissions (octal) of created folders (default: umask). |  |
| `AUTOAIR_STORAGE__TEMP_DIR` | `storage.temp_dir` | `path \| None` | `None` | mode=local: folder for the temporary file of atomic writes (default: the target folder). Must be on the same file system, or writes stop being atomic. |  |

## `storage.git`

| Environment variable | Config key | Type | Default | Description | Secret |
|---|---|---|---|---|---|
| `AUTOAIR_STORAGE__GIT__PROVIDER` | `storage.git.provider` | `'local' \| 'github' \| 'gitlab' \| 'bitbucket' \| 'azure' \| 'gitea'` | `local` | Git host API: local (git CLI, any remote), github, gitlab, bitbucket, azure, gitea (Gitea/Forgejo: git CLI push + pull requests over the REST API). |  |
| `AUTOAIR_STORAGE__GIT__REPO` | `storage.git.repo` | `str \| None` | `None` | owner/name (github), project id or path (gitlab), workspace/repo (bitbucket), organization/project/repository (azure), remote URL (local, gitea). |  |
| `AUTOAIR_STORAGE__GIT__USERNAME` | `storage.git.username` | `str \| None` | `None` | Bitbucket app-password user (omit for access tokens). |  |
| `AUTOAIR_STORAGE__GIT__AZURE_BEARER` | `storage.git.azure_bearer` | `bool` | `false` | Azure DevOps: token is an Entra ID bearer token, not a PAT. |  |
| `AUTOAIR_STORAGE__GIT__BASE_BRANCH` | `storage.git.base_branch` | `str \| None` | `None` | Defaults to the bundle ref/main. |  |
| `AUTOAIR_STORAGE__GIT__SUBDIR` | `storage.git.subdir` | `str \| None` | `None` | Folder inside the repository. |  |
| `AUTOAIR_STORAGE__GIT__MODE` | `storage.git.mode` | `'pr' \| 'push'` | `pr` | pr: a branch + pull request per DAG; push: commit straight to the branch. |  |
| `AUTOAIR_STORAGE__GIT__BRANCH_PREFIX` | `storage.git.branch_prefix` | `str` | `autoair/` | Prefix of pending-change branches (mode=pr). |  |
| `AUTOAIR_STORAGE__GIT__TOKEN` | `storage.git.token` | `secret str \| None` | `None` | Personal/project access token, app password or PAT. | yes |
| `AUTOAIR_STORAGE__GIT__API_URL` | `storage.git.api_url` | `str \| None` | `None` | REST API base URL (GitHub Enterprise, self-hosted GitLab, Azure DevOps Server…); default: the public host's. gitea: default <scheme>://<host>/api/v1 of an http(s) repo URL (required for SSH remotes). |  |
| `AUTOAIR_STORAGE__GIT__ENVIRONMENTS` | `storage.git.environments` | `dict[str, str]` | `{}` | Environment name -> branch, e.g. {'prod': 'main'}. |  |
| `AUTOAIR_STORAGE__GIT__USE_AIRFLOW_BUNDLE` | `storage.git.use_airflow_bundle` | `bool` | `false` | Fill repo/branch/subdir/credentials from Airflow's GitDagBundle config. |  |
| `AUTOAIR_STORAGE__GIT__BUNDLE_NAME` | `storage.git.bundle_name` | `str \| None` | `None` | GitDagBundle to read (use_airflow_bundle; default: the first). |  |
| `AUTOAIR_STORAGE__GIT__CONN_ID` | `storage.git.conn_id` | `str \| None` | `None` | Airflow git connection id. |  |
| `AUTOAIR_STORAGE__GIT__CREDENTIALS_TTL` | `storage.git.credentials_ttl` | `float` | `60` | Seconds the credentials read from an Airflow connection (conn_id, forced_conn_id, the bundle's or a user's own) are reused before the connection is read again; on an authentication failure it is re-read at once and the operation retried, so a rotated token works without a restart. 0 = read on every operation. |  |
| `AUTOAIR_STORAGE__GIT__CACHE_DIR` | `storage.git.cache_dir` | `path \| None` | `None` | Clone cache (provider=local). |  |
| `AUTOAIR_STORAGE__GIT__SSH_KEY_FILE` | `storage.git.ssh_key_file` | `path \| None` | `None` | provider=local: SSH key. |  |
| `AUTOAIR_STORAGE__GIT__SSH_KEY_CACHE_DAYS` | `storage.git.ssh_key_cache_days` | `int` | `30` | provider=local/gitea: days an SSH private key from a git connection stays in the clone cache (<cache_dir>/keys/id_autoair-<hash>, mode 0600) after its last use. Writing a new key file removes AutoAir's key files unused for longer (only files of that name owned by this user, never the key being used). 0 keeps them. |  |
| `AUTOAIR_STORAGE__GIT__GITHUB_APP_ID` | `storage.git.github_app_id` | `str \| None` | `None` | GitHub App id. |  |
| `AUTOAIR_STORAGE__GIT__GITHUB_INSTALLATION_ID` | `storage.git.github_installation_id` | `str \| None` | `None` | GitHub App installation id. |  |
| `AUTOAIR_STORAGE__GIT__GITHUB_PRIVATE_KEY` | `storage.git.github_private_key` | `secret str \| None` | `None` | GitHub App private key (PEM). | yes |
| `AUTOAIR_STORAGE__GIT__GITHUB_PRIVATE_KEY_FILE` | `storage.git.github_private_key_file` | `path \| None` | `None` | GitHub App private key file (PEM). |  |
| `AUTOAIR_STORAGE__GIT__FETCH_INTERVAL` | `storage.git.fetch_interval` | `float` | `5.0` | provider=local: seconds between fetches of the remote. |  |
| `AUTOAIR_STORAGE__GIT__DIRECT_PUSH_ROLES` | `storage.git.direct_push_roles` | `list[str] \| None` | `None` | AutoAir roles allowed to push straight to the target branch (deploy request flag direct_push=true in mode=pr; see iam.force_pr_without_direct_push for mode=push). Evaluated by autoair.iam; unset = iam.direct_push_roles applies (default ['Admin']); [] = nobody, except users granted 'deploy:direct_push' explicitly. |  |
| `AUTOAIR_STORAGE__GIT__FORCED_CONN_ID` | `storage.git.forced_conn_id` | `str \| None` | `None` | Admin policy: the Airflow connection every git deploy uses. Overrides conn_id (and the bundle's connection); the deploy-target dialog cannot change it. |  |
| `AUTOAIR_STORAGE__GIT__CREDENTIALS_MODE` | `storage.git.credentials_mode` | `'shared' \| 'per_user'` | `shared` | shared: one service connection/token for everybody; per_user: commits and pull requests use the deploying user's own Airflow connection (see user_conn_id_template), read with the user's token so Airflow's RBAC applies. Reads (listing, history) keep the shared credentials when configured. |  |
| `AUTOAIR_STORAGE__GIT__USER_CONN_ID_TEMPLATE` | `storage.git.user_conn_id_template` | `str` | `git_{username}` | per_user: the user's connection id. Placeholders: {username} (the Airflow username, lower-cased; characters outside [A-Za-z0-9_.-] become '_'). |  |
| `AUTOAIR_STORAGE__GIT__USER_CONN_ID_PATTERN` | `storage.git.user_conn_id_pattern` | `str \| None` | `None` | per_user: regex (fullmatch) of connection ids a user may pick in the deploy dialog instead of the template's (e.g. 'git_.*'); unset: only the template. |  |
| `AUTOAIR_STORAGE__GIT__USER_CREDENTIALS_FALLBACK` | `storage.git.user_credentials_fallback` | `bool` | `false` | per_user: use the shared credentials when the user has no readable connection (otherwise such deploys are refused). |  |
| `AUTOAIR_STORAGE__GIT__REQUEST_TOKEN` | `storage.git.request_token` | `'off' \| 'optional' \| 'required'` | `optional` | Git token the user types for one deploy, delete, promotion or pull request (header X-AutoAir-Git-Token, never stored, logged or audited): off = the header is refused; optional = a token, when given, replaces the configured credentials for that one write; required = every git write needs one (the configured connection, if any, only serves reads; MCP git writes need mcp_request_token and the header). A team target uses its own team_storage.<team>.git.request_token, but off here turns request tokens off for every target. With forced_conn_id set, tokens are refused unless request_token_overrides_forced. |  |
| `AUTOAIR_STORAGE__GIT__REQUEST_TOKEN_OVERRIDES_FORCED` | `storage.git.request_token_overrides_forced` | `bool` | `false` | A request token may replace the admin-forced connection (forced_conn_id) for that one write. Default: the forced connection always wins and the X-AutoAir-Git-Token header is refused. |  |
| `AUTOAIR_STORAGE__GIT__MCP_REQUEST_TOKEN` | `storage.git.mcp_request_token` | `bool` | `false` | MCP over HTTP: save_dag_spec / delete_dag_spec take the user's git token from the X-AutoAir-Git-Token header of the MCP request (set in the MCP client's server configuration; never a tool argument, so it stays out of the model's context and transcripts). request_token, the forced-connection rule and the HTTPS rule apply as for the REST API. Off: such a header is refused. |  |
| `AUTOAIR_STORAGE__GIT__REQUEST_TOKEN_ALLOW_INSECURE` | `storage.git.request_token_allow_insecure` | `bool` | `false` | Accept X-AutoAir-Git-Token over plain HTTP from a non-loopback client (the scheme and client address honour server.proxy_headers / forwarded_allow_ips), and send it to http:// git hosts. Only for test setups. |  |
| `AUTOAIR_STORAGE__GIT__COMMIT_AUTHOR` | `storage.git.commit_author` | `'auto' \| 'service' \| 'user'` | `auto` | Commit author: service = the service identity (the user is credited in trailers and Co-authored-by); user = the deploying user ('Name <email>'); auto = user with per_user credentials, else service. GitHub's API always commits as the token's owner (the user is credited with Co-authored-by). |  |
| `AUTOAIR_STORAGE__GIT__BRANCH_TEMPLATE` | `storage.git.branch_template` | `str` | `{branch_prefix}{environment_path}{dag_id}` | mode=pr: branch of a DAG's pending change. Placeholders: {branch_prefix}, {dag_id}, {environment} (may be empty), {environment_path} ('<env>/' or ''). |  |
| `AUTOAIR_STORAGE__GIT__PROMOTE_BRANCH_TEMPLATE` | `storage.git.promote_branch_template` | `str` | `{branch_prefix}promote/{from_env}-to-{to_env}/{dag_id}` | Branch of an environment promotion. Placeholders: {branch_prefix}, {dag_id}, {from_env}, {to_env}. |  |
| `AUTOAIR_STORAGE__GIT__COMMIT_MESSAGE_TEMPLATE` | `storage.git.commit_message_template` | `str` | `AutoAir: update DAG {dag_id}` | Commit message of a save without a user message. Placeholders: {dag_id}, {environment}, {author} and the context placeholders ({user}, {user_name}, {user_email}, {autoair_version}, {etag_before}, {etag_after}, {validation}, {approvals}, {link}, {target}, {description}, {context}). The context block and trailers are always appended unless the template places {context} itself. |  |
| `AUTOAIR_STORAGE__GIT__DELETE_MESSAGE_TEMPLATE` | `storage.git.delete_message_template` | `str` | `AutoAir: delete DAG {dag_id}` | Commit message (and PR title) of a delete. Placeholders: {dag_id}, {environment}, {author} and the context placeholders (see commit_message_template). |  |
| `AUTOAIR_STORAGE__GIT__PROMOTE_MESSAGE_TEMPLATE` | `storage.git.promote_message_template` | `str` | `AutoAir: promote DAG {dag_id} from {from_env} to {to_env}` | Commit message (and PR title) of a promotion. Placeholders: {dag_id}, {from_env}, {to_env}, {author} and the context placeholders. |  |
| `AUTOAIR_STORAGE__GIT__PR_TITLE_TEMPLATE` | `storage.git.pr_title_template` | `str` | `AutoAir: update DAG {dag_id}` | Pull request title of a save. Placeholders: {dag_id}, {environment}, {author}, {message} (first line of the commit message) and the context placeholders. |  |
| `AUTOAIR_STORAGE__GIT__PR_BODY_TEMPLATE` | `storage.git.pr_body_template` | `str` | `{message}  {description}  Opened by AutoAir.` | Pull request description. Placeholders: {message} (the commit message without the context), {description} (the PR description typed in the deploy dialog), {dag_id}, {environment}, {author} and the context placeholders. The context block is appended unless the template places {context}; it cannot be removed. |  |
| `AUTOAIR_STORAGE__GIT__PR_LABELS` | `storage.git.pr_labels` | `list[str]` | `[]` | Labels put on opened pull requests (GitHub, GitLab, Azure DevOps, Gitea). |  |
| `AUTOAIR_STORAGE__GIT__PR_REVIEWERS` | `storage.git.pr_reviewers` | `list[str]` | `[]` | Reviewers requested on opened pull requests: GitHub logins (or 'org/team' for teams), GitLab usernames, Bitbucket account UUIDs, Azure DevOps identity ids, Gitea usernames. |  |
| `AUTOAIR_STORAGE__GIT__PR_DRAFT` | `storage.git.pr_draft` | `bool` | `false` | Open pull requests as drafts. |  |
| `AUTOAIR_STORAGE__GIT__DELETE_BRANCH_AFTER_MERGE` | `storage.git.delete_branch_after_merge` | `bool` | `true` | Ask the host to delete the source branch when the pull request is merged (GitLab, Bitbucket, Azure DevOps; on GitHub this is a repository setting). |  |
| `AUTOAIR_STORAGE__GIT__COMMITTER_NAME` | `storage.git.committer_name` | `str` | `AutoAir` | provider=local: committer name of AutoAir commits. |  |
| `AUTOAIR_STORAGE__GIT__COMMITTER_EMAIL` | `storage.git.committer_email` | `str` | `autoair@localhost` | provider=local: committer e-mail (also the author when none is known). |  |
| `AUTOAIR_STORAGE__GIT__TIMEOUT` | `storage.git.timeout` | `float` | `30` | Seconds per request to the git host's REST API. |  |
| `AUTOAIR_STORAGE__GIT__COMMAND_TIMEOUT` | `storage.git.command_timeout` | `float` | `300` | provider=local: seconds a git command may take. |  |
| `AUTOAIR_STORAGE__GIT__ENV_PASSTHROUGH` | `storage.git.env_passthrough` | `list[str]` | `[]` | provider=local/gitea: environment variables (fnmatch patterns, case-insensitive) the git CLI inherits besides the ones git, ssh and curl read (PATH, HOME, locale, TMPDIR, SSH_AUTH_SOCK, SSL_CERT_*, proxy variables, GIT_*), e.g. ['KRB5CCNAME'] for Kerberos. Nothing else of the server's environment reaches git. |  |
| `AUTOAIR_STORAGE__GIT__MAX_ATTEMPTS` | `storage.git.max_attempts` | `int` | `3` | Saves retried when the branch moves between read and commit. |  |
| `AUTOAIR_STORAGE__GIT__READ_CACHE_SIZE` | `storage.git.read_cache_size` | `int` | `512` | File reads at immutable commits kept in memory. |  |

