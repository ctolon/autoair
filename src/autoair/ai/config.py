"""AI / MCP configuration: the ``ai`` section of :class:`autoair.settings.Settings`.

Environment variables use the nested form ``AUTOAIR_AI__<FIELD>`` and
``AUTOAIR_AI__<SUB>__<FIELD>`` (e.g. ``AUTOAIR_AI__PROVIDER=openai``,
``AUTOAIR_AI__RETRIEVAL__TOP_K=10``). Complex values (lists, dicts) are JSON.

This module must stay import-light (pydantic + stdlib only): ``autoair.settings``
imports it at start-up, including in the Airflow plugin.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

ProviderLiteral = Literal[
    "anthropic",
    "openai",
    "azure",
    "gemini",
    "google-cloud",
    "ollama",
    "bedrock",
    "groq",
    "mistral",
    "cohere",
]
Role = Literal["viewer", "editor", "admin"]

DEFAULT_RESEARCH_DOMAINS = [
    "airflow.apache.org",
    "pypi.org",
    "raw.githubusercontent.com",
]

#: Tools each role may let the agent call (fnmatch patterns over tool names). Tools that
#: execute user code (DagBag import in ``deploy_dry_run``, ``import_python_dag``) or read
#: Airflow logs need edit rights; online research is open to everyone when enabled.
DEFAULT_TOOL_ALLOWLIST: dict[str, list[str]] = {
    "viewer": [
        "search_*",
        "get_operator",
        "validate_spec",
        "lint_spec",
        "render_code",
        "get_saved_spec",
        "list_dag_runs",
        "list_task_instances",
        "run_statistics",
        "read_source",
        "list_installed_packages",
        "get_package_info",
        "fetch_doc",
        # developer assistant: read the open DAG, propose reviewable diffs, memory
        "get_current_spec",
        "propose_spec_change",
        "write_code",
        "list_conventions",
        "remember",
    ],
    "editor": ["*"],
    "admin": ["*"],
}


PiiEntity = Literal["email", "phone", "credit_card", "iban", "ipv4", "ssn"]


def _default_pii() -> list[PiiEntity]:
    return ["email", "phone", "credit_card", "iban", "ssn"]


def _all_providers() -> list[ProviderLiteral]:
    return ["anthropic", "openai", "gemini", "ollama"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class EmbeddingSettings(_Section):
    """Dense embeddings for hybrid retrieval (BM25 + vectors)."""

    provider: Literal["openai", "gemini", "ollama", "cohere", "bedrock"] | None = Field(
        default=None,
        description="Embeddings provider for hybrid retrieval (pydantic-ai embeddings); "
        "default: keyword (BM25) only.",
    )
    model: str | None = Field(default=None, description="Embedding model id (provider default).")
    base_url: str | None = Field(
        default=None, description="Embeddings endpoint (default: the chat provider's URL)."
    )
    api_key: SecretStr | None = Field(
        default=None, description="API key for the embeddings provider (default: chat key)."
    )
    batch_size: int = Field(default=64, ge=1, le=2048, description="Texts per embedding call.")
    max_chars: int = Field(
        default=1500, ge=100, description="Characters of each chunk that are embedded."
    )


class AgentSettings(_Section):
    """The tool-using agents (ask, chat assistants, troubleshooting) on pydantic-ai."""

    max_steps: int = Field(default=8, ge=1, le=50, description="Model turns per agent run.")
    token_budget: int = Field(
        default=250_000, ge=1_000, description="Tokens (in + out) one agent run may use."
    )
    cost_budget_usd: float | None = Field(
        default=None,
        ge=0,
        description="USD one agent run may spend (needs `pricing`); unlimited when unset.",
    )
    timeout_seconds: float = Field(
        default=300, gt=0, description="Wall-clock limit of one agent/writer run."
    )
    tool_timeout_seconds: float = Field(
        default=90, gt=0, description="Wall-clock limit of one tool call."
    )
    max_tool_output_chars: int = Field(
        default=12_000, ge=500, description="Tool output given back to the model (truncated)."
    )
    max_tokens_per_call: int = Field(
        default=8_000, ge=256, description="max_tokens of each agent model call."
    )
    final_turn_grace_seconds: float = Field(
        default=30,
        ge=0,
        description="Wall-clock seconds reserved for the final (forced answer) turn of a run "
        "(at most a quarter of `timeout_seconds`); on timeout the run returns its completed "
        "steps with `stopped=timeout`.",
    )


class RetrievalSettings(_Section):
    """Hybrid search (BM25 + embeddings, RRF), reranking, chunking and caching."""

    top_k: int = Field(default=8, ge=1, le=100, description="Default number of hits.")
    candidate_pool: int = Field(
        default=40, ge=5, le=1000, description="Candidates per ranker before fusion/rerank."
    )
    rrf_k: int = Field(default=60, ge=1, description="Reciprocal Rank Fusion constant.")
    bm25_k1: float = Field(default=1.4, gt=0, description="BM25 term-frequency saturation.")
    bm25_b: float = Field(default=0.7, ge=0, le=1, description="BM25 length normalisation.")
    title_boost: int = Field(default=3, ge=1, le=10, description="Title term weight in BM25.")
    chunk_size: int = Field(
        default=1_200, ge=200, description="Target characters per chunk (docs and code)."
    )
    chunk_overlap: int = Field(default=150, ge=0, description="Characters shared by chunks.")
    index_ttl_seconds: float = Field(
        default=120, ge=0, description="Seconds before sources are re-read for changes."
    )
    query_cache_size: int = Field(default=256, ge=0, description="Cached query results.")
    embedding_cache_max_entries: int = Field(
        default=100_000,
        ge=0,
        description="Vectors kept in the on-disk embedding cache (oldest pruned; 0 = no cap).",
    )
    query_cache_ttl_seconds: float = Field(
        default=300, ge=0, description="Seconds a cached query result is reused."
    )
    query_expansion: bool = Field(
        default=True, description="Expand queries with Airflow synonyms/abbreviations."
    )
    llm_query_rewrite: bool = Field(
        default=False, description="Let the LLM rewrite research queries (extra call)."
    )
    synonyms: dict[str, list[str]] = Field(
        default_factory=dict,
        description='Extra query-expansion vocabulary, e.g. {"dwh": ["snowflake", "warehouse"]} '
        "(merged over the built-in Airflow/cloud terms).",
    )
    reranker: Literal["none", "cross_encoder", "llm", "cohere", "voyage", "jina"] = Field(
        default="none",
        description="Reranker of the fused candidates: cross_encoder (autoair[ai-rerank]), "
        "llm (the configured model), or a hosted rerank API.",
    )
    rerank_model: str | None = Field(
        default=None, description="Reranker model id (default per reranker)."
    )
    rerank_api_key: SecretStr | None = Field(
        default=None, description="API key of the hosted rerank API."
    )
    rerank_base_url: str | None = Field(
        default=None, description="Override the hosted rerank API endpoint."
    )
    rerank_top_n: int = Field(default=20, ge=1, le=200, description="Candidates reranked.")
    context_max_chars: int = Field(
        default=4_000, ge=500, description="Retrieved context injected into one prompt."
    )
    max_specs: int = Field(default=300, ge=0, description="Saved DAGs indexed.")
    max_templates: int = Field(default=300, ge=0, description="Templates indexed.")

    @model_validator(mode="after")
    def _check_overlap(self) -> RetrievalSettings:
        # A large overlap makes every chunk re-cover the previous one (and could stall
        # the splitter): keep it below half a chunk.
        if self.chunk_overlap >= self.chunk_size // 2:
            raise ValueError(
                f"chunk_overlap ({self.chunk_overlap}) must be smaller than half of "
                f"chunk_size ({self.chunk_size})"
            )
        return self


class ResearchSettings(_Section):
    """Agentic research over the installed environment and (optionally) the internet."""

    online: bool = Field(
        default=False,
        description="Allow fetching documentation from the internet (allow-listed domains). "
        "Off = air-gapped: only installed packages, AutoAir docs and the knowledge base.",
    )
    allowed_domains: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RESEARCH_DOMAINS),
        description="Domains online research may fetch (sub-domains included).",
    )
    proxy: str | None = Field(default=None, description="HTTP(S) proxy for online research.")
    timeout_seconds: float = Field(default=15, gt=0, description="Timeout of one fetch.")
    max_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1024, description="Largest response body read."
    )
    max_redirects: int = Field(default=3, ge=0, le=10, description="Redirects followed.")
    rate_limit_per_minute: int = Field(
        default=30, ge=1, description="Fetches per minute per domain (token bucket)."
    )
    cache_ttl_seconds: float = Field(
        default=86_400, ge=0, description="Seconds a fetched page is reused."
    )
    cache_dir: Path | None = Field(
        default=None, description="Fetch cache folder (default <state>/ai-research-cache)."
    )
    index_installed: bool = Field(
        default=True,
        description="Index docstrings, provider metadata and examples of installed packages.",
    )
    package_patterns: list[str] = Field(
        default_factory=lambda: ["apache-airflow*", "airflow*"],
        description="Distributions whose modules are indexed (fnmatch, normalised names).",
    )
    extra_source_dirs: list[Path] = Field(
        default_factory=list,
        description="More folders research may read (besides site-packages and AutoAir "
        "custom operator paths).",
    )
    max_source_chars: int = Field(
        default=60_000, ge=1_000, description="Characters returned by read_source."
    )
    max_index_modules: int = Field(
        default=4_000, ge=10, description="Modules parsed when indexing installed packages."
    )
    user_agent: str = Field(
        default="AutoAir-Research/1.0 (+https://github.com/ctolon/autoair)",
        description="User-Agent of online fetches.",
    )


class GuardrailSettings(_Section):
    """Input/output checks around every model call."""

    redact_secrets: bool = Field(
        default=True, description="Mask secret-looking strings before text leaves AutoAir."
    )
    redact_patterns: list[str] = Field(
        default_factory=list, description="Extra regexes whose matches are redacted."
    )
    pii_masking: bool = Field(
        default=False, description="Mask personal data (emails, phones, cards, IBANs, IPs)."
    )
    pii_entities: list[PiiEntity] = Field(
        default_factory=_default_pii,
        description="Kinds of personal data masked when pii_masking is on.",
    )
    prompt_injection: Literal["off", "flag", "block"] = Field(
        default="flag",
        description="Injection heuristics on retrieved content / tool outputs: 'flag' wraps "
        "and annotates, 'block' drops the suspicious text.",
    )
    injection_patterns_extra: list[str] = Field(
        default_factory=list,
        description="More regexes (case-insensitive) that mark untrusted text as an "
        "injection attempt, in addition to the built-in phrases.",
    )
    max_input_chars: int = Field(
        default=200_000, ge=1_000, description="Largest user text accepted by AI endpoints."
    )
    tool_allowlist: dict[str, list[str]] = Field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_TOOL_ALLOWLIST.items()},
        description="Agent tools per role (fnmatch patterns): viewer / editor / admin.",
    )
    check_generated_code: bool = Field(
        default=True,
        description="Generated code must pass AutoAir validation, ruff and ShellCheck "
        "(when installed) before it is suggested.",
    )
    block_secret_output: bool = Field(
        default=True, description="Redact secrets the model writes into suggestions."
    )
    external: list[str] = Field(
        default_factory=list,
        description="Extra guardrails: 'llm_guard', 'nemo:<config dir>', 'guardrails_ai', "
        "or 'package.module:factory' returning a Guardrail (autoair[ai-guardrails]).",
    )

    @field_validator("redact_patterns", "injection_patterns_extra")
    @classmethod
    def _valid_regexes(cls, value: list[str]) -> list[str]:
        # An invalid pattern must fail at load time, not 500 every AI request later.
        for item in value:
            if not item:
                continue
            try:
                re.compile(item)
            except re.error as exc:
                raise ValueError(f"invalid regular expression {item!r}: {exc}") from exc
        return value


class AiAuditSettings(_Section):
    """Audit log of AI calls (who, model, tokens, cost, tools, guardrail events)."""

    enabled: bool = Field(default=True, description="Record every AI request.")
    url: str | None = Field(
        default=None, description="Audit folder (fsspec URL); default <state>/ai-audit."
    )
    include_prompts: bool = Field(
        default=False, description="Also store (redacted) prompts - may contain sensitive data."
    )
    retention_days: int = Field(
        default=90,
        ge=0,
        description="Delete audit day folders older than this many days (0 = keep forever).",
    )
    max_total_mb: int = Field(
        default=1024,
        ge=0,
        description="Delete the oldest audit days while the log is larger than this (0 = no "
        "size limit).",
    )


class KnowledgeSettings(_Section):
    """The built-in knowledge base (Markdown) and site overrides."""

    disabled: list[str] = Field(
        default_factory=list, description="Built-in document ids (file stems) to switch off."
    )
    extra_dirs: list[str] = Field(
        default_factory=list,
        description="More Markdown folders (paths or fsspec URLs); a file with the same name "
        "as a built-in document replaces it.",
    )
    store_url: str | None = Field(
        default=None,
        description="Where uploaded overrides live (fsspec URL); default <state>/ai-knowledge.",
    )
    include_repo_docs: bool = Field(
        default=True, description="Also index AutoAir's docs/ folder when running from source."
    )
    max_upload_bytes: int = Field(
        default=512 * 1024, ge=1024, description="Largest uploaded knowledge document."
    )


class PromptSettings(_Section):
    override_dir: Path | None = Field(
        default=None,
        description="Folder of prompt overrides: <NAME>.md replaces the built-in system "
        "prompt NAME (e.g. REVIEW_SYSTEM.md); see GET /api/ai/prompts.",
    )
    replace: dict[str, str] = Field(
        default_factory=dict,
        description="Replace a built-in system prompt: {NAME: text}, e.g. "
        '{"TROUBLESHOOT_SYSTEM": "..."} (an override_dir file still wins). ${variables} '
        "are substituted (docs/ai-assistants.md#prompt-variables).",
    )
    append: dict[str, str] = Field(
        default_factory=dict,
        description="Text appended to a system prompt: {NAME: text}, e.g. "
        '{"DEVELOPER_ASSISTANT_SYSTEM": "Always answer in German."}; ${variables} are '
        "substituted.",
    )
    variables: dict[str, str] = Field(
        default_factory=dict,
        description="Extra ${name} variables for prompts (besides the built-in ones such as "
        "${organization}, ${user}, ${team}, ${airflow_version}, ${today}).",
    )
    organization: str | None = Field(
        default=None, description="Organisation name the assistants refer to (${organization})."
    )


class AssistSettings(_Section):
    """Limits of the single-call assist features (review, fix, explain, task code)."""

    max_log_chars: int = Field(
        default=12_000, ge=1_000, description="Tail of a failed task's log sent to fix."
    )
    max_code_chars: int = Field(
        default=40_000, ge=1_000, description="Code sent to explain-code (truncated)."
    )
    findings_max_tokens: int = Field(
        default=12_000, ge=256, description="max_tokens of review / leak / optimise / fix calls."
    )
    explain_max_tokens: int = Field(
        default=6_000, ge=256, description="max_tokens of explain-DAG calls."
    )
    explain_code_max_tokens: int = Field(
        default=3_000, ge=256, description="max_tokens of explain-code calls."
    )
    task_code_max_tokens: int = Field(
        default=8_000, ge=256, description="max_tokens of task-code / writer calls."
    )
    fix_rounds: int = Field(
        default=2, ge=1, le=6, description="Patch-and-revalidate rounds of fix."
    )
    default_task_timeout_seconds: int = Field(
        default=3600, ge=60, description="execution_timeout the rules suggest for tasks."
    )
    default_sensor_timeout_seconds: int = Field(
        default=6 * 3600, ge=60, description="timeout the rules suggest for sensors."
    )
    memory_max_items: int = Field(
        default=2000, ge=10, description="Conventions/preferences the AI memory keeps."
    )


class WriterSettings(_Section):
    max_repairs: int = Field(
        default=2, ge=0, le=6, description="Validate-and-repair rounds of the code writers."
    )
    lint_timeout_seconds: float = Field(
        default=15, gt=0, description="Timeout of one ruff / ShellCheck run."
    )
    max_operators_in_prompt: int = Field(
        default=25, ge=1, le=200, description="Catalog operators described to the writers."
    )
    max_packages_in_prompt: int = Field(
        default=120, ge=0, le=2000, description="Installed packages listed to the writers."
    )


class UserLlmSettings(_Section):
    """Per-user models (``llm_mode`` per_user / both): what users may configure."""

    allowed_providers: list[ProviderLiteral] = Field(
        default_factory=_all_providers,
        description="Providers users may choose.",
    )
    allowed_models: list[str] = Field(
        default_factory=lambda: ["*"], description="Model ids users may choose (fnmatch)."
    )
    allowed_base_urls: list[str] = Field(
        default_factory=list,
        description="Custom endpoints users may set (URL prefixes, e.g. "
        "https://llm.internal.example/v1); empty = provider defaults only.",
    )
    allow_private_networks: bool = Field(
        default=False,
        description="Allow user endpoints that resolve to private/loopback addresses (SSRF).",
    )
    allowed_conn_ids: list[str] = Field(
        default_factory=list,
        description="Airflow connection ids users may reference instead of a key (fnmatch); "
        "empty = not allowed (the server would use the connection on their behalf).",
    )
    encryption_key: SecretStr | None = Field(
        default=None,
        description="Fernet key encrypting stored user API keys (generate with "
        "`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key()"
        ".decode())'`); default: a key file created in the state folder.",
    )
    store_url: str | None = Field(
        default=None, description="Where user model settings live; default <state>/ai-users."
    )


class AiServiceSettings(_Section):
    """Running AI as a separate service (``autoair ai-serve``) and proxying to it."""

    replay_cache_max_entries: int = Field(
        default=100_000,
        ge=256,
        description="Maximum signed request nonces retained by SQL-backed AI service "
        "replay detection, divided among 256 hash buckets. A full bucket rejects new "
        "requests until entries expire; live entries are never evicted.",
    )
    timeout_seconds: float = Field(
        default=600, gt=0, description="Proxy timeout (streams may run this long)."
    )
    proxy_mcp: bool = Field(
        default=False, description="Also proxy /mcp to the AI service (instead of in-process)."
    )
    serve_mcp: bool = Field(default=True, description="The AI service also serves /mcp.")
    shared_secret: SecretStr | None = Field(
        default=None,
        description="HMAC secret shared by the main server and the AI service: the proxy "
        "signs the caller identity, the service trusts signed requests.",
    )
    verify_tokens: bool = Field(
        default=True,
        description="AI service: verify unsigned bearer tokens against Airflow.",
    )


class AssistantProfileSettings(_Section):
    """One chat assistant (developer / admin)."""

    enabled: bool = Field(default=True, description="Offer this assistant in the chat panel.")
    tools: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Tools this assistant may call (fnmatch over tool names); the developer "
        "assistant is also limited by ai.guardrails.tool_allowlist of the caller's role.",
    )
    max_steps: int = Field(default=10, ge=1, le=50, description="Model turns per chat turn.")
    memory: bool = Field(
        default=True, description="Use and offer to store memories in this assistant's scope."
    )
    allow_external_mcp: bool = Field(
        default=True,
        description="Attach the external MCP servers scoped to this assistant "
        "(ai.mcp_client.servers[].agents); their tools also go through this profile's tools "
        "allow-list, the developer assistant's ai.guardrails.tool_allowlist (mcp__<server>__"
        "<tool>) and, for the admin assistant, only tools the server marks read-only.",
    )


class AdminProfileSettings(AssistantProfileSettings):
    """The admin assistant (more turns: its questions span IAM, audit and configuration)."""

    max_steps: int = Field(default=12, ge=1, le=50, description="Model turns per chat turn.")


class AssistantsSettings(_Section):
    """The chat assistants ("Ask AI"): developer (ai:use) and admin (ai:admin/admin:panel)."""

    developer: AssistantProfileSettings = Field(
        default_factory=AssistantProfileSettings, description="Developer assistant."
    )
    admin: AdminProfileSettings = Field(
        default_factory=AdminProfileSettings, description="Admin assistant (read-only tools)."
    )
    store_url: str | None = Field(
        default=None,
        description="Where chat threads live (fsspec URL); default <state>/ai-threads (rows "
        "of the SQL database when database.backend is sqlite/postgresql).",
    )
    max_threads_per_user: int = Field(
        default=200, ge=1, description="Threads kept per user and mode (oldest deleted)."
    )
    max_messages_per_thread: int = Field(
        default=400, ge=4, description="Messages kept per thread (oldest dropped)."
    )
    compaction_trigger_chars: int = Field(
        default=40_000,
        ge=2_000,
        description="Compact a thread (summarise older turns) once its history sent to the "
        "model exceeds this many characters.",
    )
    compaction_keep_messages: int = Field(
        default=6, ge=2, le=100, description="Recent messages always sent verbatim."
    )
    summary_max_tokens: int = Field(
        default=1_500, ge=200, description="max_tokens of the compaction summary call."
    )
    max_follow_ups: int = Field(
        default=3, ge=0, le=10, description="Suggested follow-up questions per answer."
    )


ResourceName = Literal[
    "connections",
    "variables",
    "pools",
    "xcom_keys",
    "xcom_values",
    "other_runs",
    "import_errors",
    "airflow_config",
    "filesystem",
    "research",
    "online",
]


class TroubleshootResources(_Section):
    """Airflow resources the troubleshooting agent may read (users can only narrow this)."""

    connections: bool = Field(
        default=True,
        description="Connection metadata: conn_type, host, schema, port and the names of "
        "extra fields - never logins, passwords or extra values.",
    )
    variables: bool = Field(default=True, description="Variable keys (never values).")
    pools: bool = Field(default=True, description="Pools and their slot usage.")
    xcom_keys: bool = Field(default=True, description="XCom keys of the analysed run.")
    xcom_values: bool = Field(
        default=False, description="XCom values (truncated, secrets scrubbed)."
    )
    other_runs: bool = Field(default=True, description="Other runs of the DAG (history).")
    import_errors: bool = Field(default=True, description="DAG import errors.")
    airflow_config: bool = Field(
        default=False,
        description="Airflow's configuration (GET /config; Airflow masks sensitive values "
        "and AutoAir scrubs secrets again).",
    )


def _deny_globs() -> list[str]:
    return [
        ".env",
        "*.env",
        "*.pem",
        "*.key",
        "*.p12",
        "*.pfx",
        "*.kdbx",
        "id_rsa*",
        "id_ed25519*",
        "*secret*",
        "*credential*",
        "*.cfg",
        "*.ini",
        "*.db",
        "*.sqlite*",
    ]


class TroubleshootFilesystem(_Section):
    """Read-only, sandboxed file access for the troubleshooting agent (task log files)."""

    roots: list[Path] = Field(
        default_factory=list,
        description="Folders the agent may read (e.g. Airflow's base_log_folder); empty = no "
        "filesystem access. Symlinks and '..' cannot leave them.",
    )
    include_dags_folder: bool = Field(
        default=False, description="Also allow the DAGs folder (read-only)."
    )
    allow_globs: list[str] = Field(
        default_factory=lambda: ["*"],
        description="File names the agent may read (fnmatch); directories are always listed.",
    )
    deny_globs: list[str] = Field(
        default_factory=_deny_globs,
        description="File names never read (fnmatch, checked on every path component).",
    )
    max_file_bytes: int = Field(
        default=50 * 1024 * 1024,
        ge=1024,
        description="Larger files are refused (only listed).",
    )
    max_read_chars: int = Field(
        default=20_000, ge=1_000, description="Characters returned by one read (head/tail/grep)."
    )
    max_list_entries: int = Field(
        default=200, ge=10, description="Entries returned by one directory listing."
    )


TroubleshootActionsMode = Literal["read_only", "hitl", "apply"]
TroubleshootActionType = Literal[
    "clear",
    "retry",
    "clear_run",
    "mark_success",
    "mark_failed",
    "mark_run_success",
    "mark_run_failed",
]


def _default_actions() -> list[TroubleshootActionType]:
    # Re-running is safe to automate; overriding a recorded state hides failures, so marking
    # needs an explicit opt-in.
    return ["clear", "retry", "clear_run"]


class TroubleshootSettings(_Section):
    """AI troubleshooting of DAG runs (log analysis agent)."""

    enabled: bool = Field(default=True, description="Offer 'Analyze with AI' on runs and tasks.")
    resources: TroubleshootResources = Field(
        default_factory=TroubleshootResources, description="Airflow resources it may read."
    )
    filesystem: TroubleshootFilesystem = Field(
        default_factory=TroubleshootFilesystem, description="Sandboxed log/DAG file access."
    )
    max_tasks: int = Field(
        default=4, ge=1, le=50, description="Failed task instances whose logs are pre-loaded."
    )
    max_tries: int = Field(
        default=4, ge=1, le=20, description="Tries per task instance whose logs are read."
    )
    max_log_chars: int = Field(
        default=12_000, ge=1_000, description="Tail of each try's log given to the agent."
    )
    max_steps: int = Field(default=12, ge=1, le=50, description="Model turns per analysis.")
    timeout_seconds: float = Field(
        default=420, gt=0, description="Wall-clock limit of one analysis."
    )
    allow_external_mcp: bool = Field(
        default=True, description="Let users attach allow-listed external MCP servers."
    )
    store_url: str | None = Field(
        default=None,
        description="Where analyses (reports, proposals, decisions) live; default "
        "<state>/ai-troubleshoot (SQL rows with a database backend).",
    )
    airflow_actions: TroubleshootActionsMode = Field(
        default="read_only",
        description="What happens with the Airflow actions an analysis proposes (clear or "
        "retry a task, clear a run, mark a state): read_only = instructions only; hitl = each "
        "action gets an Execute button and runs only after the user approves it; apply = the "
        "vetted actions run automatically when the analysis completes. Actions always run "
        "with the user's own Airflow token, need run:manage and features.runs, and stay "
        "limited to allowed_actions and the analysed DAG. A request may only choose a more "
        "restrictive mode.",
    )
    allowed_actions: list[TroubleshootActionType] = Field(
        default_factory=_default_actions,
        description="Action types that may be executed (JSON list in the environment): "
        "clear (task instance), retry (a failed task instance), clear_run (only failed tasks "
        "unless the action says otherwise), mark_success / mark_failed (task instance), "
        "mark_run_success / mark_run_failed. Proposals of other types stay instructions.",
    )
    max_actions: int = Field(
        default=5,
        ge=0,
        le=50,
        description="Executable actions per analysis; further proposals stay instructions "
        "(0 = none).",
    )
    max_cleared_task_instances: int = Field(
        default=20,
        ge=1,
        le=1_000,
        description="Task instances one clear may reset (checked with a dry run first, "
        "downstream tasks included); larger clears are refused.",
    )
    action_claim_timeout_seconds: float = Field(
        default=300.0,
        ge=5.0,
        le=86_400.0,
        description="Seconds a hitl action stays claimed (status running) by the replica "
        "executing it; after that another approval may take it over (the replica died "
        "mid-call). Longer than the slowest Airflow clear.",
    )
    allow_include_downstream: bool = Field(
        default=True, description="A clear may include the task's downstream tasks."
    )
    allow_include_upstream: bool = Field(
        default=False, description="A clear may include the task's upstream tasks."
    )
    actions_other_runs: bool = Field(
        default=False,
        description="Actions may target other runs of the analysed DAG (default: only the "
        "analysed run).",
    )


McpAgent = Literal["troubleshoot", "developer", "admin"]


def _default_agents() -> list[McpAgent]:
    return ["troubleshoot"]


class McpServerConfig(_Section):
    """An external MCP server the agents may use as a tool provider."""

    name: str = Field(
        pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$", description="Short id (tool name prefix)."
    )
    description: str = Field(default="", max_length=500, description="Shown to users.")
    transport: Literal["http", "sse", "stdio"] = Field(
        default="http", description="streamable HTTP, SSE or a local stdio process."
    )
    url: str | None = Field(default=None, description="Server URL (http / sse).")
    command: str | None = Field(default=None, description="Executable (stdio only).")
    args: list[str] = Field(default_factory=list, description="Arguments (stdio only).")
    env: dict[str, SecretStr] = Field(
        default_factory=dict, description="Environment of the stdio process (secrets)."
    )
    headers: dict[str, SecretStr] = Field(
        default_factory=dict, description="HTTP headers, e.g. Authorization (secrets)."
    )
    tools: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Tools agents may call (fnmatch over the server's tool names).",
    )
    timeout_seconds: float = Field(default=30, gt=0, description="Timeout of one tool call.")
    enabled: bool = Field(default=True, description="Offer this server.")
    agents: list[McpAgent] = Field(
        default_factory=_default_agents, description="Agents that may attach this server."
    )

    @model_validator(mode="after")
    def _check_transport(self) -> McpServerConfig:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError(f"MCP server {self.name!r}: stdio needs a command")
        elif not self.url or not re.match(r"^https?://", self.url):
            raise ValueError(f"MCP server {self.name!r}: {self.transport} needs an http(s) url")
        return self


class McpClientSettings(_Section):
    """External MCP servers as tool providers of the agents (allow-listed)."""

    servers: list[McpServerConfig] = Field(
        default_factory=list, description="Servers defined in the configuration."
    )
    database_servers: bool = Field(
        default=True,
        description="Also offer servers defined in the admin panel (stored with the state; "
        "http/sse only - stdio commands can only come from the configuration).",
    )
    max_tools_per_server: int = Field(
        default=40, ge=1, le=500, description="Tools of one server given to an agent."
    )
    max_output_chars: int = Field(
        default=12_000, ge=500, description="Characters of one MCP tool result (truncated)."
    )
    connect_timeout_seconds: float = Field(
        default=15, gt=0, description="Timeout of connecting and listing tools."
    )
    store_url: str | None = Field(
        default=None,
        description="Where admin-defined servers live (fsspec URL; header/env secrets "
        "encrypted); default <state>/ai-mcp-servers (SQL: rows).",
    )


class RulesSettings(_Section):
    """Admin-managed project rules (Markdown) injected into authoring/writing prompts."""

    enabled: bool = Field(default=True, description="Inject the project rules.")
    max_chars: int = Field(
        default=8_000, ge=200, description="Characters of rules injected into one prompt."
    )
    store_url: str | None = Field(
        default=None, description="Where rules live; default <state>/ai-rules (SQL: rows)."
    )
    cache_seconds: float = Field(
        default=10, ge=0, description="Seconds the rules are cached per process."
    )


class UpgradeSettings(_Section):
    """The Airflow upgrade assistant (deprecated Airflow 2 idioms)."""

    ruff_rules: list[str] = Field(
        default_factory=lambda: ["AIR3"],
        description="ruff rule selectors run on the generated code (AIR3xx: Airflow 3 "
        "migration rules).",
    )
    max_dags: int = Field(default=100, ge=1, description="DAGs analysed by one 'all DAGs' request.")


class McpSettings(_Section):
    allow_writes: bool = Field(
        default=True,
        description="Allow MCP clients to save/delete DAGs and trigger runs (always behind "
        "confirm=true); false makes the MCP server read-only.",
    )


class AiSettings(_Section):
    """AI assistance: model access, agent, retrieval, research, guardrails and service."""

    enabled: bool = Field(
        default=True,
        description="Build the LLM client. False: LLM features answer 503 while the "
        "rules-only features (leak scan, review, optimise, search) keep working. To hide "
        "AI completely use features.ai=false.",
    )
    # --- central model
    provider: ProviderLiteral | None = Field(
        default=None,
        description="LLM provider (a pydantic-ai provider; install its autoair[ai-*] extra). "
        "Default: inferred from the Airflow connection / the API key that is set (Anthropic "
        "first).",
    )
    model: str | None = Field(
        default=None,
        description="Model id, optionally as 'provider:model' like Airflow common.ai "
        "(default per provider, e.g. claude-sonnet-5-5).",
    )
    models: list[str] = Field(
        default_factory=list, description="Models users may pick per request (besides model)."
    )
    effort: str | None = Field(
        default=None, description="Reasoning effort (low/medium/high/xhigh/max) when supported."
    )
    api_key: SecretStr | None = Field(
        default=None, description="API key of the configured provider (any provider)."
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None, description="Anthropic API key (alternative to api_key)."
    )
    base_url: str | None = Field(
        default=None,
        description="Provider endpoint: OpenAI-compatible base URL (vLLM, LM Studio), Azure "
        "OpenAI endpoint, Ollama URL, or an Anthropic/Gemini gateway.",
    )
    api_version: str | None = Field(
        default=None, description="Azure OpenAI api-version (provider openai: switches to Azure)."
    )
    extra_headers: dict[str, str] = Field(
        default_factory=dict, description="Extra HTTP headers for LLM gateways."
    )
    llm_conn_id: str | None = Field(
        default=None,
        description="Airflow connection holding the model (common.ai 'pydanticai' format: "
        "extra.model 'provider:model', password = API key, host = base URL).",
    )
    timeout_seconds: float = Field(default=180, gt=0, description="Timeout of one model call.")
    max_retries: int = Field(default=3, ge=0, le=10, description="Retries of a model call.")
    max_output_tokens: int = Field(
        default=16_000, ge=256, description="Upper bound of max_tokens for any call."
    )
    pricing: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description='USD per million tokens per model, e.g. {"claude-sonnet-5-5": {"input": '
        '3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}}; enables cost reporting.',
    )
    llm_mode: Literal["central", "per_user", "both"] = Field(
        default="central",
        description="central: everyone uses the model above; per_user: every user configures "
        "their own; both: a user's own model when set, else the central one.",
    )
    memory_url: str | None = Field(
        default=None,
        description="Folder of the AI memory (team/user conventions); default <state>/ai-memory.",
    )
    service_url: str | None = Field(
        default=None,
        description="Base URL of a separate AI service (autoair ai-serve); when set, "
        "/api/ai/* is proxied there instead of running in-process.",
    )
    stream_heartbeat_seconds: float = Field(
        default=15, gt=0, description="SSE keep-alive comment interval."
    )

    embedding: EmbeddingSettings = Field(
        default_factory=EmbeddingSettings, description="Embeddings for hybrid retrieval."
    )
    agent: AgentSettings = Field(default_factory=AgentSettings, description="Agent limits.")
    retrieval: RetrievalSettings = Field(
        default_factory=RetrievalSettings, description="Search, rerank and chunking."
    )
    research: ResearchSettings = Field(
        default_factory=ResearchSettings, description="Agentic research (air-gapped/online)."
    )
    guardrails: GuardrailSettings = Field(
        default_factory=GuardrailSettings, description="Input/output guardrails."
    )
    audit: AiAuditSettings = Field(default_factory=AiAuditSettings, description="AI audit log.")
    knowledge: KnowledgeSettings = Field(
        default_factory=KnowledgeSettings, description="Knowledge base overrides."
    )
    prompts: PromptSettings = Field(default_factory=PromptSettings, description="Prompts.")
    writers: WriterSettings = Field(default_factory=WriterSettings, description="Writers.")
    assist: AssistSettings = Field(
        default_factory=AssistSettings, description="Assist feature limits."
    )
    user_llm: UserLlmSettings = Field(
        default_factory=UserLlmSettings, description="Per-user model restrictions."
    )
    service: AiServiceSettings = Field(
        default_factory=AiServiceSettings, description="Separate AI service / proxy."
    )
    mcp: McpSettings = Field(default_factory=McpSettings, description="MCP server.")
    assistants: AssistantsSettings = Field(
        default_factory=AssistantsSettings, description="Chat assistants (Ask AI)."
    )
    troubleshoot: TroubleshootSettings = Field(
        default_factory=TroubleshootSettings, description="AI troubleshooting of DAG runs."
    )
    mcp_client: McpClientSettings = Field(
        default_factory=McpClientSettings, description="External MCP servers (agent tools)."
    )
    rules: RulesSettings = Field(default_factory=RulesSettings, description="Project AI rules.")
    upgrade: UpgradeSettings = Field(
        default_factory=UpgradeSettings, description="Airflow upgrade assistant."
    )


def ai_settings_of(settings: object | None) -> AiSettings:
    """The ``ai`` section of a ``Settings`` object (defaults for anything else)."""
    section = getattr(settings, "ai", None)
    return section if isinstance(section, AiSettings) else AiSettings()


_ACTIVE: list[AiSettings] = []


def activate_ai(settings: object | None) -> AiSettings:
    """Make ``settings``' ai section the one :func:`active_ai` returns in this process
    (for helpers without a ``Settings`` at hand: rules, assist, memory)."""
    ai = ai_settings_of(settings)
    _ACTIVE[:] = [ai]
    return ai


def active_ai() -> AiSettings:
    if not _ACTIVE:
        _ACTIVE.append(AiSettings())
    return _ACTIVE[0]


def ai_feature_enabled(settings: object | None) -> bool:
    features = getattr(settings, "features", None)
    return bool(getattr(features, "ai", True))


def mcp_feature_enabled(settings: object | None) -> bool:
    features = getattr(settings, "features", None)
    return bool(getattr(features, "mcp", True))


__all__ = [
    "DEFAULT_RESEARCH_DOMAINS",
    "DEFAULT_TOOL_ALLOWLIST",
    "AgentSettings",
    "AiAuditSettings",
    "AiServiceSettings",
    "AiSettings",
    "AssistSettings",
    "AssistantProfileSettings",
    "AssistantsSettings",
    "EmbeddingSettings",
    "GuardrailSettings",
    "KnowledgeSettings",
    "McpClientSettings",
    "McpServerConfig",
    "McpSettings",
    "PromptSettings",
    "ResearchSettings",
    "RetrievalSettings",
    "Role",
    "RulesSettings",
    "TroubleshootFilesystem",
    "TroubleshootResources",
    "TroubleshootSettings",
    "UpgradeSettings",
    "UserLlmSettings",
    "WriterSettings",
    "activate_ai",
    "active_ai",
    "ai_feature_enabled",
    "ai_settings_of",
    "mcp_feature_enabled",
]
